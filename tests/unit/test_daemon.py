"""Daemon runner — task lifecycle, worker pool, cost recording.

Uses plain ``asyncio.run`` rather than pytest-asyncio so the suite runs in
minimal environments.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

from maxwell_daemon.backends import registry
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskStatus

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


async def _wait_for_status(
    daemon: Daemon, task_id: str, expected: TaskStatus, timeout: float = 10.0
) -> Task:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status == expected:
            return task
        await asyncio.sleep(0.02)
    task = daemon.get_task(task_id)
    if task and task.status == expected:
        return task
    raise AssertionError(
        f"task {task_id} did not reach {expected}; final={task.status if task else None}"
    )


async def _with_daemon(
    config: MaxwellDaemonConfig,
    ledger_path: Path,
    *,
    worker_count: int,
    body: Callable[[Daemon], Awaitable[T]],
) -> T:
    d = Daemon(
        config, ledger_path=ledger_path, task_store_path=ledger_path.with_suffix(".tasks.db")
    )
    await d.start(worker_count=worker_count)
    try:
        return await body(d)
    finally:
        await d.stop()


class TestLifecycle:
    def test_start_spawns_workers(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            assert len(d._workers) == 3

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=3, body=body))

    def test_stop_cancels_workers(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            await d.start(worker_count=2)
            await d.stop()
            assert len(d._workers) == 0

        _run(body())

    def test_double_start_is_idempotent(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            await d.start(worker_count=5)
            assert len(d._workers) == 2

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=2, body=body))


class TestTaskExecution:
    def test_queued_task_transitions_to_completed(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            task = d.submit("hello")
            assert task.status == TaskStatus.QUEUED
            final = await _wait_for_status(d, task.id, TaskStatus.COMPLETED)
            assert final.result == "ok"
            assert final.cost_usd > 0
            assert final.started_at is not None
            assert final.finished_at is not None

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))

    def test_failed_task_records_error(
        self, isolated_ledger_path: Path, register_recording_backend: None
    ) -> None:
        from tests.conftest import RecordingBackend

        class ExplodingBackend(RecordingBackend):
            def __init__(self, **kw: Any) -> None:
                super().__init__(raise_on_complete=RuntimeError("nope"), **kw)

        registry._factories["exploding"] = ExplodingBackend
        try:
            cfg = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"bad": {"type": "exploding", "model": "x"}},
                    "agent": {"default_backend": "bad"},
                }
            )

            async def body(d: Daemon) -> None:
                task = d.submit("hi")
                final = await _wait_for_status(d, task.id, TaskStatus.FAILED)
                assert final.error is not None
                assert "nope" in final.error

            _run(_with_daemon(cfg, isolated_ledger_path, worker_count=1, body=body))
        finally:
            registry._factories.pop("exploding", None)

    def test_multiple_workers_process_concurrently(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            tasks = [d.submit(f"t{i}") for i in range(8)]
            for t in tasks:
                await _wait_for_status(d, t.id, TaskStatus.COMPLETED)
            assert all(d.get_task(t.id).status == TaskStatus.COMPLETED for t in tasks)

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=4, body=body))

    def test_cost_is_recorded_in_ledger(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            task = d.submit("hi")
            await _wait_for_status(d, task.id, TaskStatus.COMPLETED)
            assert d._ledger.month_to_date() > 0

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))


class TestState:
    def test_state_exposes_backends(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        async def body(d: Daemon) -> None:
            state = d.state()
            assert "primary" in state.backends_available

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))

    def test_state_version_from_package_metadata(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """state().version should come from importlib.metadata, not be hardcoded."""
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as pkg_version

        import maxwell_daemon

        try:
            expected = pkg_version("maxwell-daemon")
        except PackageNotFoundError:
            expected = "unknown"

        # The module-level __version__ must match what importlib.metadata reports.
        assert maxwell_daemon.__version__ == expected

        async def body(d: Daemon) -> None:
            assert d.state().version == expected
            assert d.state().version == maxwell_daemon.__version__

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))

    def test_from_config_path_roundtrip(
        self,
        tmp_path: Path,
        minimal_config: MaxwellDaemonConfig,
    ) -> None:
        from maxwell_daemon.config import save_config

        cfg_path = tmp_path / "c.yaml"
        save_config(minimal_config, cfg_path)
        d = Daemon.from_config_path(cfg_path)
        assert "primary" in d.state().backends_available


class TestRunningStatusResilience:
    """Tests for re-queuing when RUNNING status update fails (#142)."""

    def test_task_requeued_when_update_status_running_fails(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """When update_status(RUNNING) raises on first call, task is re-queued and retried."""

        class _PartiallyFailingStore:
            def __init__(self) -> None:
                self.running_call_count = 0

            def save(self, _task: Any) -> None:
                pass

            def update_status(self, task_id: Any, status: Any, **_kw: Any) -> None:
                if status is TaskStatus.RUNNING:
                    self.running_call_count += 1
                    if self.running_call_count == 1:
                        raise RuntimeError("simulated lock contention")

            def recover_pending(self) -> list[Any]:
                return []

        store = _PartiallyFailingStore()

        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            d._task_store = store  # type: ignore[assignment]
            await d.start(worker_count=1)
            try:
                task = d.submit("hi")
                # First update_status(RUNNING) raises -> task re-queued.
                # Second attempt succeeds -> task eventually completes.
                final = await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
                assert final.status == TaskStatus.COMPLETED
                # Must have been called at least twice (one fail, one success).
                assert store.running_call_count >= 2
            finally:
                await d.stop()

        _run(body())

    def test_task_not_lost_when_update_status_running_raises(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """Task remains accessible after a failed RUNNING update (not silently dropped)."""

        class _OnceFailStore:
            def __init__(self) -> None:
                self._failed = False

            def save(self, _task: Any) -> None:
                pass

            def update_status(self, task_id: Any, status: Any, **_kw: Any) -> None:
                if not self._failed and status is TaskStatus.RUNNING:
                    self._failed = True
                    raise RuntimeError("transient error")

            def recover_pending(self) -> list[Any]:
                return []

        store = _OnceFailStore()

        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            d._task_store = store  # type: ignore[assignment]
            await d.start(worker_count=1)
            try:
                task = d.submit("check not lost")
                await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
                # Task is still registered in the daemon dict after completion.
                assert d.get_task(task.id) is not None
                assert d.get_task(task.id).status == TaskStatus.COMPLETED
            finally:
                await d.stop()

        _run(body())

    def test_requeue_error_is_logged(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """A failed RUNNING status update is logged at ERROR level."""
        import logging

        import pytest

        class _FailFirstStore:
            def __init__(self) -> None:
                self._failed = False

            def save(self, _task: Any) -> None:
                pass

            def update_status(self, task_id: Any, status: Any, **_kw: Any) -> None:
                if not self._failed and status is TaskStatus.RUNNING:
                    self._failed = True
                    raise RuntimeError("disk full")

            def recover_pending(self) -> list[Any]:
                return []

        store = _FailFirstStore()

        async def body(caplog: pytest.LogCaptureFixture) -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            d._task_store = store  # type: ignore[assignment]
            await d.start(worker_count=1)
            try:
                with caplog.at_level(logging.ERROR, logger="maxwell_daemon.daemon"):
                    task = d.submit("log test")
                    await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
                matched = [r for r in caplog.records if "re-queuing" in r.getMessage()]
                assert matched, "expected re-queuing log message"
            finally:
                await d.stop()

        # Run inline with a fixture-free caplog substitute
        import io

        handler = logging.StreamHandler(io.StringIO())
        handler.setLevel(logging.ERROR)
        logger = logging.getLogger("maxwell_daemon.daemon")
        logger.addHandler(handler)
        try:

            async def run() -> None:
                d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
                d._task_store = store  # type: ignore[assignment]
                await d.start(worker_count=1)
                try:
                    task = d.submit("log test")
                    await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
                finally:
                    await d.stop()

            _run(run())
        finally:
            logger.removeHandler(handler)
        output = handler.stream.getvalue()
        assert "re-queuing" in output


class TestSubmitThreadsafe:
    """Tests for Daemon.submit_threadsafe() — cross-thread task submission (#164)."""

    def test_submit_threadsafe_before_start_raises(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """submit_threadsafe() raises RuntimeError when daemon is not started."""
        import pytest

        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        with pytest.raises(RuntimeError, match="daemon must be started"):
            d.submit_threadsafe("hello")

    def test_loop_is_none_before_start(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """_loop is None before start() is called."""
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        assert d._loop is None

    def test_loop_captured_after_start(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """_loop is set to the running event loop after start()."""

        async def body(d: Daemon) -> None:
            assert d._loop is not None
            assert d._loop is asyncio.get_running_loop()

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))

    def test_submit_threadsafe_from_background_thread_enqueues_task(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """submit_threadsafe() called from a background thread successfully enqueues a task.

        The background thread is run via asyncio.to_thread so the event loop
        remains free to service the coroutine scheduled by run_coroutine_threadsafe.
        """
        result: dict[str, Any] = {}

        async def body(d: Daemon) -> None:
            def background() -> Any:
                # Runs in a worker thread; event loop stays free.
                return d.submit_threadsafe("hello from thread")

            task = await asyncio.to_thread(background)
            result["task"] = task
            assert d.get_task(task.id) is task
            final = await _wait_for_status(d, task.id, TaskStatus.COMPLETED)
            assert final.result == "ok"

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))
        assert result["task"] is not None

    def test_submit_threadsafe_task_is_processed(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """submit_threadsafe() tasks are dequeued and executed by workers."""

        async def body(d: Daemon) -> None:
            def background() -> Any:
                return d.submit_threadsafe("process me")

            task = await asyncio.to_thread(background)
            final = await _wait_for_status(d, task.id, TaskStatus.COMPLETED)
            assert final.status == TaskStatus.COMPLETED

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))

    def test_submit_threadsafe_returns_task_object(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """submit_threadsafe() returns a Task instance with the submitted prompt."""

        async def body(d: Daemon) -> None:
            def background() -> Any:
                return d.submit_threadsafe("my prompt")

            task = await asyncio.to_thread(background)
            assert isinstance(task, Task)
            assert task.prompt == "my prompt"
            assert task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED)
            await _wait_for_status(d, task.id, TaskStatus.COMPLETED)

        _run(_with_daemon(minimal_config, isolated_ledger_path, worker_count=1, body=body))
