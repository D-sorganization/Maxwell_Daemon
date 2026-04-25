"""task_store and event-bus failures in the task execution path.

The daemon should *log* task_store exceptions rather than silently drop
writes.  A failing TASK_STARTED event publish must route through the existing
failure-handling path so the task is finalised (finished_at, FAILED status)
and the worker coroutine stays alive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskStatus

T = TypeVar("T")


def _run(coro: Awaitable[T]) -> T:
    return asyncio.run(coro)  # type: ignore[arg-type]


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

    def get(self, _id: str) -> Any:
        return None

    def delete(self, _id: str) -> None:
        pass

    async def aprune(self, _days: int) -> int:
        return 0


async def _wait_for_status(
    daemon: Daemon, task_id: str, expected: TaskStatus, timeout: float = 10.0
) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status is expected:
            return
        await asyncio.sleep(0.01)
    task = daemon.get_task(task_id)
    if task and task.status is expected:
        return
    raise AssertionError(f"task {task_id} did not reach {expected}")


class TestTaskStoreErrorLogging:
    def test_failing_save_is_logged_not_silently_swallowed(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        store = _FailingSaveStore()

        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            d._task_store = store  # type: ignore[assignment]
            await d.start(worker_count=1)
            try:
                task = d.submit("hi")
                store.armed = True
                await _wait_for_status(d, task.id, TaskStatus.COMPLETED, timeout=10.0)
            finally:
                await d.stop()

        _run(body())

        # The fix replaces suppress(Exception) with an explicit log.exception.
        captured = capsys.readouterr()
        assert (
            "task store write failed" in captured.out or "task store write failed" in captured.err
        )
        # The failure is an *exception* log (with traceback), not a plain error.
        assert "Traceback" in captured.out or "Traceback" in captured.err


class TestEventPublishFailure:
    """Issue #238 — a failing TASK_STARTED publish must not leave the task in RUNNING.

    Before the fix, ``await self._events.publish(TASK_STARTED)`` lived outside
    the ``try/except/finally`` block in ``_execute()``.  A transient failure
    there would propagate uncaught through ``_worker_loop``, crashing that
    worker coroutine and leaving the task stuck in RUNNING with no
    ``finished_at``.  After the fix the publish is inside the try block so
    the except/finally handlers always run.
    """

    def test_event_publish_failure_finalises_task_as_failed(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
    ) -> None:
        async def body() -> None:
            d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
            await d.start(worker_count=1)
            try:
                # Patch the event bus publish to raise on TASK_STARTED.
                # Submit after patching so the TASK_QUEUED publish is also caught,
                # but we only fail the first call (TASK_STARTED during _execute).
                original_publish = d._events.publish
                call_count = 0

                async def _failing_publish(event: Any) -> None:
                    nonlocal call_count
                    call_count += 1
                    if event.kind.name == "TASK_STARTED":
                        raise RuntimeError("simulated event bus failure")
                    await original_publish(event)

                d._events.publish = _failing_publish  # type: ignore[method-assign]
                task = d.submit("test-prompt")
                await _wait_for_status(d, task.id, TaskStatus.FAILED, timeout=10.0)
                t = d.get_task(task.id)
                assert t is not None
                assert t.status is TaskStatus.FAILED
                assert t.finished_at is not None, (
                    "task must have finished_at set even on publish failure"
                )
            finally:
                await d.stop()

        _run(body())
