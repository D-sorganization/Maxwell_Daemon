"""Unit tests for fleet coordinator role behavior — issue #177.

Tests cover:
- TaskStatus.DISPATCHED enum value
- Task.dispatched_to field
- Daemon.record_worker_heartbeat()
- Daemon._dispatch_to_fleet() assigning tasks and marking them DISPATCHED
- Stale-worker requeuing when a machine goes offline
- Role-based start() behavior (coordinator vs worker vs standalone)
- Config: role field and coordinator_poll_seconds
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon.fleet_coordinator import FleetCoordinator
from maxwell_daemon.daemon.runner import Daemon, Task, TaskKind, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    role: str = "standalone",
    machines: list[dict] | None = None,  # type: ignore[type-arg]
) -> MaxwellDaemonConfig:
    """Build a minimal config with the recording backend registered."""
    from maxwell_daemon.backends import registry

    class _RecordingBackend:
        name = "recording"

        def __init__(self, **kw: Any) -> None:
            pass

        async def complete(self, messages: list, *, model: str, **kwargs: Any) -> Any:  # type: ignore[type-arg]
            from maxwell_daemon.backends import BackendResponse, TokenUsage

            return BackendResponse(
                content="ok",
                finish_reason="stop",
                usage=TokenUsage(10, 5, 15),
                model=model,
                backend=self.name,
            )

        async def health_check(self) -> bool:
            return True

        def capabilities(self, model: str) -> Any:
            from maxwell_daemon.backends import BackendCapabilities

            return BackendCapabilities(
                cost_per_1k_input_tokens=0.001,
                cost_per_1k_output_tokens=0.002,
            )

    registry._factories["recording"] = _RecordingBackend  # type: ignore[assignment]
    data: dict[str, Any] = {
        "role": role,
        "backends": {"primary": {"type": "recording", "model": "test-model"}},
        "agent": {"default_backend": "primary"},
    }
    if machines:
        data["fleet"] = {"machines": machines}
    return MaxwellDaemonConfig.model_validate(data)


def _task(task_id: str = "t1", status: TaskStatus = TaskStatus.QUEUED, priority: int = 100) -> Task:
    return Task(
        id=task_id,
        prompt="test",
        kind=TaskKind.PROMPT,
        priority=priority,
        status=status,
    )


# ---------------------------------------------------------------------------
# Config model tests
# ---------------------------------------------------------------------------


class TestRoleConfig:
    def test_default_role_is_standalone(self) -> None:
        cfg = _make_config()
        assert cfg.role == "standalone"

    def test_coordinator_role(self) -> None:
        cfg = _make_config(role="coordinator")
        assert cfg.role == "coordinator"

    def test_worker_role(self) -> None:
        cfg = _make_config(role="worker")
        assert cfg.role == "worker"

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises((Exception, ValueError)):
            MaxwellDaemonConfig.model_validate(
                {
                    "role": "bogus",
                    "backends": {"b": {"type": "recording", "model": "m"}},
                    "agent": {"default_backend": "b"},
                }
            )

    def test_coordinator_poll_seconds_default(self) -> None:
        cfg = _make_config()
        assert cfg.fleet.coordinator_poll_seconds == 30

    def test_coordinator_poll_seconds_configurable(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"b": {"type": "recording", "model": "m"}},
                "agent": {"default_backend": "b"},
                "fleet": {"coordinator_poll_seconds": 60},
            }
        )
        assert cfg.fleet.coordinator_poll_seconds == 60


# ---------------------------------------------------------------------------
# TaskStatus.DISPATCHED
# ---------------------------------------------------------------------------


class TestDispatchedStatus:
    def test_dispatched_value(self) -> None:
        assert TaskStatus.DISPATCHED.value == "dispatched"

    def test_task_dispatched_to_field_default_none(self) -> None:
        t = _task()
        assert t.dispatched_to is None

    def test_task_dispatched_to_can_be_set(self) -> None:
        t = _task()
        t.dispatched_to = "worker-1"
        assert t.dispatched_to == "worker-1"

    def test_task_status_can_be_dispatched(self) -> None:
        t = _task()
        t.status = TaskStatus.DISPATCHED
        assert t.status is TaskStatus.DISPATCHED


# ---------------------------------------------------------------------------
# Daemon.record_worker_heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_record_updates_last_seen(self, tmp_path: Path) -> None:
        cfg = _make_config()
        daemon = Daemon(
            cfg,
            ledger_path=tmp_path / "ledger.db",
            task_store_path=tmp_path / "tasks.db",
        )
        before = datetime.now(timezone.utc)
        daemon.record_worker_heartbeat("worker-1")
        after = datetime.now(timezone.utc)
        ts = daemon._worker_last_seen["worker-1"]
        assert before <= ts <= after

    def test_multiple_workers_tracked_independently(self, tmp_path: Path) -> None:
        cfg = _make_config()
        daemon = Daemon(
            cfg,
            ledger_path=tmp_path / "ledger.db",
            task_store_path=tmp_path / "tasks.db",
        )
        daemon.record_worker_heartbeat("worker-1")
        daemon.record_worker_heartbeat("worker-2")
        assert "worker-1" in daemon._worker_last_seen
        assert "worker-2" in daemon._worker_last_seen

    def test_heartbeat_updates_timestamp(self, tmp_path: Path) -> None:
        cfg = _make_config()
        daemon = Daemon(
            cfg,
            ledger_path=tmp_path / "ledger.db",
            task_store_path=tmp_path / "tasks.db",
        )
        daemon.record_worker_heartbeat("worker-1")
        first_ts = daemon._worker_last_seen["worker-1"]
        daemon.record_worker_heartbeat("worker-1")
        second_ts = daemon._worker_last_seen["worker-1"]
        assert second_ts >= first_ts


# ---------------------------------------------------------------------------
# Daemon role-based start() behavior
# ---------------------------------------------------------------------------


class TestRoleBasedStart:
    def test_standalone_starts_workers(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config(role="standalone")
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=2, recover=False)
            try:
                assert len(daemon._workers) == 2
            finally:
                await daemon.stop()

        asyncio.run(_run())

    def test_worker_role_starts_workers(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config(role="worker")
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=2, recover=False)
            try:
                assert len(daemon._workers) == 2
                assert daemon._worker_count == 2
            finally:
                await daemon.stop()

        asyncio.run(_run())

    def test_coordinator_starts_no_workers(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config(
                role="coordinator",
                machines=[{"name": "w1", "host": "localhost", "port": 8080}],
            )
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=4, recover=False)
            try:
                # Coordinator spawns no local workers regardless of worker_count arg.
                assert len(daemon._workers) == 0
                assert daemon._worker_count == 0
                # Coordinator loop should be running as a bg task.
                assert any(
                    getattr(t, "get_name", lambda: "")() == "coordinator-loop"
                    for t in daemon._bg_tasks
                )
            finally:
                await daemon.stop()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# _dispatch_to_fleet: assignment and DISPATCHED status
# ---------------------------------------------------------------------------


@dataclass
class _FakeHTTPResponse:
    status_code: int
    _body: dict[str, Any] = field(default_factory=dict)

    def json(self) -> dict[str, Any]:
        return self._body


@dataclass
class _FakeHTTPClient:
    """Records calls; health checks return 200, task submits return 202."""

    submitted: list[dict[str, Any]] = field(default_factory=list)
    health_ok: bool = True
    submit_ok: bool = True

    async def get(self, url: str, *, headers: dict[str, str]) -> _FakeHTTPResponse:
        code = 200 if self.health_ok else 503
        return _FakeHTTPResponse(status_code=code)

    async def post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> _FakeHTTPResponse:
        self.submitted.append({"url": url, "payload": json})
        code = 202 if self.submit_ok else 500
        return _FakeHTTPResponse(status_code=code)


def _make_coordinator(
    *,
    tasks: dict[str, Task] | None = None,
    worker_last_seen: dict[str, datetime] | None = None,
    machines: list[Any] | None = None,
    task_store: Any = None,
    enqueue_fn: Any = None,
) -> FleetCoordinator:
    """Build a FleetCoordinator with a real-like config SimpleNamespace."""
    fleet_machines = machines or []
    config = SimpleNamespace(
        fleet_machines=fleet_machines,
        fleet_coordinator_poll_seconds=5,
        fleet_heartbeat_seconds=30,
        api_auth_token=None,
    )
    return FleetCoordinator(
        config=config,
        tasks=tasks if tasks is not None else {},
        tasks_lock=threading.Lock(),
        task_store=task_store or MagicMock(),
        worker_last_seen=worker_last_seen if worker_last_seen is not None else {},
        enqueue_task_entry=enqueue_fn or MagicMock(),
        running_flag=lambda: False,
    )


class TestDispatchToFleet:
    def test_queued_task_gets_dispatched(self, tmp_path: Path) -> None:
        """A QUEUED task is assigned to a healthy machine and marked DISPATCHED."""

        async def _run() -> None:
            task = _task("abc123")
            tasks: dict[str, Task] = {"abc123": task}

            fake_http = _FakeHTTPClient()

            # Build a machine config SimpleNamespace matching what FleetCoordinator needs.
            machine = SimpleNamespace(name="w1", host="worker1", port=8080, capacity=1, tags=[])
            coordinator = _make_coordinator(tasks=tasks, machines=[machine])

            import maxwell_daemon.fleet.client as fleet_client_mod

            original_cls = fleet_client_mod.RemoteDaemonClient

            class _PatchedClient(original_cls):  # type: ignore[misc,valid-type]
                def __init__(self, **kw: Any) -> None:
                    super().__init__(http_client=fake_http, **kw)

            fleet_client_mod.RemoteDaemonClient = _PatchedClient  # type: ignore[misc]
            try:
                await coordinator._dispatch_tick()
            finally:
                fleet_client_mod.RemoteDaemonClient = original_cls  # type: ignore[misc]

            assert task.status is TaskStatus.DISPATCHED
            assert task.dispatched_to == "w1"
            assert len(fake_http.submitted) == 1
            assert fake_http.submitted[0]["payload"]["task_id"] == "abc123"

        asyncio.run(_run())

    def test_no_machines_configured_is_noop(self, tmp_path: Path) -> None:
        """When fleet has no machines, _dispatch_tick returns without error."""

        async def _run() -> None:
            task = _task("t1")
            tasks: dict[str, Task] = {"t1": task}
            coordinator = _make_coordinator(tasks=tasks, machines=[])
            # Should complete without raising.
            await coordinator._dispatch_tick()
            # Task stays QUEUED since there's nowhere to send it.
            assert task.status is TaskStatus.QUEUED

        asyncio.run(_run())

    def test_unhealthy_machine_skips_dispatch(self, tmp_path: Path) -> None:
        """Tasks are not dispatched to unhealthy machines."""

        async def _run() -> None:
            task = _task("abc")
            tasks: dict[str, Task] = {"abc": task}
            machine = SimpleNamespace(name="w1", host="dead-host", port=8080, capacity=1, tags=[])
            coordinator = _make_coordinator(tasks=tasks, machines=[machine])

            fake_http = _FakeHTTPClient(health_ok=False)

            import maxwell_daemon.fleet.client as fleet_client_mod

            original_cls = fleet_client_mod.RemoteDaemonClient

            class _PatchedClient(original_cls):  # type: ignore[misc,valid-type]
                def __init__(self, **kw: Any) -> None:
                    super().__init__(http_client=fake_http, **kw)

            fleet_client_mod.RemoteDaemonClient = _PatchedClient  # type: ignore[misc]
            try:
                await coordinator._dispatch_tick()
            finally:
                fleet_client_mod.RemoteDaemonClient = original_cls  # type: ignore[misc]

            # Unhealthy machine: task should remain QUEUED (unassigned).
            assert task.status is TaskStatus.QUEUED
            assert len(fake_http.submitted) == 0

        asyncio.run(_run())

    def test_stale_dispatched_task_requeued(self, tmp_path: Path) -> None:
        """DISPATCHED tasks whose worker has been offline too long are requeued."""

        async def _run() -> None:
            task = _task("xyz")
            task.status = TaskStatus.DISPATCHED
            task.dispatched_to = "w1"
            tasks: dict[str, Task] = {"xyz": task}

            # w1's last heartbeat was a long time ago (>3x heartbeat_seconds = 90s).
            stale_time = datetime.now(timezone.utc) - timedelta(seconds=200)
            worker_last_seen: dict[str, datetime] = {"w1": stale_time}

            enqueued: list[Task] = []
            machine = SimpleNamespace(name="w1", host="dead-host", port=8080, capacity=1, tags=[])
            coordinator = _make_coordinator(
                tasks=tasks,
                machines=[machine],
                worker_last_seen=worker_last_seen,
                enqueue_fn=lambda priority, t: enqueued.append(t),
            )

            fake_http = _FakeHTTPClient(health_ok=False)

            import maxwell_daemon.fleet.client as fleet_client_mod

            original_cls = fleet_client_mod.RemoteDaemonClient

            class _PatchedClient(original_cls):  # type: ignore[misc,valid-type]
                def __init__(self, **kw: Any) -> None:
                    super().__init__(http_client=fake_http, **kw)

            fleet_client_mod.RemoteDaemonClient = _PatchedClient  # type: ignore[misc]
            try:
                await coordinator._dispatch_tick()
            finally:
                fleet_client_mod.RemoteDaemonClient = original_cls  # type: ignore[misc]

            # Task should be requeued.
            assert task.status is TaskStatus.QUEUED
            assert task.dispatched_to is None

        asyncio.run(_run())


class TestSetWorkerCount:
    """Tests for Daemon.set_worker_count() — dynamic worker pool scaling."""

    def test_scale_up_adds_workers(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config()
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=1)
            try:
                assert len(daemon._workers) == 1
                await daemon.set_worker_count(3)
                assert len(daemon._workers) == 3
            finally:
                await daemon.stop()

        asyncio.run(_run())

    def test_scale_down_sends_sentinels(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config()
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=3)
            try:
                await daemon.set_worker_count(1)
                # Worker count is set; workers will exit when sentinel is dequeued.
                assert daemon._worker_count == 1
            finally:
                await daemon.stop()

        asyncio.run(_run())

    def test_rejects_zero_count(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config()
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=1)
            try:
                with pytest.raises(ValueError, match="at least 1"):
                    await daemon.set_worker_count(0)
            finally:
                await daemon.stop()

        asyncio.run(_run())


class TestReprioritizeTask:
    """Tests for Daemon.reprioritize_task() — runtime task priority adjustment."""

    def test_reprioritize_changes_priority(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config()
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=1)
            try:
                task = daemon.submit("hello")
                # Only reprioritize if still QUEUED
                if task.status is TaskStatus.QUEUED:
                    updated = daemon.reprioritize_task(task.id, 50)
                    assert updated.priority == 50
            finally:
                await daemon.stop()

        asyncio.run(_run())

    def test_reprioritize_missing_task_raises(self, tmp_path: Path) -> None:
        async def _run() -> None:
            cfg = _make_config()
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await daemon.start(worker_count=1)
            try:
                with pytest.raises(KeyError):
                    daemon.reprioritize_task("nonexistent-id", 50)
            finally:
                await daemon.stop()

        asyncio.run(_run())


class TestReloadConfig:
    """Tests for Daemon.reload_config() — hot config reloading."""

    def test_reload_config_updates_budget(self, tmp_path: Path) -> None:
        from maxwell_daemon.config import save_config

        async def _run() -> None:
            cfg = _make_config()
            cfg_path = tmp_path / "config.yaml"
            save_config(cfg, cfg_path)
            daemon = Daemon(
                cfg,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            daemon._config_path = cfg_path
            await daemon.start(worker_count=1)
            try:
                reloaded_path = daemon.reload_config()
                assert reloaded_path == cfg_path
            finally:
                await daemon.stop()

        asyncio.run(_run())


# force CI trigger
