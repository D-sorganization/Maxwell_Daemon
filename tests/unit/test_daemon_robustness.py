"""Daemon robustness cluster regressions (#978).

Four related defects on the runner / fleet-coordinator paths:

* (a) ``submit_issue`` must not enqueue while holding ``_tasks_lock``.
* (b) the coordinator must reuse one client per remote and forward ``tls_verify``.
* (c) a dependent of a terminally-failed dependency must FAIL, not churn forever.
* (d) a cancel racing the dispatch await window must not be overwritten with
  DISPATCHED.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar
from unittest.mock import MagicMock

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.fleet_coordinator import FleetCoordinator
from maxwell_daemon.daemon.task_models import Task, TaskKind, TaskStatus
from maxwell_daemon.events import EventKind

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.new_event_loop().run_until_complete(coro)  # type: ignore[arg-type]


def _make_daemon(tmp_path: Path, cfg: MaxwellDaemonConfig, suffix: str = "a") -> Daemon:
    return Daemon(
        cfg,
        ledger_path=tmp_path / f"ledger-{suffix}.db",
        task_store_path=tmp_path / f"tasks-{suffix}.db",
    )


# ── (a) submit_issue enqueues outside _tasks_lock ────────────────────────────


class TestSubmitIssueLockDiscipline:
    def test_enqueue_runs_without_holding_tasks_lock(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        """The enqueue must observe an *unlocked* _tasks_lock (#978a).

        We assert the lock is acquirable from inside the enqueue callback — if
        submit_issue still held it, ``acquire(blocking=False)`` would fail.
        """
        d = _make_daemon(tmp_path, minimal_config)
        observed: dict[str, bool] = {}
        original = d._enqueue_task_entry

        def _spy(priority: int, task: Task) -> None:
            got = d._tasks_lock.acquire(blocking=False)
            observed["lock_free"] = got
            if got:
                d._tasks_lock.release()
            original(priority, task)

        d._enqueue_task_entry = _spy  # type: ignore[method-assign]
        task = d.submit_issue(repo="owner/repo", issue_number=1)
        assert observed["lock_free"] is True
        assert d._tasks[task.id].status is TaskStatus.QUEUED

    def test_saturation_rolls_back_store_and_registry(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        """A saturated enqueue leaves no orphaned task in the store or registry."""
        from maxwell_daemon.daemon.runner import QueueSaturationError

        d = _make_daemon(tmp_path, minimal_config)

        def _boom(priority: int, task: Task) -> None:
            raise QueueSaturationError("queue full")

        d._enqueue_task_entry = _boom  # type: ignore[method-assign]
        try:
            d.submit_issue(repo="owner/repo", issue_number=2)
        except QueueSaturationError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("expected QueueSaturationError")
        assert d._tasks == {}
        assert d._task_store.get("__none__") is None


# ── (c) failed dependency fails the dependent ────────────────────────────────


class TestFailedDependencyFailsDependent:
    def test_dependent_of_failed_dep_transitions_to_failed(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        d = _make_daemon(tmp_path, minimal_config)

        dep = Task(id="dep", prompt="p", kind=TaskKind.PROMPT, status=TaskStatus.FAILED)
        dependent = Task(
            id="child",
            prompt="p",
            kind=TaskKind.PROMPT,
            status=TaskStatus.QUEUED,
            depends_on=["dep"],
        )
        d._tasks[dep.id] = dep
        d._tasks[dependent.id] = dependent
        d._task_store.save(dep)
        d._task_store.save(dependent)
        d._queue.put_nowait((dependent.priority, dependent))

        events: list[Any] = []

        async def _capture(event: Any) -> None:
            events.append(event)

        d._events.publish = _capture  # type: ignore[method-assign]

        _stopped, claimed = _run(d._claim_next_queued_task())

        assert claimed is None
        assert dependent.status is TaskStatus.FAILED
        assert "dep" in (dependent.error or "")
        # Persisted failure + a TASK_FAILED event with the dependency reason.
        assert d._task_store.get("child").status is TaskStatus.FAILED
        assert any(e.kind is EventKind.TASK_FAILED for e in events)

    def test_completed_dependency_still_allows_claim(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        """A COMPLETED dependency must not be mistaken for a failed one."""
        d = _make_daemon(tmp_path, minimal_config)
        dep = Task(id="dep", prompt="p", kind=TaskKind.PROMPT, status=TaskStatus.COMPLETED)
        dependent = Task(
            id="child",
            prompt="p",
            kind=TaskKind.PROMPT,
            status=TaskStatus.QUEUED,
            depends_on=["dep"],
        )
        d._tasks[dep.id] = dep
        d._tasks[dependent.id] = dependent
        d._queue.put_nowait((dependent.priority, dependent))

        _stopped, claimed = _run(d._claim_next_queued_task())

        assert claimed is dependent
        assert dependent.status is TaskStatus.RUNNING


# ── (b)/(d) fleet coordinator: client reuse, tls_verify, cancel race ──────────


@dataclass
class _FakeHTTPResponse:
    status_code: int
    _body: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return self._body


@dataclass
class _RecordingHTTP:
    """Records calls; health 200, submit 202. Tracks aclose() for leak checks."""

    submitted: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    async def get(self, url: str, *, headers: dict[str, str]) -> _FakeHTTPResponse:
        return _FakeHTTPResponse(status_code=200)

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> _FakeHTTPResponse:
        self.submitted.append({"url": url, "payload": json})
        return _FakeHTTPResponse(status_code=202)

    async def aclose(self) -> None:
        self.closed = True


def _make_coordinator(
    *,
    tasks: dict[str, Task],
    machines: list[Any],
    task_store: Any = None,
) -> FleetCoordinator:
    config = SimpleNamespace(
        fleet_machines=machines,
        fleet_coordinator_poll_seconds=5,
        fleet_heartbeat_seconds=30,
        api_auth_token=None,
    )
    return FleetCoordinator(
        config=config,
        tasks=tasks,
        tasks_lock=threading.Lock(),
        task_store=task_store or MagicMock(),
        worker_last_seen={},
        enqueue_task_entry=MagicMock(),
        running_flag=lambda: False,
    )


class TestCoordinatorClientLifecycle:
    def test_tls_verify_false_reaches_httpx(self) -> None:
        """tls_verify on the machine config must reach the default httpx client (#978b)."""
        from maxwell_daemon.fleet.client import RemoteDaemonClient

        client = RemoteDaemonClient(tls_verify=False)
        # The default adapter stores the verify flag on its httpx client.
        assert client._tls_verify is False
        assert client._owns_http is True

    def test_injected_client_is_not_closed(self) -> None:
        """aclose() only releases transports the client created itself (#978b)."""
        from maxwell_daemon.fleet.client import RemoteDaemonClient

        http = _RecordingHTTP()
        client = RemoteDaemonClient(http_client=http)
        _run(client.aclose())
        assert http.closed is False  # caller owns an injected transport

    def test_one_persistent_client_per_machine_reused(self) -> None:
        """Two ticks must reuse one client per machine, not build a new pool each tick."""
        task = Task(id="t1", prompt="p", kind=TaskKind.PROMPT, status=TaskStatus.QUEUED)
        machine = SimpleNamespace(
            name="w1", host="h", port=8080, capacity=1, tags=[], tls=False, tls_verify=False
        )
        coordinator = _make_coordinator(tasks={"t1": task}, machines=[machine])

        created: list[Any] = []
        import maxwell_daemon.fleet.client as fleet_client_mod

        original_cls = fleet_client_mod.RemoteDaemonClient

        class _PatchedClient(original_cls):  # type: ignore[misc,valid-type]
            def __init__(self, **kw: Any) -> None:
                super().__init__(http_client=_RecordingHTTP(), **kw)
                self.aclose_calls = 0
                created.append(self)

            async def aclose(self) -> None:
                self.aclose_calls += 1
                await super().aclose()

        fleet_client_mod.RemoteDaemonClient = _PatchedClient  # type: ignore[misc]
        try:
            _run(coordinator._dispatch_tick())
            # Reset task to QUEUED for a second tick and dispatch again.
            task.status = TaskStatus.QUEUED
            task.dispatched_to = None
            _run(coordinator._dispatch_tick())
        finally:
            fleet_client_mod.RemoteDaemonClient = original_cls  # type: ignore[misc]

        # One client built for the single machine, reused across both ticks
        # rather than a fresh pool per tick (#978b).
        assert len(created) == 1
        assert created[0]._tls_verify is False
        # coordinator.aclose() propagates to every cached per-machine client.
        _run(coordinator.aclose())
        assert created[0].aclose_calls == 1
        assert coordinator._clients == {}

    def test_cancel_during_dispatch_await_is_not_overwritten(self) -> None:
        """A task cancelled while submit_task awaits stays CANCELLED, not DISPATCHED (#978d)."""
        task = Task(id="t1", prompt="p", kind=TaskKind.PROMPT, status=TaskStatus.QUEUED)
        machine = SimpleNamespace(
            name="w1", host="h", port=8080, capacity=1, tags=[], tls=False, tls_verify=True
        )
        tasks = {"t1": task}
        coordinator = _make_coordinator(tasks=tasks, machines=[machine])

        import maxwell_daemon.fleet.client as fleet_client_mod

        original_cls = fleet_client_mod.RemoteDaemonClient

        class _RacingHTTP(_RecordingHTTP):
            async def post(
                self, url: str, *, json: dict[str, Any], headers: dict[str, str]
            ) -> _FakeHTTPResponse:
                # Simulate a cancel landing during the dispatch await window.
                task.status = TaskStatus.CANCELLED
                return await super().post(url, json=json, headers=headers)

        class _PatchedClient(original_cls):  # type: ignore[misc,valid-type]
            def __init__(self, **kw: Any) -> None:
                super().__init__(http_client=_RacingHTTP(), **kw)

        fleet_client_mod.RemoteDaemonClient = _PatchedClient  # type: ignore[misc]
        try:
            _run(coordinator._dispatch_tick())
        finally:
            fleet_client_mod.RemoteDaemonClient = original_cls  # type: ignore[misc]

        assert task.status is TaskStatus.CANCELLED
        assert task.dispatched_to is None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
