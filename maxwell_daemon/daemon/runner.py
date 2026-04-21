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

from maxwell_daemon import __version__
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
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    cost_usd: float = 0.0
    pr_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None


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
        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._started_at = datetime.now(timezone.utc)
        self._running = False
        # Lazily-built collaborators — injected by tests, built on demand in prod.
        self._github_client: Any = None
        self._workspace: Any = None
        self._issue_executor_factory: Any = None

    @property
    def events(self) -> EventBus:
        return self._events

    @classmethod
    def from_config_path(cls, path: Path | str | None = None) -> Daemon:
        return cls(load_config(path))

    async def start(self, *, worker_count: int = 2, recover: bool = True) -> None:
        if self._running:
            return
        if recover:
            self.recover()
        self._running = True
        for i in range(worker_count):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
        log.info("daemon started with %d workers", worker_count)

    async def set_worker_count(self, n: int) -> None:
        """Rescale the worker pool to exactly *n* workers at runtime.

        :param n: desired number of workers (must be >= 1).
        :raises PreconditionError: if *n* < 1.
        """
        from maxwell_daemon.contracts import require

        require(n >= 1, "worker count must be at least 1")

        current = len(self._workers)
        if n > current:
            # Spawn additional workers.
            for i in range(current, n):
                task = asyncio.create_task(
                    self._worker_loop(i), name=f"worker-{i}"
                )
                self._workers.append(task)
            log.info("set_worker_count: added %d worker(s); total=%d", n - current, n)
        elif n < current:
            # Cancel the excess workers from the end of the list.
            to_cancel = self._workers[n:]
            self._workers = self._workers[:n]
            for worker in to_cancel:
                worker.cancel()
            if to_cancel:
                await asyncio.gather(*to_cancel, return_exceptions=True)
            log.info("set_worker_count: removed %d worker(s); total=%d", current - n, n)

    def recover(self) -> list[Task]:
        """Re-queue tasks from a prior daemon run. Called automatically from start()."""
        recovered = self._task_store.recover_pending()
        with self._tasks_lock:
            for task in recovered:
                self._tasks[task.id] = task
                self._queue.put_nowait(task)
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
    ) -> Task:
        task = Task(
            id=uuid.uuid4().hex[:12],
            prompt=prompt,
            kind=TaskKind.PROMPT,
            repo=repo,
            backend=backend,
            model=model,
        )
        # Write to self._tasks under lock to prevent iteration errors
        with self._tasks_lock:
            self._tasks[task.id] = task
        self._task_store.save(task)
        self._queue.put_nowait(task)
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
        )
        # Write to self._tasks under lock to prevent iteration errors
        with self._tasks_lock:
            self._tasks[task.id] = task
        self._task_store.save(task)
        self._queue.put_nowait(task)
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

    def state(self) -> DaemonState:
        with self._tasks_lock:
            tasks_snapshot = dict(self._tasks)
        return DaemonState(
            version=__version__,
            config_path=None,
            tasks=tasks_snapshot,
            started_at=self._started_at,
            backends_available=self._router.available_backends(),
            worker_count=len(self._workers),
            queue_depth=self._queue.qsize(),
        )

    async def _worker_loop(self, worker_id: int) -> None:
        from maxwell_daemon.logging import bind_context

        log.info("worker %d ready", worker_id)
        while self._running or not self._queue.empty():
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
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
