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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from conductor.backends import Message, MessageRole
from conductor.config import ConductorConfig, load_config
from conductor.core import (
    BackendRouter,
    BudgetEnforcer,
    BudgetExceededError,
    CostLedger,
    CostRecord,
)
from conductor.events import Event, EventBus, EventKind
from conductor.metrics import record_request

log = logging.getLogger("conductor.daemon")


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: str
    prompt: str
    repo: str | None = None
    backend: str | None = None
    model: str | None = None
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    cost_usd: float = 0.0
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


class Daemon:
    def __init__(self, config: ConductorConfig, *, ledger_path: Path | None = None) -> None:
        self._config = config
        self._router = BackendRouter(config)
        self._ledger = CostLedger(ledger_path or Path.home() / ".local/share/conductor/ledger.db")
        self._budget = BudgetEnforcer(config.budget, self._ledger)
        self._events = EventBus()
        self._tasks: dict[str, Task] = {}
        self._queue: asyncio.Queue[Task] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._started_at = datetime.now(timezone.utc)
        self._running = False

    @property
    def events(self) -> EventBus:
        return self._events

    @classmethod
    def from_config_path(cls, path: Path | str | None = None) -> Daemon:
        return cls(load_config(path))

    async def start(self, *, worker_count: int = 2) -> None:
        if self._running:
            return
        self._running = True
        for i in range(worker_count):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"worker-{i}"))
        log.info("daemon started with %d workers", worker_count)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
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
            repo=repo,
            backend=backend,
            model=model,
        )
        self._tasks[task.id] = task
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

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def state(self) -> DaemonState:
        return DaemonState(
            version="0.1.0",
            config_path=None,
            tasks=dict(self._tasks),
            started_at=self._started_at,
            backends_available=self._router.available_backends(),
        )

    async def _worker_loop(self, worker_id: int) -> None:
        log.info("worker %d ready", worker_id)
        while self._running:
            try:
                task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            await self._execute(task)

    async def _execute(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.now(timezone.utc)
        await self._events.publish(
            Event(kind=EventKind.TASK_STARTED, payload={"id": task.id, "prompt": task.prompt})
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
                    payload={"id": task.id, "error": str(e), "reason": "budget_exceeded"},
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
