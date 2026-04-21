"""Daemon concurrency — ``_tasks`` dict must survive parallel readers/writers.

Historically :meth:`Daemon.submit` mutated ``self._tasks`` without a lock while
:meth:`Daemon.state` iterated a snapshot via ``dict(self._tasks)``. Under
real concurrency that pattern raises
``RuntimeError: dictionary changed size during iteration``.

The test below fires dozens of submit() + state() calls across a thread pool
and asserts the dict never tears.

A second test verifies that tasks submitted from a foreign thread via
``_queue_task_threadsafe`` are reliably received by async workers (issue #164).
``asyncio.Queue.put_nowait`` is not thread-safe; the fix uses
``loop.call_soon_threadsafe`` so sleeping workers are woken correctly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


class TestTasksDictThreadSafety:
    def test_concurrent_submit_and_state_do_not_tear_tasks_dict(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        # Swap the TaskStore for a fast in-memory stub so the SQLite serial
        # point doesn't mask the race — the bug is about ``self._tasks`` not
        # ``self._task_store``.
        stub: Any = MagicMock()
        d._task_store = stub
        errors: list[BaseException] = []

        def _submit(_: int) -> None:
            try:
                d.submit("hi")
            except BaseException as e:
                errors.append(e)

        def _read(_: int) -> None:
            try:
                # state() copies self._tasks — the bug surfaces here when a
                # concurrent submit() mutates the dict mid-copy.
                d.state()
            except BaseException as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            # Interleave the two call types so readers see the writer mid-flight.
            futures: list[concurrent.futures.Future[None]] = []
            for i in range(50):
                futures.append(pool.submit(_submit, i))
                futures.append(pool.submit(_read, i))
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

        assert not errors, f"unexpected errors under contention: {errors[:3]}"
        # Every submit() landed.
        assert len(d._tasks) == 50

    def test_state_iteration_survives_concurrent_mutation(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """Tighter race test — hammer ``state()`` while writers mutate _tasks.

        Also iterates ``state().tasks`` after the copy: a torn snapshot can
        slip through the ``dict()`` call on some CPython versions under heavy
        contention. We do lots of reads to maximise the chance of catching it.
        """
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        stub: Any = MagicMock()
        d._task_store = stub
        errors: list[BaseException] = []
        stop = False

        def _writer() -> None:
            nonlocal stop
            try:
                while not stop:
                    d.submit("hi")
            except BaseException as e:
                errors.append(e)

        def _reader() -> None:
            try:
                for _ in range(500):
                    snap = d.state()
                    # Force full iteration — catches torn snapshots.
                    for _tid, _task in snap.tasks.items():
                        pass
            except BaseException as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            writers = [pool.submit(_writer) for _ in range(4)]
            readers = [pool.submit(_reader) for _ in range(4)]
            for fut in concurrent.futures.as_completed(readers):
                fut.result()
            stop = True
            for fut in concurrent.futures.as_completed(writers):
                fut.result()

        assert not errors, f"torn dict under contention: {errors[:3]}"


class TestCrossThreadQueueSubmit:
    """Issue #164 — submit() from a non-event-loop thread must reliably wake workers.

    ``asyncio.Queue.put_nowait`` does not wake sleeping ``await queue.get()``
    calls when invoked from a foreign OS thread.  The fix routes the put
    through ``loop.call_soon_threadsafe`` so the event loop's internal
    selector/condition is notified correctly.
    """

    @pytest.mark.asyncio
    async def test_tasks_submitted_from_thread_are_received_by_async_worker(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        """Tasks pushed into the queue from a foreign thread must be dequeued."""
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        stub: Any = MagicMock()
        d._task_store = stub

        received: list[str] = []
        loop = asyncio.get_running_loop()

        async def _drain(n: int) -> None:
            """Read exactly n items out of the queue."""
            for _ in range(n):
                task = await asyncio.wait_for(d._queue.get(), timeout=5.0)
                received.append(task.id)

        from maxwell_daemon.daemon.runner import Task

        task_count = 20
        drain_task = loop.create_task(_drain(task_count))

        # Submit all tasks from a thread that is NOT the event-loop thread.
        # Before the fix, put_nowait() from a foreign thread would not reliably
        # wake sleeping ``await queue.get()`` calls; the drain_task would stall.
        def _push_tasks() -> None:
            for i in range(task_count):
                task = Task(id=f"t{i:04d}", prompt="cross-thread")
                d._queue_task_threadsafe(task)

        t = threading.Thread(target=_push_tasks)
        t.start()
        t.join(timeout=5.0)
        assert not t.is_alive(), "submitter thread hung"

        await drain_task  # will raise TimeoutError if workers weren't woken
        assert len(received) == task_count, f"expected {task_count} tasks, got {len(received)}"
