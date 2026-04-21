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
        self._loop = asyncio.get_running_loop()
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

    def recover