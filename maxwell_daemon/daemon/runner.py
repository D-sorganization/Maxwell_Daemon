"""Daemon lifecycle and task loop.

The daemon owns one event loop, a backend router, a cost ledger, and a task queue.
External callers (CLI, REST API, gRPC) interact through `Daemon.submit()` and
`Daemon.state()`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maxwell_daemon import __version__
from maxwell_daemon.audit import AuditLogger
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
    CostLedger,
)
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.auth_session_store import AuthSessionStore
from maxwell_daemon.core.delegate_lifecycle import (
    DelegateLifecycleService,
    DelegateSessionStore,
)
from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.core.work_item_store import WorkItemStore
from maxwell_daemon.core.work_items import WorkItem, WorkItemStatus
from maxwell_daemon.daemon.single_instance import InstanceLock
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

log = get_logger("maxwell_daemon.daemon")

# Task data models are extracted to task_models.py (phase 1 of #798).
# Re-exported here for backwards-compatibility so existing import paths
# (e.g. ``from maxwell_daemon.daemon.runner import Task``) keep working.
# Fleet coordinator logic is extracted to fleet_coordinator.py (phase 2 of #798).
from maxwell_daemon.daemon.fleet_coordinator import FleetCoordinator  # noqa: E402
from maxwell_daemon.daemon.maintenance import DaemonMaintenanceMixin  # noqa: E402
from maxwell_daemon.daemon.retry_policy import DEFAULT_RETRY_POLICY  # noqa: E402
from maxwell_daemon.daemon.submission import DaemonSubmissionMixin  # noqa: E402
from maxwell_daemon.daemon.task_models import (  # noqa: E402
    DaemonState,
    DuplicateTaskIdError,
    QueueSaturationError,
    Task,
    TaskKind,
    TaskStatus,
)
from maxwell_daemon.daemon.worker import WorkerExecutionMixin  # noqa: E402

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "ConfigSnapshot",
    "Daemon",
    "DaemonState",
    "DuplicateTaskIdError",
    "FleetCoordinator",
    "QueueSaturationError",
    "Task",
    "TaskKind",
    "TaskStatus",
]


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    """Frozen execution collaborators captured when a worker claims a task."""

    config: MaxwellDaemonConfig
    router: BackendRouter
    budget: BudgetEnforcer
    ledger: CostLedger


@functools.total_ordering
class _StopSentinel:
    """Totally-ordered stop marker for the worker PriorityQueue.

    The queue holds ``(priority, payload)`` tuples and ``heapq`` falls back to
    comparing the second element when priorities tie.  A bare ``None`` sentinel
    is not orderable, so two stop markers at the same priority raised
    ``TypeError: '<' not supported between instances of 'NoneType'`` mid-heappush
    — corrupting the heap and failing the scale-down API (issue #974).  This
    sentinel sorts deterministically against itself and after any real ``Task``
    (it is only ever enqueued at priority ``-1``, ahead of all tasks, so the
    tiebreaker is exercised only when multiple sentinels collide).
    """

    __slots__ = ()

    def __lt__(self, other: object) -> bool:
        # All sentinels are interchangeable: never strictly less than anything.
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StopSentinel)

    def __hash__(self) -> int:
        return hash(_StopSentinel)


# Singleton stop marker shared by every scale-down signal.
_STOP = _StopSentinel()


class Daemon(DaemonMaintenanceMixin, DaemonSubmissionMixin, WorkerExecutionMixin):
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

        from maxwell_daemon.mcp.client import McpClientManager

        self._mcp_manager = McpClientManager(config.mcp_servers)

        self._router = BackendRouter(config, mcp_manager=self._mcp_manager)
        self._fleet_registry = InMemoryFleetCapabilityRegistry()
        storage_root = (
            Path(ledger_path).expanduser().parent
            if ledger_path is not None
            else Path.home() / ".local/share/maxwell-daemon"
        )
        self._ledger = CostLedger(ledger_path or storage_root / "ledger.db")
        self._budget = BudgetEnforcer(config.budget, self._ledger)
        self._events = EventBus()
        self._workspace_root = (
            workspace_root or Path.home() / ".local/share/maxwell-daemon/workspaces"
        )
        # Durable stores live next to the cost ledger by default. Keeping the
        # default root derived from an explicit ledger path lets tests and
        # embedded instances isolate all SQLite state with one parameter.
        default_store = storage_root / "tasks.db"
        self._task_store = TaskStore(task_store_path or default_store)
        # Single-instance guard keyed on the storage root (#975): a second
        # daemon against the same root would corrupt state / double-spend.
        self._instance_lock = InstanceLock(storage_root)
        default_work_item_store = storage_root / "work_items.db"
        self._work_item_store = WorkItemStore(work_item_store_path or default_work_item_store)
        default_task_graph_store = storage_root / "task_graphs.db"
        self._task_graph_store = TaskGraphStore(task_graph_store_path or default_task_graph_store)
        default_artifact_store = storage_root / "artifacts.db"
        default_artifact_root = storage_root / "artifacts"
        self._artifact_store = ArtifactStore(
            artifact_store_path or default_artifact_store,
            blob_root=artifact_blob_root or default_artifact_root,
        )
        self._task_graphs = TaskGraphService(
            store=self._task_graph_store,
            artifact_store=self._artifact_store,
        )
        default_action_store = storage_root / "actions.db"
        self._action_store = ActionStore(action_store_path or default_action_store)
        self._actions = ActionService(
            self._action_store,
            policy=ActionPolicy(
                mode=ApprovalMode(config.tools.approval_tier),
                workspace_root=self._workspace_root,
            ),
            events=self._events,
            on_side_effect_started=self.mark_task_side_effects_started,
        )
        default_delegate_store = (
            delegate_lifecycle_store_path or storage_root / "delegate_sessions.db"
        )
        self._delegate_lifecycle = DelegateLifecycleService(
            DelegateSessionStore(default_delegate_store)
        )
        from maxwell_daemon.core.template_store import TemplateStore

        self._template_store = TemplateStore(Path.home() / ".local/share/maxwell-daemon/templates")

        default_auth_store = auth_store_path or storage_root / "auth_sessions.db"
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
        # asyncio.PriorityQueue is loop-affine and not safe for concurrent
        # off-loop put/full calls before the daemon loop starts running.
        self._queue_lock = threading.Lock()
        self._needs_restart = False
        self._restart_required_reasons: list[str] = []
        # PriorityQueue: workers dequeue (priority, task) tuples. Lower priority
        # number = higher urgency (0=emergency, 50=high, 100=normal, 200=batch).
        self._queue: asyncio.PriorityQueue[tuple[int, Task | _StopSentinel]] = (
            asyncio.PriorityQueue(maxsize=config.agent.max_queue_depth)
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
        # Created lazily in start() when role == "coordinator" (see fleet_coordinator.py).
        self._fleet_coordinator: FleetCoordinator | None = None
        self._active_execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_stream_event_at: dict[str, datetime] = {}
        self._last_stream_event_kind: dict[str, str] = {}
        self._stalled_task_ids: set[str] = set()

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def template_store(self) -> Any:
        return self._template_store

    @classmethod
    def from_config_path(cls, path: Path | str | None = None) -> Daemon:
        resolved = Path(path).expanduser() if path else default_config_path()
        return cls(load_config(resolved), config_path=resolved)

    def _capture_config_snapshot(self) -> ConfigSnapshot:
        with self._config_lock:
            return ConfigSnapshot(
                config=self._config,
                router=self._router,
                budget=self._budget,
                ledger=self._ledger,
            )

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
        new_budget = BudgetEnforcer(new_config.budget, self._ledger)
        new_router = BackendRouter(new_config, mcp_manager=self._mcp_manager, budget=new_budget)
        with self._config_lock:
            self._config = new_config
            self._router = new_router
            self._budget = BudgetEnforcer(new_config.budget, self._ledger)
            if hasattr(self, "_actions"):
                self._actions._policy = ActionPolicy(
                    mode=ApprovalMode(new_config.tools.approval_tier),
                    workspace_root=self._workspace_root,
                )
            if self._fleet_coordinator is not None:
                self._fleet_coordinator.update_config(new_config)

        log.info("config reloaded from %s", path)
        return path

    @staticmethod
    def _task_kind_key(task: Task) -> str:
        if task.kind is TaskKind.ISSUE and task.issue_mode:
            return task.issue_mode
        return task.kind.value

    def _kind_cap_for(self, task: Task) -> int | None:
        key = self._task_kind_key(task)
        with self._config_lock:
            return self._config.agent.concurrency_by_kind.get(key)

    def _count_running_tasks_locked(self, *, kind_key: str) -> int:
        return sum(
            1
            for running in self._tasks.values()
            if running.status is TaskStatus.RUNNING and self._task_kind_key(running) == kind_key
        )

    async def _restore_deferred(self, deferred: list[tuple[int, Task]]) -> None:
        """Re-enqueue deferred entries without ever losing them on ``QueueFull``.

        These entries were just dequeued from this same bounded queue, so the
        space normally exists — but a concurrent producer could fill the queue
        between the ``get`` and the re-``put``.  ``put_nowait`` would then raise
        ``QueueFull`` and silently drop the held entry (issue #973).  We fall
        back to an awaitable ``put`` for any entry that does not fit so deferred
        work is always preserved.
        """
        for deferred_item in deferred:
            try:
                self._queue.put_nowait(deferred_item)
            except asyncio.QueueFull:
                await self._queue.put(deferred_item)

    def _classify_dependencies_locked(self, task: Task) -> tuple[str | None, bool]:
        """Classify a task's dependencies. Caller must hold ``_tasks_lock``.

        Returns ``(failed_dependency, deps_pending)``:

        * ``failed_dependency`` is the id of the first dependency that terminally
          failed/cancelled — such a dependency will never reach COMPLETED, so
          deferring on it would strand the dependent in a tight re-enqueue loop
          forever (#978c). When set, the dependent should be failed.
        * ``deps_pending`` is True when at least one dependency is still
          unfinished (missing or not yet COMPLETED) and none has failed — the
          dependent should be deferred.
        """
        for dep in task.depends_on:
            dep_task = self._tasks.get(dep)
            if dep_task is not None and dep_task.status in (
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                return dep, False
        deps_pending = any(
            self._tasks.get(dep) is None or self._tasks[dep].status is not TaskStatus.COMPLETED
            for dep in task.depends_on
        )
        return None, deps_pending

    async def _claim_next_queued_task(self) -> tuple[bool, Task | None]:
        deferred: list[tuple[int, Task]] = []
        claimed: Task | None = None
        while True:
            try:
                if deferred:
                    priority, payload = self._queue.get_nowait()
                else:
                    priority, payload = await asyncio.wait_for(self._queue.get(), timeout=0.2)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                break

            if isinstance(payload, _StopSentinel):
                await self._restore_deferred(deferred)
                return True, None
            task = payload

            kind_cap = self._kind_cap_for(task)
            failed_dependency: str | None = None
            with self._tasks_lock:
                if task.status is not TaskStatus.QUEUED:
                    continue
                if task.depends_on:
                    failed_dependency, deps_pending = self._classify_dependencies_locked(task)
                    if failed_dependency is None and deps_pending:
                        deferred.append((priority, task))
                        continue
                if failed_dependency is not None:
                    dep_message = f"dependency {failed_dependency} failed"
                    task.status = TaskStatus.FAILED
                    task.error = dep_message
                    task.finished_at = datetime.now(timezone.utc)
                else:
                    if kind_cap is not None:
                        kind_key = self._task_kind_key(task)
                        if self._count_running_tasks_locked(kind_key=kind_key) >= kind_cap:
                            deferred.append((priority, task))
                            continue
                    task.status = TaskStatus.RUNNING
                    task.started_at = datetime.now(timezone.utc)
                    claimed = task
            # Persist + publish the dependency-failure outside the lock (no store
            # I/O or awaiting while holding the threading lock), then keep draining
            # the queue rather than claiming the failed task for execution.
            if failed_dependency is not None:
                try:
                    self._task_store.save(task)
                except Exception:
                    log.exception(
                        "task store write failed while failing dependent task=%s", task.id
                    )
                await self._events.publish(
                    Event(
                        kind=EventKind.TASK_FAILED,
                        payload=attach_observability(
                            {
                                "id": task.id,
                                "error": task.error,
                                "reason": "dependency_failed",
                            },
                            task_id=task.id,
                            backend=task.backend,
                            model=task.model,
                        ),
                    )
                )
                continue
            # ``put_nowait``/await of deferred entries must happen OUTSIDE the
            # threading lock (no awaiting while holding it).
            if claimed is not None:
                break

        await self._restore_deferred(deferred)
        return False, claimed

    async def start(self, *, worker_count: int = 2, recover: bool = True) -> None:
        if self._running:
            return
        # Acquire the single-instance lock BEFORE recovery — recovery mutates
        # shared on-disk state, which is exactly what must not happen twice
        # against one storage root (#975). Raises InstanceLockError if held.
        self._instance_lock.acquire()
        if recover:
            self.recover()
        self._running = True
        self._loop = asyncio.get_running_loop()
        role = self._config.role

        if role == "coordinator":
            # Coordinator: runs discovery and dispatches to remote workers — no local execution.
            self._worker_count = 0
            log.info("daemon started as coordinator (no local workers)")
            self._fleet_coordinator = FleetCoordinator(
                config=self._config,
                tasks=self._tasks,
                tasks_lock=self._tasks_lock,
                task_store=self._task_store,
                worker_last_seen=self._worker_last_seen,
                enqueue_task_entry=self._enqueue_task_entry,
                running_flag=lambda: self._running,
            )
            coord_task = asyncio.create_task(
                self._fleet_coordinator.run_loop(), name="coordinator-loop"
            )
            self._bg_tasks.add(coord_task)
            coord_task.add_done_callback(self._bg_tasks.discard)
        elif role == "worker":
            # Worker: accepts tasks via REST API and executes locally — no discovery.
            self._worker_count = worker_count
            self._workers = [
                asyncio.create_task(self._worker_loop(i), name=f"AgentWorker-{i}")
                for i in range(self._worker_count)
            ]
            await self._mcp_manager.start()
            log.info("Daemon workers started", workers=self._worker_count)
        else:
            # Standalone (default): run local workers.
            self._worker_count = worker_count
            for i in range(worker_count):
                self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
            await self._mcp_manager.start()
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

        if self._config.agent.task_live_retention_seconds > 0:
            evict_task = asyncio.create_task(
                self._live_eviction_loop(),
                name="live-memory-eviction",
            )
            self._bg_tasks.add(evict_task)
            evict_task.add_done_callback(self._bg_tasks.discard)
        if self._config.agent.stall_timeout_seconds > 0:
            stall_task = asyncio.create_task(
                self._stall_reconcile_loop(),
                name="stall-reconcile",
            )
            self._bg_tasks.add(stall_task)
            stall_task.add_done_callback(self._bg_tasks.discard)

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

        if getattr(self, "_memory", None) is not None:
            close_method = getattr(self._memory, "aclose", None)
            if close_method is not None:
                await close_method()

        await self._mcp_manager.stop()

        # Release the long-lived per-machine fleet clients' connection pools
        # (#978b) so a coordinator shutdown leaves no leaked httpx sockets.
        if self._fleet_coordinator is not None:
            await self._fleet_coordinator.aclose()

        # Release the single-instance lock so a clean restart can re-acquire it.
        self._instance_lock.release()

        log.info("Daemon shut down")

    def get_task(self, task_id: str) -> Task | None:
        # dict.get is GIL-atomic; no lock needed on hot-path reads.
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        return self._task_store.get(task_id)

    def list_tasks(
        self,
        *,
        limit: int = 100,
        status: TaskStatus | None = None,
        kind: TaskKind | None = None,
        repo: str | None = None,
        completed_before: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list[Task]:
        """List tasks with filtering and pagination.

        This method queries the durable store and does not rely on the in-memory
        live dict, making it safe for large histories.
        """
        return self._task_store.list_tasks(
            limit=limit,
            status=status,
            kind=kind.value if kind else None,
            repo=repo,
            completed_before=completed_before,
            created_before=created_before,
        )

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

    def mark_task_side_effects_started(self, task_id: str) -> None:
        """Persist that a task has crossed the transparent-failover boundary."""
        with self._tasks_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            if task.side_effects_started:
                return
            task.side_effects_started = True
        self._task_store.save(task)

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
            # Send a totally-ordered stop sentinel (priority=-1) to excess
            # workers so they exit cleanly after finishing their current task.
            for _ in range(current - n):
                await self._queue.put((-1, _STOP))
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

    # -- coordinator loop (see daemon/fleet_coordinator.py) -----------------
    # The _coordinator_loop, _dispatch_to_fleet, and _handle_stale_dispatched_task
    # methods have been extracted to FleetCoordinator (phase 2 of #798).
    # Daemon.start() creates a FleetCoordinator instance and drives it via
    # FleetCoordinator.run_loop() when role == "coordinator".

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
            needs_restart=self._needs_restart,
            restart_required_reasons=list(self._restart_required_reasons),
        )

    @property
    def fleet_registry(self) -> InMemoryFleetCapabilityRegistry:
        """Mutable in-memory capability registry for fleet status surfaces."""
        return self._fleet_registry

    @property
    def delegate_lifecycle(self) -> DelegateLifecycleService:
        """Durable delegate session lifecycle service."""
        return self._delegate_lifecycle


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

        # ``loop.add_signal_handler`` raises NotImplementedError on Windows'
        # ProactorEventLoop. Guard every install so the daemon starts there too,
        # matching the SIGUSR1 path in start() (#981). Fall back to
        # ``signal.signal`` for SIGINT/SIGTERM so Ctrl-C still stops the daemon.
        def _request_stop() -> None:
            loop.call_soon_threadsafe(stop.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, OSError):
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, lambda _signum, _frame: _request_stop())

        def _sighup_handler() -> None:
            """Reload config in-place on SIGHUP without stopping workers."""
            try:
                daemon.reload_config()
            except Exception:
                log.exception("config reload failed; keeping existing config")

        sighup = getattr(signal, "SIGHUP", None)
        if sighup is not None:
            # No SIGHUP on Windows — hot-reload-on-signal is simply unavailable.
            with contextlib.suppress(NotImplementedError, OSError):
                loop.add_signal_handler(sighup, _sighup_handler)
        await stop.wait()
        await daemon.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
