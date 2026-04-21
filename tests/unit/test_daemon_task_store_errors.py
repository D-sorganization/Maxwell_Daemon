"""Silent ``task_store`` failures used to be swallowed by ``suppress(Exception)``.

The daemon should *log* an exception (so operators see DB corruption in
Loki/Grafana) rather than silently drop the write. The task itself still
completes from the worker's point of view.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskStatus

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)


class _FailingSaveStore:
    """task_store stub that starts benign then flips to raise on save().

    submit() itself calls save(); we need those initial writes to succeed so
    the queued task reaches the worker. Once ``.armed = True`` the subsequent
    save() in ``_execute``'s finally block raises and exercises the logger.
    """

    def __init__(self) -> None:
        self.save_calls = 0
        self.update_status_calls = 0
        self.armed = False

    def save(self, task: Any) -> None:
        self.save_calls += 1
        if self.armed:
            raise RuntimeError("simulated disk-full on save")

    def update_status(self, *_a: Any, **_kw: Any) -> None:
        self.update_status_calls += 1

    def recover_pending(self) -> list[Any]:
        return []


async def _wait_for_status(
    daemon: Daemon, task_id: str, expected: TaskStatus, timeout: float = 10.0
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status is expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"task {task_id} did not reach {expected}")


class TestTaskStoreErrorLogging:
    def test_failing_save_is_logged_not_silently_swallowed(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        store = _FailingSaveStore()

        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            d._task_store = store  # type: ignore[assignment]
            await d.start(worker_count=1)
            try:
                task = d.submit("hi")
                # Arm the failing save *after* submit so the worker's save in
                # ``_execute``'s finally block is the one that raises.
                store.armed = True
                # Task still completes in-memory even if the DB write fails.
                await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
            finally:
                await d.stop()

        with caplog.at_level(logging.ERROR, logger="maxwell_daemon.daemon"):
            _run(body())

        # The fix replaces suppress(Exception) with an explicit log.exception.
        matched = [r for r in caplog.records if "task store write failed" in r.getMessage()]
        assert matched, (
            "expected 'task store write failed' log.exception; "
            f"records={[r.getMessage() for r in caplog.records]}"
        )
        # The failure is an *exception* log (with traceback), not a plain error.
        assert matched[0].exc_info is not None
