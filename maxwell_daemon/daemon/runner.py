"""Daemon lifecycle and task loop.

The daemon owns one event loop, a backend router, a cost ledger, and a task queue.
External callers (CLI, REST API, gRPC) interact through `Daemon.submit()` and
`Daemon.state()`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import signal
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from maxwell_daemon import __version__
from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.config import MaxwellDaemonConfig, load_config
from maxwell_daemon.config.loader import default_config_path
from maxwell_daemon.core import (
    Action,
    ActionKind,
    ActionPolicy,
    ActionRiskLevel,
    ActionService,
    ActionStatus,
    ApprovalMode,
    Artifact,
    ArtifactKind,
    ArtifactStore,
    BackendRouter,
    BudgetEnforcer,
    BudgetExceededError,
    CostLedger,
    CostRecord,
)
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.auth_session_store import AuthSessionStore
from maxwell_daemon.core.delegate_lifecycle import DelegateLifecycleService, DelegateSessionStore
from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.core.work_item_store import WorkItemStore
from maxwell_daemon.core.work_items import WorkItem, WorkItemStatus
from maxwell_daemon.director import (
    GraphNodeExecutor,
    GraphStatus,
    TaskGraphRecord,
    TaskGraphService,
    TaskGraphStore,
    TaskGraphTemplate,
)
from maxwell_daemon.events import Event, EventBus, EventKind, attach_observability
from maxwell_daemon.fleet.capabilities import InMemoryFleetCapabilityRegistry
from maxwell_daemon.logging import get_logger
from maxwell_daemon.metrics import record_request

log = get_logger("maxwell_daemon.daemon")


class QueueSaturationError(Exception):
    """Raised when the priority queue is full and cannot accept more tasks."""

    def __init__(self, message: str, backoff_seconds: int = 60) -> None:
        super().__init__(message)
        self.backoff_seconds = backoff_seconds


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


class DuplicateTaskIdError(ValueError):
    """Raised when a caller supplies a task id that already exists."""


@dataclass
class Task:
    id: str
    prompt: str
    kind: TaskKind = TaskKind.PROMPT
    repo: str | None = None
    backend: str | None = None
    model: str | None = None
    route_reason: str | None = None
    # Issue-specific fields (set when kind == ISSUE).
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None  # "plan" | "implement"
    # A/B grouping: sibling tasks share an ab_group so the UI pairs them.
    ab_group: str | None = None
    # DAG dependencies: list of task IDs that must reach COMPLETED before this
    # task is allowed to start.  An empty list (the default) means "no deps".
    depends_on: list[str] = field(default_factory=list)
    # Priority: lower number = higher priority. 0=emergency, 50=high, 100=normal, 200=batch.
    priority: int = 100
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    waived_by: str | None = None
    waiver_reason: str | None = None
    waived_at: datetime | None = None
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
        config_path: Path | None = None,
        ledger_path: Path | None = None,
        workspace_root: Path | None = None,
        task_store_path: Path | None = None,
        work_item_store_path: Path | None = None,
        task_graph_store_path: Path | None = None,
        artifact_store_path: Path | None = None,
        artifact_blob_root: Path | None = None,
        action_store_path: Path | None = None,
        delegate_lifecycle_store_path: Path | None = None,
        auth_store_path: Path | None = None,
    ) -> None:
        self._config = config
        # Path used for hot-reload; populated by from_config_path.
        self._config_path: Path | None = config_path
        # Protects atomic swap of _config and _router during reload.
        self._config_lock = threading.Lock()
        self._router = BackendRouter(config)
        self._fleet_registry = InMemoryFleetCapabilityRegistry()
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
        default_work_item_store = Path.home() / ".local/share/maxwell-daemon/work_items.db"
        self._work_item_store = WorkItemStore(work_item_store_path or default_work_item_store)
        default_task_graph_store = Path.home() / ".local/share/maxwell-daemon/task_graphs.db"
        self._task_graph_store = TaskGraphStore(task_graph_store_path or default_task_graph_store)
        default_artifact_store = Path.home() / ".local/share/maxwell-daemon/artifacts.db"
        default_artifact_root = Path.home() / ".local/share/maxwell-daemon/artifacts"
        self._artifact_store = ArtifactStore(
            artifact_store_path or default_artifact_store,
            blob_root=artifact_blob_root or default_artifact_root,
        )
        self._task_graphs = TaskGraphService(
            store=self._task_graph_store,
            artifact_store=self._artifact_store,
        )
        default_action_store = Path.home() / ".local/share/maxwell-daemon/actions.db"
        self._action_store = ActionStore(action_store_path or default_action_store)
        self._actions = ActionService(
            self._action_store,
            policy=ActionPolicy(
                mode=ApprovalMode(config.tools.approval_tier),
                workspace_root=self._workspace_root,
            ),
            events=self._events,
        )
        default_delegate_store = (
            delegate_lifecycle_store_path
            or Path.home() / ".local/share/maxwell-daemon/delegate_sessions.db"
        )
        self._delegate_lifecycle = DelegateLifecycleService(
            DelegateSessionStore(default_delegate_store)
        )

        default_auth_store = auth_store_path or (
            Path.home() / ".local/share/maxwell-daemon/auth_sessions.db"
        )
        self._auth_store = AuthSessionStore(default_auth_store)
        # Memory store — co-located with the ledger for easy backup.
        from maxwell_daemon.memory import (
            EpisodicStore,
            MemoryBackend,
            MemoryManager,
            RepoProfile,
            ScratchPad,
        )

        default_memory = self._config.memory_workspace_path / "memory.db"
        self._memory: MemoryBackend
        if self._config.role == "worker" and self._config.fleet_coordinator_url:
            from maxwell_daemon.fleet.memory import RemoteMemoryManager

            self._memory = RemoteMemoryManager(
                coordinator_url=self._config.fleet_coordinator_url,
                auth_token=self._config.api_auth_token,
            )
        else:
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
        self._queue: asyncio.PriorityQueue[tuple[int, Task | None]] = asyncio.PriorityQueue(
            maxsize=config.agent.max_queue_depth
        )
        self._workers: list[asyncio.Task[None]] = []
        self._worker_count: int = 0
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._started_at = datetime.now(timezone.utc)
        self._running = False
        # Captured in :meth:`start`; used by :meth:`submit_threadsafe` to
        # schedule cross-thread queue puts via the running event loop.
        self._loop: asyncio.AbstractEventLoop | None = None
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
        resolved = Path(path).expanduser() if path else default_config_path()
        return cls(load_config(resolved), config_path=resolved)

    def reload_config(self) -> Path:
        """Reload configuration from disk and swap atomically.

        Thread-safe: acquires ``_config_lock`` before updating ``_config`` and
        ``_router`` so in-flight workers always see a consistent pair. Running
        workers are *not* interrupted — they complete with the old config and
        new tasks pick up the new config automatically.

        Returns the path that was reloaded.

        Raises ``FileNotFoundError`` if the config file cannot be found and
        ``pydantic.ValidationError`` if the new config is invalid — the existing
        config is left untouched in both cases.
        """
        path = self._config_path or default_config_path()
        # Validate first (outside the lock) so we never swap in a bad config.
        new_config = load_config(path)
        new_router = BackendRouter(new_config)
        with self._config_lock:
            self._config = new_config
            self._router = new_router
        log.info("config reloaded from %s", path)
        return path

    async def start(self, *, worker_count: int = 2, recover: bool = True) -> None:
        if self._running:
            return
        if recover:
            self.recover()
        self._running = True
        self._loop = asyncio.get_running_loop()
        role = self._config.role

        if role == "coordinator":
            # Coordinator: runs discovery and dispatches to remote workers — no local execution.
            self._worker_count = 0
            log.info("daemon started as coordinator (no local workers)")
            coord_task = asyncio.create_task(self._coordinator_loop(), name="coordinator-loop")
            self._bg_tasks.add(coord_task)
            coord_task.add_done_callback(self._bg_tasks.discard)
        elif role == "worker":
            # Worker: accepts tasks via REST API and executes locally — no discovery.
            self._worker_count = worker_count
            for i in range(worker_count):
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
            log.info("daemon started as worker with %d workers", worker_count)
        else:
            # Standalone (default): run local workers.
            self._worker_count = worker_count
            for i in range(worker_count):
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
            log.info("daemon started (standalone) with %d workers", worker_count)

        # Install SIGUSR1 handler for config hot-reload (Unix only).
        sigusr1 = getattr(signal, "SIGUSR1", None)
        if sigusr1 is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.add_signal_handler(
                    sigusr1,
                    lambda: asyncio.create_task(self._reload_config_signal()),
                )
            except (NotImplementedError, OSError):
                # Windows or unsupported platform — skip signal handler.
                pass
        if self._config.agent.task_retention_days > 0:
            prune_task = asyncio.create_task(self._retention_loop(), name="retention-pruner")
            self._bg_tasks.add(prune_task)
            prune_task.add_done_callback(self._bg_tasks.discard)
        if self._config.memory_dream_interval_seconds > 0:
            dream_task = asyncio.create_task(
                self._dream_cycle_loop(),
                name="memory-dream-cycle",
            )
            self._bg_tasks.add(dream_task)
            dream_task.add_done_callback(self._bg_tasks.discard)
        log.info("daemon started with %d workers", self._worker_count)

    def recover(self) -> list[Task]:
        """Recover non-terminal tasks from a prior daemon run.

        Queued tasks are re-enqueued locally. Fleet-dispatched tasks are restored
        to the in-memory task map without entering the local worker queue so a
        restarted coordinator can continue accounting for the remote lease.
        """
        recovered = self._task_store.recover_pending()
        queued = 0
        dispatched = 0
        with self._tasks_lock:
            for task in recovered:
                self._tasks[task.id] = task
                if task.status is TaskStatus.QUEUED:
                    self._enqueue_task_entry(task.priority, task)
                    queued += 1
                elif task.status is TaskStatus.DISPATCHED:
                    dispatched += 1
        if recovered:
            log.info(
                "recovered %d pending task(s) from previous run (queued=%d dispatched=%d)",
                len(recovered),
                queued,
                dispatched,
            )
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

        # Stop background loops and flush fire-and-forget tasks.
        if self._bg_tasks:
            for task in self._bg_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
            self._bg_tasks.clear()

        log.info("daemon stopped")

    def prune_retained_history(self, older_than_days: int | None = None) -> dict[str, int]:
        """Prune terminal tasks and ledger rows older than the retention window."""
        days = (
            self._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        if days <= 0:
            return {"tasks": 0, "ledger_records": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        with self._tasks_lock:
            stale_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.status in terminal
                and task.finished_at is not None
                and task.finished_at < cutoff
            ]
            for task_id in stale_ids:
                self._tasks.pop(task_id, None)

        pruned_tasks = self._task_store.prune(days)
        pruned_ledger = self._ledger.prune(days)
        return {"tasks": pruned_tasks, "ledger_records": pruned_ledger}

    async def aprune_retained_history(self, older_than_days: int | None = None) -> dict[str, int]:
        """Prune retained history without blocking the event loop on SQLite work."""
        days = (
            self._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        if days <= 0:
            return {"tasks": 0, "ledger_records": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        with self._tasks_lock:
            stale_ids = [
                task_id
                for task_id, task in self._tasks.items()
                if task.status in terminal
                and task.finished_at is not None
                and task.finished_at < cutoff
            ]
            for task_id in stale_ids:
                self._tasks.pop(task_id, None)

        pruned_tasks, pruned_ledger = await asyncio.gather(
            self._task_store.aprune(days),
            self._ledger.aprune(days),
        )
        return {"tasks": pruned_tasks, "ledger_records": pruned_ledger}

    async def _retention_loop(self) -> None:
        interval = self._config.agent.task_prune_interval_seconds
        while self._running:
            try:
                result = await self.aprune_retained_history()
                if result["tasks"] or result["ledger_records"]:
                    log.info("retention prune completed: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("retention prune failed", exc_info=True)
            await asyncio.sleep(interval)

    async def _dream_cycle_loop(self) -> None:
        """Periodically consolidate raw markdown memory when explicitly enabled."""
        while self._running:
            interval = self._config.memory_dream_interval_seconds
            if interval <= 0:
                return
            await asyncio.sleep(interval)
            if not self._running:
                return
            try:
                result = await self.run_memory_dream_cycle()
                log.info("memory dream cycle completed: %s", result)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("memory dream cycle failed", exc_info=True)

    async def run_memory_dream_cycle(self) -> str:
        """Run one memory anneal pass against the configured local markdown store."""
        from maxwell_daemon.core.memory_annealer import MemoryAnnealer
        from maxwell_daemon.core.roles import Role, RoleOrchestrator

        annealer = MemoryAnnealer(workspace=self._config.memory_workspace_path)
        if annealer.status().raw_log_count == 0:
            return "No raw memory to anneal."

        role = Role(
            name="memory_summarizer",
            system_prompt=(
                "You consolidate raw Maxwell-Daemon execution logs into concise, durable "
                "markdown memory. Preserve technical decisions, repository conventions, "
                "and lessons learned. Drop transient chatter and secrets."
            ),
        )
        summarizer = RoleOrchestrator(self._router).assign_player(role)
        return await MemoryAnnealer(
            workspace=self._config.memory_workspace_path,
            summarizer_role=summarizer,
        ).anneal()

    def _enqueue_task_entry(self, priority: int, task: Task | None) -> None:
        """Insert a queue entry while respecting daemon loop thread affinity."""
        item = (priority, task)
        if self._queue.full():
            log.warning("queue is saturated (max_depth=%d)", self._config.agent.max_queue_depth)
            raise QueueSaturationError(
                "Task queue is full, please try again later", backoff_seconds=60
            )

        if self._loop is None or not self._loop.is_running():
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull as exc:
                raise QueueSaturationError(
                    "Task queue is full, please try again later", backoff_seconds=60
                ) from exc
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is self._loop:
            # If we are on the event loop thread, we might be inside a signal handler.
            # Mutating the PriorityQueue inline can corrupt the heap if the signal
            # interrupted a heapq operation. Use call_soon_threadsafe to defer safely.
            def _put_inline() -> None:
                try:
                    self._queue.put_nowait(item)
                except asyncio.QueueFull:
                    log.error("Queue saturated inline; dropped task %s", getattr(task, "id", None))

            self._loop.call_soon_threadsafe(_put_inline)
            return

        result: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _put() -> None:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                result.set_exception(
                    QueueSaturationError(
                        "Task queue is full, please try again later", backoff_seconds=60
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced via Future
                result.set_exception(exc)
            else:
                result.set_result(None)

        self._loop.call_soon_threadsafe(_put)
        result.result(timeout=5.0)

    def submit(
        self,
        prompt: str,
        *,
        repo: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        priority: int = 100,
        task_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> Task:
        resolved_task_id = task_id or uuid.uuid4().hex[:12]
        task = Task(
            id=resolved_task_id,
            prompt=prompt,
            kind=TaskKind.PROMPT,
            repo=repo,
            backend=backend,
            model=model,
            priority=priority,
            depends_on=list(depends_on) if depends_on else [],
        )
        # Persist and track the task under lock, then route the queue mutation
        # through the daemon loop if this caller is on a foreign thread.
        with self._tasks_lock:
            if task_id is not None:
                self._reject_duplicate_task_id(task.id)
            self._task_store.save(task)
            self._tasks[task.id] = task
            try:
                self._enqueue_task_entry(task.priority, task)
            except QueueSaturationError:
                del self._tasks[task.id]
                self._task_store.delete(task.id)
                raise
        # Fire-and-forget: if there's no running loop yet (e.g. sync test
        # submits before start()), skip the event — the queued state is
        # observable via get_task().
        try:
            loop = asyncio.get_running_loop()
            # Task kept alive via strong reference in _bg_tasks.
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_QUEUED,
                        payload=attach_observability(
                            {"id": task.id},
                            task_id=task.id,
                        ),
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No running event loop — called from a sync context before start().
            # The task is already enqueued; the missing event is acceptable here.
            pass
        return task

    def submit_threadsafe(
        self,
        prompt: str,
        *,
        repo: str | None = None,
        backend: str | None = None,
        model: str | None = None,
    ) -> Task:
        """Enqueue a prompt task from any thread. **Cross-thread safe.**

        Unlike :meth:`submit`, this method is safe to call from threads that
        are *not* running the daemon's event loop (e.g. WSGI middleware,
        background threads, sync test clients).  It uses
        ``asyncio.run_coroutine_threadsafe`` to schedule the queue put on the
        running event loop so the sleeping worker is reliably woken.

        :raises RuntimeError: if the daemon has not been started yet
            (``self._loop`` is ``None``).
        """
        if self._loop is None:
            raise RuntimeError("daemon must be started before submit_threadsafe()")
        task = Task(
            id=uuid.uuid4().hex[:12],
            prompt=prompt,
            kind=TaskKind.PROMPT,
            repo=repo,
            backend=backend,
            model=model,
        )
        self._task_store.save(task)
        with self._tasks_lock:
            self._tasks[task.id] = task
            try:
                self._enqueue_task_entry(task.priority, task)
            except QueueSaturationError:
                del self._tasks[task.id]
                self._task_store.delete(task.id)
                raise
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
        task_id: str | None = None,
    ) -> Task:
        """Queue a task that reads a GitHub issue and opens a draft PR for it."""
        if mode not in {"plan", "implement"}:
            raise ValueError(f"mode must be 'plan' or 'implement', got {mode!r}")
        resolved_task_id = task_id or uuid.uuid4().hex[:12]
        task = Task(
            id=resolved_task_id,
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
        # See note in submit(): queue mutation must stay loop-affine once the
        # daemon has started.
        with self._tasks_lock:
            if task_id is not None:
                self._reject_duplicate_task_id(task.id)
            self._task_store.save(task)
            self._tasks[task.id] = task
            try:
                self._enqueue_task_entry(task.priority, task)
            except QueueSaturationError:
                del self._tasks[task.id]
                self._task_store.delete(task.id)
                raise
        try:
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
        except RuntimeError:
            # No running event loop — called from a sync context before start().
            # The task is already enqueued; the missing event is acceptable here.
            pass
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

    def _reject_duplicate_task_id(self, task_id: str) -> None:
        get_persisted_task = getattr(self._task_store, "get", None)
        persisted_task = get_persisted_task(task_id) if callable(get_persisted_task) else None
        if task_id in self._tasks or persisted_task is not None:
            raise DuplicateTaskIdError(f"task id {task_id!r} already exists")

    def create_work_item(self, item: WorkItem) -> WorkItem:
        self._work_item_store.save(item)
        loaded = self._work_item_store.get(item.id)
        if loaded is None:
            raise RuntimeError(f"work item {item.id} was not persisted")
        return loaded

    def update_work_item(self, item: WorkItem) -> WorkItem:
        if self._work_item_store.get(item.id) is None:
            raise KeyError(item.id)
        self._work_item_store.save(item)
        loaded = self._work_item_store.get(item.id)
        if loaded is None:
            raise RuntimeError(f"work item {item.id} was not persisted")
        return loaded

    def get_work_item(self, item_id: str) -> WorkItem | None:
        return self._work_item_store.get(item_id)

    def list_work_items(
        self,
        *,
        limit: int = 100,
        status: WorkItemStatus | None = None,
        repo: str | None = None,
        source: str | None = None,
        max_priority: int | None = None,
    ) -> list[WorkItem]:
        return self._work_item_store.list_items(
            limit=limit,
            status=status,
            repo=repo,
            source=source,
            max_priority=max_priority,
        )

    def set_task_graph_executor(self, executor: GraphNodeExecutor | None) -> None:
        """Inject or clear the task graph node executor.

        Production backend-routed execution is intentionally a follow-up slice;
        tests and specialized hosts can supply a concrete executor now.
        """
        self._task_graphs.set_executor(executor)

    def create_task_graph(
        self,
        work_item_id: str,
        *,
        template: TaskGraphTemplate | None = None,
        graph_id: str | None = None,
        labels: tuple[str, ...] = (),
    ) -> TaskGraphRecord:
        item = self._work_item_store.get(work_item_id)
        if item is None:
            raise KeyError(work_item_id)
        return self._task_graphs.create_from_work_item(
            item,
            template=template,
            graph_id=graph_id,
            labels=labels,
        )

    def get_task_graph(self, graph_id: str) -> TaskGraphRecord | None:
        return self._task_graphs.get(graph_id)

    def list_task_graphs(
        self,
        *,
        work_item_id: str | None = None,
        status: GraphStatus | None = None,
        limit: int = 100,
    ) -> list[TaskGraphRecord]:
        return self._task_graphs.list_records(
            work_item_id=work_item_id,
            status=status,
            limit=limit,
        )

    def start_task_graph(self, graph_id: str) -> TaskGraphRecord:
        return self._task_graphs.start(graph_id)

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._artifact_store.get(artifact_id)

    def propose_action(
        self,
        *,
        task_id: str,
        kind: ActionKind,
        summary: str,
        payload: dict[str, Any] | None = None,
        work_item_id: str | None = None,
        risk_level: ActionRiskLevel = ActionRiskLevel.MEDIUM,
    ) -> Action:
        action, _decision = self._actions.propose(
            task_id=task_id,
            kind=kind,
            summary=summary,
            payload=payload,
            work_item_id=work_item_id,
            risk_level=risk_level,
        )
        return action

    def get_action(self, action_id: str) -> Action | None:
        return self._actions.get(action_id)

    def list_task_actions(self, task_id: str) -> list[Action]:
        return self._actions.list_for_task(task_id)

    def list_actions(
        self,
        *,
        status: ActionStatus | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        limit: int = 100,
    ) -> list[Action]:
        return self._actions.list(
            status=status,
            task_id=task_id,
            work_item_id=work_item_id,
            limit=limit,
        )

    def approve_action(
        self,
        action_id: str,
        *,
        actor: str,
        audit: AuditLogger | None = None,
    ) -> Action:
        return self._actions.approve(action_id, actor=actor, audit=audit)

    def reject_action(
        self,
        action_id: str,
        *,
        actor: str,
        reason: str | None = None,
        audit: AuditLogger | None = None,
    ) -> Action:
        return self._actions.reject(action_id, actor=actor, reason=reason, audit=audit)

    def read_artifact_bytes(self, artifact_id: str) -> bytes:
        return self._artifact_store.read_bytes(artifact_id)

    def list_task_artifacts(
        self,
        task_id: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        return self._artifact_store.list_for_task(task_id, kind=kind)

    def list_work_item_artifacts(
        self,
        item_id: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        return self._artifact_store.list_for_work_item(item_id, kind=kind)

    def transition_work_item(self, item_id: str, status: WorkItemStatus) -> WorkItem:
        return self._work_item_store.transition(item_id, status)

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
        try:
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
        except RuntimeError:
            # No running event loop — sync cancellation before daemon start.
            # The status change is already persisted; the missing event is acceptable.
            pass
        return task

    def retry_task(self, task_id: str, *, expected_status: TaskStatus) -> Task:
        """Requeue a failed task after checking the caller's stale-state guard."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status is not expected_status:
                raise ValueError(
                    f"task {task_id} is {task.status.value}; expected {expected_status.value}"
                )
            if task.status is not TaskStatus.FAILED:
                raise ValueError(
                    f"task {task_id} is {task.status.value}; only failed tasks can be retried"
                )
            task.status = TaskStatus.QUEUED
            task.result = None
            task.error = None
            task.started_at = None
            task.finished_at = None
            task.dispatched_to = None
            task.waived_by = None
            task.waiver_reason = None
            task.waived_at = None
            self._task_store.save(task)
        try:
            loop = asyncio.get_running_loop()
            bg = loop.create_task(
                self._events.publish(
                    Event(
                        kind=EventKind.TASK_QUEUED,
                        payload={"id": task_id, "reason": "retry"},
                    )
                )
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        except RuntimeError:
            # No running event loop — sync retry before daemon start.
            # The task is already re-queued; the missing event is acceptable.
            pass
        return task

    def waive_task(
        self,
        task_id: str,
        *,
        expected_status: TaskStatus,
        actor: str,
        reason: str,
    ) -> Task:
        """Record an explicit gate waiver without rewriting the original failure."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(task_id)
            if task.status is not expected_status:
                raise ValueError(
                    f"task {task_id} is {task.status.value}; expected {expected_status.value}"
                )
            if task.status is not TaskStatus.FAILED:
                raise ValueError(
                    f"task {task_id} is {task.status.value}; only failed tasks can be waived"
                )
            task.waived_by = actor
            task.waiver_reason = reason
            task.waived_at = datetime.now(timezone.utc)
            self._task_store.save(task)
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
                task = asyncio.create_task(self._worker_loop(worker_id), name=f"worker-{worker_id}")
                self._workers.append(task)
                log.info(
                    "scaled up: added worker %d (total=%d)",
                    worker_id,
                    len(self._workers),
                )
        elif n < current:
            # Send sentinel (priority=-1, task=None) to excess workers so they
            # exit cleanly after finishing their current task.
            for _ in range(current - n):
                await self._queue.put((-1, None))
            # Remove excess workers from tracking immediately; sentinels will
            # signal them to exit after finishing their current task.
            self._workers = self._workers[:n]
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
            # Enqueue a fresh entry with the new priority; the stale entry will
            # be skipped when dequeued because the task will no longer be
            # QUEUED by then (or the worker will simply execute it at the
            # corrected priority). Cross-thread callers still need the queue
            # mutation to bounce through the daemon loop.
            self._enqueue_task_entry(new_priority, task)
        self._task_store.save(task)
        log.info("reprioritized task=%s old=%d new=%d", task_id, old_priority, new_priority)
        return task

    # -- coordinator loop ----------------------------------------------------

    async def _coordinator_loop(self) -> None:
        """Periodically flush QUEUED tasks to remote workers via FleetDispatcher."""
        poll_seconds = self._config.fleet_coordinator_poll_seconds
        while self._running:
            try:
                await self._dispatch_to_fleet()
            except Exception:
                log.exception("coordinator dispatch error")
            await asyncio.sleep(poll_seconds)

    async def _dispatch_to_fleet(self) -> None:
        """One coordinator dispatch tick: probe machines, plan, submit, requeue stale tasks."""
        from maxwell_daemon.fleet.client import RemoteDaemonClient, RemoteDaemonError
        from maxwell_daemon.fleet.dispatcher import (
            FleetDispatcher,
            MachineState,
            TaskRequirement,
        )

        fleet_machines = self._config.fleet_machines
        if not fleet_machines:
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
            for m in fleet_machines
        )

        client = RemoteDaemonClient(
            auth_token=self._config.api_auth_token,
        )

        # Probe all machines in parallel to get live health.
        machines = await client.refresh_all(initial_machines)

        # Requeue tasks dispatched to machines that have gone offline.
        now = datetime.now(timezone.utc)
        stale_threshold = self._config.fleet_heartbeat_seconds * 3
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        for t in tasks_snapshot.values():
            if t.status is not TaskStatus.DISPATCHED or t.dispatched_to is None:
                continue
            machine_name = t.dispatched_to
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
                        t.id,
                    )
                    t.status = TaskStatus.QUEUED
                    t.dispatched_to = None
                    self._enqueue_task_entry(t.priority, t)

        # Collect tasks still QUEUED after potential requeuing above.
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)

        queued_tasks = [t for t in tasks_snapshot.values() if t.status is TaskStatus.QUEUED]
        if not queued_tasks:
            return

        task_requirements = tuple(TaskRequirement(task_id=t.id) for t in queued_tasks)

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
            assigned_task = tasks_by_id.get(assignment.task_id)
            machine = machines_by_name.get(assignment.machine_name)
            if assigned_task is None or machine is None:
                continue

            task_payload: dict[str, Any] = {
                "task_id": assigned_task.id,
                "prompt": assigned_task.prompt,
                "kind": assigned_task.kind.value,
                "repo": assigned_task.repo,
                "backend": assigned_task.backend,
                "model": assigned_task.model,
                "issue_repo": assigned_task.issue_repo,
                "issue_number": assigned_task.issue_number,
                "issue_mode": assigned_task.issue_mode,
                "priority": assigned_task.priority,
            }

            try:
                result = await client.submit_task(machine, task_payload=task_payload)
            except RemoteDaemonError:
                log.exception(
                    "failed to dispatch task %s to machine %s",
                    assigned_task.id,
                    machine.name,
                )
                continue

            if result.status == "submitted":
                assigned_task.status = TaskStatus.DISPATCHED
                assigned_task.dispatched_to = machine.name
                log.info("dispatched task %s to machine %s", assigned_task.id, machine.name)
                try:
                    self._task_store.save(assigned_task)
                except Exception as exc:
                    log.warning(
                        "Failed to persist DISPATCHED state for task %s: %s",
                        assigned_task.id,
                        exc,
                        exc_info=True,
                    )
            else:
                log.warning(
                    "machine %s rejected task %s: %s",
                    machine.name,
                    assigned_task.id,
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
            self.reload_config()
        except Exception:
            log.exception("config reload via SIGUSR1 failed")

    def state(self) -> DaemonState:
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)
        return DaemonState(
            version=__version__,
            config_path=self._config_path,
            tasks=tasks_snapshot,
            started_at=self._started_at,
            backends_available=self._router.available_backends(),
            worker_count=self._worker_count,
            queue_depth=self._queue.qsize(),
        )

    @property
    def fleet_registry(self) -> InMemoryFleetCapabilityRegistry:
        """Mutable in-memory capability registry for fleet status surfaces."""
        return self._fleet_registry

    @property
    def delegate_lifecycle(self) -> DelegateLifecycleService:
        """Durable delegate session lifecycle service."""
        return self._delegate_lifecycle

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
            # Reprioritization leaves stale queue entries behind. Only the first
            # worker that atomically claims a still-queued task may execute it.
            with self._tasks_lock:
                if task.status is not TaskStatus.QUEUED:
                    continue
                # DAG dependency check: if any upstream task is not yet in a
                # terminal-success state, requeue and wait for the next tick.
                if task.depends_on:
                    unfinished = [
                        dep
                        for dep in task.depends_on
                        if self._tasks.get(dep, None) is None
                        or self._tasks[dep].status is not TaskStatus.COMPLETED
                    ]
                    if unfinished:
                        log.debug(
                            "task %s waiting for dependencies %s; re-queuing",
                            task.id,
                            unfinished,
                        )
                        # Re-enqueue without changing status so the task stays QUEUED
                        # and will be retried once the dependencies finish.
                        self._queue.put_nowait((task.priority, task))
                        self._queue.task_done()
                        continue
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now(timezone.utc)
            with bind_context(task_id=task.id, worker_id=worker_id):
                # Attempt to mark the task RUNNING in the durable store before
                # executing.  If that write fails (disk full, lock contention),
                # re-queue the task so it is retried rather than silently lost.
                # Double-execution is still possible if the DB write succeeds but
                # the worker crashes before execution completes; preventing that
                # fully requires a lease/heartbeat mechanism (future work).
                try:
                    self._task_store.update_status(
                        task.id, TaskStatus.RUNNING, started_at=task.started_at
                    )
                except Exception as exc:
                    log.error(
                        "failed to mark task %s RUNNING: %s; re-queuing",
                        task.id,
                        exc,
                    )
                    with self._tasks_lock:
                        if task.status is TaskStatus.RUNNING:
                            task.status = TaskStatus.QUEUED
                            task.started_at = None
                    await self._queue.put((task.priority, task))
                    continue
                await self._execute(task)

    async def _execute(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        try:
            self._task_store.update_status(task.id, TaskStatus.RUNNING, started_at=task.started_at)
        except Exception:
            log.exception("task store write failed for task=%s", task.id)
            raise
        decision_backend = decision_model = "unknown"
        try:
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_STARTED,
                    payload=attach_observability(
                        {"id": task.id, "prompt": task.prompt},
                        task_id=task.id,
                    ),
                )
            )
            self._budget.require_under_budget()
            decision = self._router.route(
                repo=task.repo,
                backend_override=task.backend,
                model_override=task.model,
            )
            task.backend = decision.backend_name
            task.route_reason = decision.reason
            if task.kind is not TaskKind.ISSUE:
                task.model = decision.model
            decision_backend = task.backend
            decision_model = task.model or decision.model
            try:
                self._task_store.save(task)
            except Exception:
                log.exception("task store write failed while recording route for task=%s", task.id)

            if task.kind is TaskKind.ISSUE:
                await self._execute_issue(task, decision)
                return

            resp = await decision.backend.complete(
                [Message(role=MessageRole.USER, content=task.prompt)],
                model=decision.model,
            )
            task.result = resp.content
            estimated_cost = decision.backend.estimate_cost(resp.usage, decision.model)
            task.cost_usd = estimated_cost if estimated_cost is not None else 0.0
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
                    payload=attach_observability(
                        {"id": task.id, "cost_usd": task.cost_usd},
                        task_id=task.id,
                        backend=decision.backend_name,
                        model=decision.model,
                        cost_usd=task.cost_usd,
                        duration_seconds=(
                            datetime.now(timezone.utc) - task.started_at
                        ).total_seconds(),
                    ),
                )
            )
        except BudgetExceededError as e:
            log.warning("task %s refused: %s", task.id, e)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            active_backend = task.backend or decision_backend
            active_model = task.model or decision_model
            record_request(
                backend=active_backend,
                model=active_model,
                status="budget_exceeded",
            )
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload=attach_observability(
                        {
                            "id": task.id,
                            "error": str(e),
                            "reason": "budget_exceeded",
                        },
                        task_id=task.id,
                        backend=active_backend,
                        model=active_model,
                    ),
                )
            )
        except Exception as e:
            log.exception("task %s failed", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
            active_backend = task.backend or decision_backend
            active_model = task.model or decision_model
            record_request(backend=active_backend, model=active_model, status="error")
            await self._events.publish(
                Event(
                    kind=EventKind.TASK_FAILED,
                    payload=attach_observability(
                        {"id": task.id, "error": str(e)},
                        task_id=task.id,
                        backend=active_backend,
                        model=active_model,
                    ),
                )
            )
        finally:
            task.finished_at = datetime.now(timezone.utc)
            try:
                self._memory.scratchpad.clear(task.id)
            except Exception as exc:
                log.warning("scratchpad clear failed for task %s: %s", task.id, exc, exc_info=True)
            # Persist the final task state so restarts see exactly what the
            # daemon saw. Save rather than update_status because status may
            # have flipped more than once through the try/except chain.
            try:
                self._task_store.save(task)
            except Exception:
                # The task completed in-memory; log so operators can investigate
                # disk/lock issues, but don't alter the in-memory status because
                # the work result is already recorded on the Task object.
                log.exception("task store write failed for task=%s", task.id)
            if (
                self._memory is not None
                and hasattr(self._memory, "scratchpad")
                and getattr(self._memory, "scratchpad", None) is not None
            ):
                try:
                    self._memory.scratchpad.clear(task.id)
                except AttributeError:
                    # Scratchpad API mismatch — log but don't crash the task.
                    log.warning("scratchpad.clear API not available for task %s", task.id)

    async def _execute_issue(self, task: Task, decision: Any) -> None:
        """Run the issue → PR flow. Called with status already RUNNING."""
        from maxwell_daemon.core.repo_overrides import resolve_overrides
        from maxwell_daemon.gh import GitHubClient
        from maxwell_daemon.gh.executor import IssueExecutor
        from maxwell_daemon.gh.workspace import Workspace

        if task.issue_repo is None:
            raise ValueError(f"_execute_issue called for task {task.id!r} with no issue_repo set")
        if task.issue_number is None:
            raise ValueError(f"_execute_issue called for task {task.id!r} with no issue_number set")

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
                artifact_store=self._artifact_store,
            )
        )

        mode = task.issue_mode if task.issue_mode in {"plan", "implement"} else "plan"
        overrides = resolve_overrides(self._config, repo=task.issue_repo)

        # Smart model selection: if the task didn't specify a model AND the
        # backend has a tier_map, pick by issue complexity. Otherwise fall back
        # to whatever the router resolved.
        effective_model = decision.model
        backend_cfg = self._router._backend_config(decision.backend_name)
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

        task.backend = decision.backend_name
        task.model = effective_model
        task.route_reason = decision.reason
        try:
            self._task_store.save(task)
        except Exception:
            log.exception(
                "task store write failed while recording issue routing for task=%s", task.id
            )

        async def _emit_test_output(chunk: str, stream: str) -> None:
            await self._events.publish(
                Event(
                    kind=EventKind.TEST_OUTPUT,
                    payload=attach_observability(
                        {
                            "task_id": task.id,
                            "chunk": chunk,
                            "stream": stream,
                        },
                        task_id=task.id,
                    ),
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
            model=effective_model,
            status="success",
        )
        await self._events.publish(
            Event(
                kind=EventKind.TASK_COMPLETED,
                payload=attach_observability(
                    {
                        "id": task.id,
                        "kind": "issue",
                        "repo": task.issue_repo,
                        "issue": task.issue_number,
                        "pr_url": result.pr_url,
                    },
                    task_id=task.id,
                    backend=decision.backend_name,
                    model=effective_model,
                ),
            )
        )


def main() -> None:
    """Run the daemon standalone (systemd entrypoint)."""
    from maxwell_daemon.logging import configure_logging

    daemon = Daemon.from_config_path()
    log_file = getattr(daemon._config, "log_file", None)
    configure_logging(level="INFO", log_file=log_file)

    async def _run() -> None:
        await daemon.start()
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        def _sighup_handler() -> None:
            """Reload config in-place on SIGHUP without stopping workers."""
            try:
                daemon.reload_config()
            except Exception:
                log.exception("config reload failed; keeping existing config")

        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            loop.add_signal_handler(sighup, _sighup_handler)
        await stop.wait()
        await daemon.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
