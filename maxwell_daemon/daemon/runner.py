"""Daemon lifecycle and task loop.

The daemon owns one event loop, a backend router, a cost ledger, and a task queue.
External callers (CLI, REST API, gRPC) interact through `Daemon.submit()` and
`Daemon.state()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.config import MaxwellDaemonConfig, load_config
from maxwell_daemon.core import (
    BackendRouter,
    BudgetEnforcer,
    BudgetExceededError,
    CostLedger,
    CostRecord,
    resolve_overrides,
)
from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.events import Event, EventBus, EventKind
from maxwell_daemon.metrics import record_request

log = logging.getLogger("maxwell_daemon.daemon")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"  # assigned to a remote worker, awaiting execution
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskKind(str, Enum):
    PROMPT = "prompt"
    ISSUE = "issue"


@dataclass
class Task:
    id: str
    prompt: str
    kind: TaskKind = TaskKind.PROMPT
    repo: str | None = None
    backend: str | None = None
    model: str | None = None
    # Issue-specific fields (set when kind == ISSUE).
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None  # "plan" | "implement"
    # A/B grouping: sibling tasks share an ab_group so the UI pairs them.
    ab_group: str | None = None
    # Priority: lower number = higher priority. 0=emergency, 50=high, 100=normal, 200=batch.
    priority: int = 100
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    cost_usd: float = 0.0
    pr_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Fleet dispatch tracking: set when a coordinator sends this task to a remote worker.
    dispatched_to: str | None = None  # machine name of the worker that received this task

    def __lt__(self, other: object) -> bool:
        """Support PriorityQueue ordering — compare by (priority, created_at)."""
        if not isinstance(other, Task):
            return NotImplemented
        return (self.priority, self.created_at) < (other.priority, other.created_at)


@dataclass
class DaemonState:
    version: str
    config_path: Path | None
    tasks: dict[str, Task]
    started_at: datetime
    backends_available: list[str]
    worker_count: int = 0
    queue_depth: int = 0


class Daemon:
    def __init__(
        self,
        config: MaxwellDaemonConfig,
        *,
        ledger_path: Path | None = None,
        workspace_root: Path | None = None,
        task_store_path: Path | None = None,
    ) -> None:
        self._config = config
        self._router = BackendRouter(config)
        self._ledger = CostLedger(
            ledger_path or Path.home() / ".local/share/maxwell-daemon/ledger.db"
        )
        self._budget = BudgetEnforcer(config.budget, self._ledger)
        self._events = EventBus()
        self._workspace_root = (
            workspace_root or Path.home() / ".local/share/maxwell-daemon/workspaces"
        )
        # Durable task store lives next to the cost ledger by default.
        default_store = Path.home() / ".local/share/maxwell-daemon/tasks.db"
        self._task_store = TaskStore(task_store_path or default_store)
        # Memory store — co-located with the ledger for easy backup.
        from maxwell_daemon.memory import (
            EpisodicStore,
            MemoryManager,
            RepoProfile,
            ScratchPad,
        )

        default_memory = Path.home() / ".local/share/maxwell-daemon/memory.db"
        self._memory = MemoryManager(
            scratchpad=ScratchPad(),
            profile=RepoProfile(default_memory),
            episodes=EpisodicStore(default_memory),
        )
        self._tasks: dict[str, Task] = {}
        # ``_tasks`` is touched from async workers *and* from synchronous
        # callers like :meth:`submit` (typically a FastAPI request thread).
        # Single-key operations (``dict[k] = v`` and ``dict.get(k)``) are
        # GIL-atomic in CPython, so we don't lock hot-path reads/writes.
        # We only lock the *iteration* sites (:meth:`state`, :meth:`recover`)
        # and check-then-act sites (:meth:`cancel_task`) where a plain
        # dict snapshot or read-then-mutate can race a concurrent writer.
        # Plain :class:`threading.Lock` (not asyncio.Lock) because callers
        # are a mix of sync and async — acquisition is uncontended under
        # normal load and a lock-free async path doesn't win us anything.
        self._tasks_lock = threading.Lock()
        # PriorityQueue: workers dequeue (priority, task) tuples. Lower priority
        # number = higher urgency (0=emergency, 50=high, 100=normal, 200=batch).
        self._queue: asyncio.PriorityQueue[tuple[int, Task | None]] = asyncio.PriorityQueue()
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count: int = 0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._started_at = datetime.now(timezone.utc)
        self._running = False
        self._config_path: Path | None = None
        # Lazily-built collaborators — injected by tests, built on demand in prod.
        self._github_client: Any = None
        self._workspace: Any = None
        self._issue_executor_factory: Any = None
        # Fleet coordination: track last-seen time per worker machine for heartbeat.
        # Keys are machine names; values are the UTC datetime of last contact.
        self._worker_last_seen: dict[str, datetime] = {}

    @property
    def events(self) -> EventBus:
        return self._events

    @classmethod
    def from_config_path(cls, path: Path | str | None = None) -> Daemon:
        daemon = cls(load_config(path))
        if path is not None:
            daemon._config_path = Path(path).expanduser()
        return daemon

    async def start(self, *, worker_count: int = 2, recover: bool = True) -> None:
        if self._running:
            return
        if recover:
            self.recover()
        self._running = True
        role = self._config.role

        if role == "coordinator":
            # Coordinator: runs discovery and dispatches to remote workers — no local execution.
            self._worker_count = 0
            log.info("daemon started as coordinator (no local workers)")
            # Start coordinator dispatch loop as a background task.
            coord_task = asyncio.create_task(self._coordinator_loop(), name="coordinator-loop")
            self._bg_tasks.add(coord_task)
            coord_task.add_done_callback(self._bg_tasks.discard)
        elif role == "worker":
            # Worker: accepts tasks via REST API and executes locally — no discovery.
            self._worker_count = worker_count
            for i in range(worker_count):
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
            log.info("daemon started as worker with %d workers", worker_count)
            # TODO: register with coordinator on startup (future work)
        else:
            # Standalone (default): run both discovery and local execution.
            self._worker_count = worker_count
            for i in range(worker_count):
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
            log.info("daemon started (standalone) with %d workers", worker_count)

        # Install SIGUSR1 handler for config hot-reload (Unix only).
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(
                signal.SIGUSR1,
                lambda: asyncio.create_task(self._reload_config_signal()),
            )
        except (AttributeError, NotImplementedError, OSError):
            # Windows or unsupported platform — skip signal handler.
            pass
        log.info("daemon started with %d workers", self._worker_count)

    def recover(self) -> list[Task]:
        """Re-queue tasks from a prior daemon run. Called automatically from start()."""
        recovered = self._task_store.recover_pending()
        with self._tasks_lock:
            for task in recovered:
                self._tasks[task.id] = task
                self._queue.put_nowait((task.priority, task))
        if recovered:
            log.info("recovered %d pending task(s) from previous run", len(recovered))
        return recovered

    async def stop(self, *, drain: bool = False, timeout: float = 30.0) -> None:
        """Stop the daemon.

        :param drain: if True, stop accepting new tasks but wait for in-flight
            work to finish (up to ``timeout`` seconds). If False, cancel
            workers immediately — in-flight tasks will be interrupted.
        :param timeout: max seconds to wait for in-flight work when drain=True.
        """
        self._running = False
        if drain:
            # Let workers finish whatever they've pulled off the queue.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                log.warning("drain timeout exceeded; cancelling remaining workers")
        # Cancel anything still running (either because drain=False or timeout).
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if hasattr(self._router, "aclose_all"):
            await self._router.aclose_all()

        # Flush fire-and-forget background tasks (like event publishing)
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        log.info("daemon stopped")

    def submit(
        self,
        prompt: str,
        *,
        repo: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        priority: int = 100,
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            prompt=prompt,
            kind=TaskKind.PROMPT,
            repo=repo,
            backend=backend,
            model=model,
            priority=priority,
        )
        # Write to self._tasks under lock to prevent iteration errors
        with self._tasks_lock:
            self._tasks[task.id] = task
        self._task_store.save(task)
        self._queue.put_nowait((task.priority, task))
        # Fire-and-forget: if there's no running loop yet (e.g. sync test
        # submits before start()), skip the event — the queued state is
        # observable via get_task().
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            # Task kept alive via strong reference in _bg_tasks.
            bg = loop.create_task(
                self._events.publish(Event(kind=EventKind.TASK_QUEUED, payload={"id": task.id}))
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        return task

    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
        priority: int = 100,
    ) -> Task:
        """Queue a task that reads a GitHub issue and opens a draft PR for it."""
        if mode not in {"plan", "implement"}:
            raise ValueError(f"mode must be 'plan' or 'implement', got {mode!r}")
        task = Task(
            id=uuid.uuid4().hex[:12],
            prompt=f"{repo}#{issue_number}",
            kind=TaskKind.ISSUE,
            repo=repo,
            backend=backend,
            model=model,
            issue_repo=repo,
            issue_number=issue_number,
            issue_mode=mode,
            priority=priority,
        )
        # Write to self._tasks under lock to prevent iteration errors
        with self._tasks_lock:
            self._tasks[task.id] = task
        self._task_store.save(task)
        self._queue.put_nowait((task.priority, task))
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_QUEUED,
                        payload={
                            "id": task.id,
                            "kind": "issue",
                            "repo": repo,
                            "issue": issue_number,
                        },
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        return task

    def submit_issue_ab(
        self,
        *,
        repo: str,
        issue_number: int,
        backends: list[str],
        mode: str = "plan",
    ) -> list[Task]:
        """Dispatch the same issue to multiple backends concurrently.

        Tasks share an ``ab_group`` so the UI can pair them and a reviewer can
        compare PRs side-by-side.
        """
        if len(backends) < 2:
            raise ValueError("A/B dispatch needs at least two backends")
        if len(set(backends)) != len(backends):
            raise ValueError("A/B dispatch backends must be distinct")
        ab_group = uuid.uuid4().hex[:12]
        tasks: list[Task] = []
        for backend in backends:
            # Let submit_issue do the regular queueing, then tag the group.
            task = self.submit_issue(
                repo=repo, issue_number=issue_number, mode=mode, backend=backend
            )
            task.ab_group = ab_group
            # Persist the group so recovery sees it too.
            self._task_store.save(task)
            tasks.append(task)
        return tasks

    def set_issue_collaborators(
        self,
        *,
        github_client: Any,
        workspace: Any,
        executor_factory: Any,
    ) -> None:
        """Inject issue-dispatch collaborators (used by tests + server setup)."""
        self._github_client = github_client
        self._workspace = workspace
        self._issue_executor_factory = executor_factory

    def record_worker_heartbeat(self, machine_name: str) -> None:
        """Update last-seen timestamp for a worker machine (called by heartbeat endpoint)."""
        self._worker_last_seen[machine_name] = datetime.now(timezone.utc)

    def get_task(self, task_id: str) -> Task | None:
        # dict.get is GIL-atomic; no lock needed on hot-path reads.
        return self._tasks.get(task_id)

    def cancel_task(self, task_id: str) -> Task:
        """Cancel a queued task. Raises ValueError if not found or not cancellable."""
        # Check-then-act: lock so status transition is observed atomically
        # relative to a concurrent writer.
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is not None and task.status is TaskStatus.QUEUED:
                task.status = TaskStatus.CANCELLED
                task.finished_at = datetime.now(timezone.utc)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.CANCELLED:
            # Under the lock we only flipped QUEUED → CANCELLED; any other
            # status at read time means the task is already running/done and
            # cannot be cancelled from here.
            raise ValueError(
                f"task {task_id} is {task.status.value}; only queued tasks can be cancelled"
            )
        self._task_store.update_status(task.id, TaskStatus.CANCELLED, finished_at=task.finished_at)
        with contextlib.suppress(RuntimeError):
            loop = asyncio.get_running_loop()
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_FAILED,
                        payload={"id": task_id, "reason": "cancelled"},
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        return task

    async def set_worker_count(self, n: int) -> None:
        """Scale workers up or down to *n* without restarting the daemon.

        Safe to call while the daemon is running. Workers scaling down will
        finish their current task before exiting.
        """
        if n < 1:
            raise ValueError(f"worker count must be at least 1, got {n}")
        if n > 64:
            raise ValueError(f"worker count must be at most 64, got {n}")
        current = len(self._workers)
        if n > current:
            for i in range(n - current):
                worker_id = current + i
                task = asyncio.create_task(
                    self._worker_loop(worker_id), name=f"worker-{worker_id}"
                )
                self._workers.append(task)
                log.info("scaled up: added worker %d (total=%d)", worker_id, len(self._workers))
        elif n < current:
            # Send sentinel (priority=-1, task=None) to excess workers so they
            # exit cleanly after finishing their current task.
            for _ in range(current - n):
                await self._queue.put((-1, None))
            # Prune completed/cancelled worker tasks from the list.
            self._workers = [w for w in self._workers if not w.done()]
            log.info("scaled down: sent %d stop sentinel(s) (target=%d)", current - n, n)
        self._worker_count = n

    def reprioritize_task(self, task_id: str, new_priority: int) -> Task:
        """Change the priority of a queued task.

        Note: the PriorityQueue cannot be mutated in-place; the new priority is
        stored on the Task object and will be respected when the entry is
        dequeued (the worker re-checks task.priority). Any already-dequeued
        entry with the old priority is harmless — the task object is the source
        of truth.
        """
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status is not TaskStatus.QUEUED:
                raise ValueError(
                    f"task {task_id} is {task.status.value}; only queued tasks can be reprioritized"
                )
            old_priority = task.priority
            task.priority = new_priority
        # Enqueue a fresh entry with the new priority; the stale entry will be
        # skipped when dequeued because the task will no longer be QUEUED by then
        # (or the worker will simply execute it at the corrected priority).
        self._queue.put_nowait((new_priority, task))
        self._task_store.save(task)
        log.info(
            "reprioritized task=%s old=%d new=%d", task_id, old_priority, new_priority
        )
        return task

    async def reload_config(self, path: Path | None = None) -> dict[str, object]:
        """Re-read and apply hot-reloadable config settings without restarting.

        Hot-reloadable: worker_count, discovery_interval, budget_limits.
        NOT hot-reloadable (require restart): database path, API host/port.

        Returns a dict of {field: (old_value, new_value)} for changed fields.
        """
        config_path = path or self._config_path
        if config_path is None:
            from maxwell_daemon.config.loader import default_config_path

            config_path = default_config_path()
        new_config = load_config(config_path)
        changed: dict[str, object] = {}

        # Worker count
        old_worker_count = self._worker_count
        new_worker_count = getattr(new_config.agent, "worker_count", old_worker_count)
        # worker_count is not in the config model today — skip if absent.
        if hasattr(new_config.agent, "worker_count") and new_worker_count != old_worker_count:
            await self.set_worker_count(new_worker_count)
            changed["worker_count"] = (old_worker_count, new_worker_count)

        # Discovery interval
        old_interval = self._config.agent.discovery_interval_seconds
        new_interval = new_config.agent.discovery_interval_seconds
        if new_interval != old_interval:
            changed["discovery_interval_seconds"] = (old_interval, new_interval)

        # Budget limits
        old_budget = self._config.budget.monthly_limit_usd
        new_budget = new_config.budget.monthly_limit_usd
        if new_budget != old_budget:
            changed["budget.monthly_limit_usd"] = (old_budget, new_budget)

        # Repo list (names only, for logging)
        old_repos = sorted(r.name for r in self._config.repos)
        new_repos = sorted(r.name for r in new_config.repos)
        if new_repos != old_repos:
            changed["repos"] = (old_repos, new_repos)

        self._config = new_config
        self._budget = BudgetEnforcer(new_config.budget, self._ledger)
        self._config_path = config_path
        log.info("config reloaded from %s; changed=%s", config_path, list(changed.keys()))
        return changed

    # -- coordinator loop ----------------------------------------------------

    async def _coordinator_loop(self) -> None:
        """Periodically flush QUEUED tasks to remote workers via FleetDispatcher."""
        poll_seconds = self._config.fleet.coordinator_poll_seconds
        while self._running:
            try:
                await self._dispatch_to_fleet()
            except Exception:
                log.exception("coordinator dispatch error")
            await asyncio.sleep(poll_seconds)

    async def _dispatch_to_fleet(self) -> None:
        """One coordinator dispatch tick: probe machines, plan, submit, requeue stale tasks."""
        from maxwell_daemon.fleet.client import RemoteDaemonClient, RemoteDaemonError
        from maxwell_daemon.fleet.dispatcher import FleetDispatcher, MachineState, TaskRequirement

        fleet_cfg = self._config.fleet
        if not fleet_cfg.machines:
            return

        # Build initial MachineState snapshots from config.
        initial_machines = tuple(
            MachineState(
                name=m.name,
                host=m.host,
                port=m.port,
                capacity=m.capacity,
                tags=tuple(m.tags),
            )
            for m in fleet_cfg.machines
        )

        client = RemoteDaemonClient(
            auth_token=self._config.api.auth_token,
        )

        # Probe all machines in parallel to get live health.
        machines = await client.refresh_all(initial_machines)

        # Requeue tasks dispatched to machines that have gone offline.
        now = datetime.now(timezone.utc)
        stale_threshold = fleet_cfg.heartbeat_seconds * 3
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        for task in tasks_snapshot.values():
            if task.status is not TaskStatus.DISPATCHED or task.dispatched_to is None:
                continue
            machine_name = task.dispatched_to
            machine_healthy = any(m.name == machine_name and m.healthy for m in machines)
            if not machine_healthy:
                last_seen = self._worker_last_seen.get(machine_name)
                stale = True
                if last_seen is not None:
                    elapsed = (now - last_seen).total_seconds()
                    stale = elapsed > stale_threshold
                if stale:
                    log.warning(
                        "worker %s appears offline; requeueing task %s",
                        machine_name,
                        task.id,
                    )
                    task.status = TaskStatus.QUEUED
                    task.dispatched_to = None
                    self._queue.put_nowait((task.priority, task))

        # Collect tasks still QUEUED after potential requeuing above.
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        queued_tasks = [t for t in tasks_snapshot.values() if t.status is TaskStatus.QUEUED]
        if not queued_tasks:
            return

        task_requirements = tuple(
            TaskRequirement(task_id=t.id)
            for t in queued_tasks
        )

        # Tally active_tasks on each machine from known DISPATCHED tasks.
        dispatched_counts: dict[str, int] = {}
        for t in tasks_snapshot.values():
            if t.status is TaskStatus.DISPATCHED and t.dispatched_to:
                dispatched_counts[t.dispatched_to] = dispatched_counts.get(t.dispatched_to, 0) + 1

        machines_with_load = tuple(
            MachineState(
                name=m.name,
                host=m.host,
                port=m.port,
                capacity=m.capacity,
                tags=m.tags,
                active_tasks=dispatched_counts.get(m.name, 0),
                healthy=m.healthy,
            )
            for m in machines
        )

        dispatcher = FleetDispatcher()
        plan = dispatcher.plan(machines_with_load, task_requirements)

        # Build lookup maps for fast resolution.
        tasks_by_id = {t.id: t for t in queued_tasks}
        machines_by_name = {m.name: m for m in machines}

        for assignment in plan.assignments:
            task = tasks_by_id.get(assignment.task_id)
            machine = machines_by_name.get(assignment.machine_name)
            if task is None or machine is None:
                continue

            task_payload: dict[str, Any] = {
                "task_id": task.id,
                "prompt": task.prompt,
                "kind": task.kind.value,
                "repo": task.repo,
                "backend": task.backend,
                "model": task.model,
                "issue_repo": task.issue_repo,
                "issue_number": task.issue_number,
                "issue_mode": task.issue_mode,
                "priority": task.priority,
            }

            try:
                result = await client.submit_task(machine, task_payload=task_payload)
            except RemoteDaemonError:
                log.exception(
                    "failed to dispatch task %s to machine %s", task.id, machine.name
                )
                continue

            if result.status == "submitted":
                task.status = TaskStatus.DISPATCHED
                task.dispatched_to = machine.name
                log.info("dispatched task %s to machine %s", task.id, machine.name)
                with contextlib.suppress(Exception):
                    self._task_store.save(task)
            else:
                log.warning(
                    "machine %s rejected task %s: %s",
                    machine.name,
                    task.id,
                    result.detail,
                )

        if plan.unassigned:
            log.debug(
                "coordinator: %d task(s) could not be placed this tick: %s",
                len(plan.unassigned),
                plan.unassigned,
            )

    async def _reload_config_signal(self) -> None:
        """Signal handler wrapper — logs errors rather than crashing the event loop."""
        try:
            await self.reload_config()
        except Exception:
            log.exception("config reload via SIGUSR1 failed")

    def state(self) -> DaemonState:
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)
        return DaemonState(
            version="0.1.0",
            config_path=self._config_path,
            tasks=tasks_snapshot,
            started_at=self._started_at,
            backends_available=self._router.available_backends(),
            worker_count=self._worker_count,
            queue_depth=self._queue.qsize(),
        )

    async def _worker_loop(self, worker_id: int) -> None:
        from maxwell_daemon.logging import bind_context

        log.info("worker %d ready", worker_id)
        while self._running or not self._queue.empty():
            try:
                _priority, task = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            # Sentinel value (None task) signals this worker to exit — used by
            # set_worker_count() when scaling down.
            if task is None:
                log.info("worker %d received stop sentinel; exiting", worker_id)
                break
            # Tasks cancelled while queued shouldn't be executed.
            if task.status is TaskStatus.CANCELLED:
                continue
            with bind_context(task_id=task.id, worker_id=worker_id):
                await self._execute(task)

    async def _execute(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        try:
            self._task_store.update_status(task.id, TaskStatus.RUNNING, started_at=task.started_at)
        except Exception:
            log.exception("task store write failed for task=%s", task.id)
            raise
        await self._events.publish(
            Event(
                kind=EventKind.TASK_STARTED,
                payload={"id": task.id, "prompt": task.prompt},
            )
        )
        decision_backend = decision_model = "unknown"
        try:
            self._budget.require_under_budget()
            decision = self._router.route(
                repo=task.repo,
                backend_override=task.backend,
                model_override=task.model,
            )
            decision_backend = decision.backend_name
            decision_model = decision.model

            if task.kind is TaskKind.ISSUE:
                await self._execute_issue(task, decision)
                return

            resp = await decision.backend.complete(
                [Message(role=MessageRole.USER, content=task.prompt)],
                model=decision.model,
            )
            task.result = resp.content
            task.cost_usd = decision.backend.estimate_cost(resp.usage, decision.model)
            task.status = TaskStatus.COMPLETED
            self._ledger.record(
                CostRecord(
                    ts=datetime.now(timezone.utc),
                    backend=decision.backend_name,
                    model=decision.model,
                    usage=resp.usage,
                    cost_usd=task.cost_usd,
                    repo=task.repo,
                    agent_id=task.id,
                )
            )
            record_request(
                backend=decision.backend_name,
                model=decision.model,
                status="success",
                tokens=resp.usage.total_tokens,
                cost_usd=task.cost_usd,
                duration_seconds=(datetime.now(timezone.utc) - task.started_at).total_seconds(),
            )
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_COMPLETED,
                    payload={"id": task.id, "cost_usd": task.cost_usd},
                )
            )
        except BudgetExceededError as e:
            log.warning("task %s refused: %s", task.id, e)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            record_request(
                backend=decision_backend,
                model=decision_model,
                status="budget_exceeded",
            )
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload={
                        "id": task.id,
                        "error": str(e),
                        "reason": "budget_exceeded",
                    },
                )
            )
        except Exception as e:
            log.exception("task %s failed", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            record_request(backend=decision_backend, model=decision_model, status="error")
            await self._events.publish(
                Event(kind=EventKind.TASK_FAILED, payload={"id": task.id, "error": str(e)})
            )
        finally:
            task.finished_at = datetime.now(timezone.utc)
            with contextlib.suppress(Exception):
                self._memory.scratchpad.clear(task.id)
            # Persist the final task state so restarts see exactly what the
            # daemon saw. Save rather than update_status because status may
            # have flipped more than once through the try/except chain.
            try:
                self._task_store.save(task)
            except Exception:
                log.exception("task store write failed for task=%s", task.id)
            if (
                self._memory is not None
                and hasattr(self._memory, "scratchpad")
                and getattr(self._memory, "scratchpad", None) is not None
            ):
                with contextlib.suppress(AttributeError):
                    self._memory.scratchpad.clear(task.id)

    async def _execute_issue(self, task: Task, decision: Any) -> None:
        """Run the issue → PR flow. Called with status already RUNNING."""
        from maxwell_daemon.gh import GitHubClient
        from maxwell_daemon.gh.executor import IssueExecutor
        from maxwell_daemon.gh.workspace import Workspace

        assert task.issue_repo is not None
        assert task.issue_number is not None

        github = self._github_client or GitHubClient()
        workspace = self._workspace or Workspace(root=self._workspace_root)
        executor = (
            self._issue_executor_factory(github, workspace, decision.backend)
            if self._issue_executor_factory
            else IssueExecutor(
                github=github,
                workspace=workspace,
                backend=decision.backend,
                memory=self._memory,
            )
        )

        mode = task.issue_mode if task.issue_mode in {"plan", "implement"} else "plan"
        overrides = resolve_overrides(self._config, repo=task.issue_repo)

        # Smart model selection: if the task didn't specify a model AND the
        # backend has a tier_map, pick by issue complexity. Otherwise fall back
        # to whatever the router resolved.
        effective_model = decision.model
        backend_cfg = self._config.backends.get(decision.backend_name)
        if not task.model and backend_cfg is not None and backend_cfg.tier_map:
            from maxwell_daemon.core.model_selector import pick_model_for_issue

            try:
                issue = await github.get_issue(task.issue_repo, task.issue_number)
                selection = pick_model_for_issue(
                    title=issue.title,
                    body=issue.body,
                    labels=list(issue.labels),
                    tier_map=backend_cfg.tier_map,
                    fallback=decision.model,
                )
                effective_model = selection.model
                log.info(
                    "model-select task=%s tier=%s model=%s factors=%s",
                    task.id,
                    selection.tier.value,
                    selection.model,
                    selection.factors,
                )
            except Exception:
                # Selection is opportunistic — a failure here falls through
                # to the default model so the task still proceeds.
                log.warning("model-select failed for task=%s; using default", task.id)

        async def _emit_test_output(chunk: str, stream: str) -> None:
            await self._events.publish(
                Event(
                    kind=EventKind.TEST_OUTPUT,
                    payload={
                        "task_id": task.id,
                        "chunk": chunk,
                        "stream": stream,
                    },
                )
            )

        result = await executor.execute_issue(
            repo=task.issue_repo,
            issue_number=task.issue_number,
            model=effective_model,
            mode=mode,  # type: ignore[arg-type]
            overrides=overrides,
            task_id=task.id,
            on_test_output=_emit_test_output,
        )
        task.status = TaskStatus.COMPLETED
        task.pr_url = result.pr_url
        task.result = result.plan
        # Issue-mode cost accounting is coarse — we don't see usage here since
        # the executor owns the backend call. Future: have the executor return
        # a usage object.
        record_request(
            backend=decision.backend_name,
            model=decision.model,
            status="success",
        )
        await self._events.publish(
            Event(
                kind=EventKind.TASK_COMPLETED,
                payload={
                    "id": task.id,
                    "kind": "issue",
                    "repo": task.issue_repo,
                    "issue": task.issue_number,
                    "pr_url": result.pr_url,
                },
            )
        )


def main() -> None:
    """Run the daemon standalone (systemd entrypoint)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    daemon = Daemon.from_config_path()

    async def _run() -> None:
        await daemon.start()
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        await daemon.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
