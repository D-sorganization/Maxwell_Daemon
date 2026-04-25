"""Daemon concurrency — ``_tasks`` dict must survive parallel readers/writers.

Historically :meth:`Daemon.submit` mutated ``self._tasks`` without a lock while
:meth:`Daemon.state` iterated a snapshot via ``dict(self._tasks)``. Under
real concurrency that pattern raises
``RuntimeError: dictionary changed size during iteration``.

The test below fires dozens of submit() + state() calls across a thread pool
and asserts the dict never tears.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import (
    QueueSaturationError,
    Task,
    TaskKind,
    TaskStatus,
)


class _ThreadBoundQueue:
    def __init__(self, owner_thread: threading.Thread) -> None:
        self.owner_thread = owner_thread
        self.items: list[tuple[int, Task | None]] = []
        self.put_event = threading.Event()

    def full(self) -> bool:
        return False

    def put_nowait(self, item: tuple[int, Task | None]) -> None:
        if threading.current_thread() is not self.owner_thread:
            raise RuntimeError("queue mutation happened off the daemon loop thread")
        self.items.append(item)
        self.put_event.set()


class TestTasksDictThreadSafety:
    def test_enqueue_task_entry_puts_directly_before_daemon_loop_starts(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        task = Task(
            id="pre-start",
            prompt="queued",
            kind=TaskKind.PROMPT,
            priority=7,
            status=TaskStatus.QUEUED,
        )
        queue_stub: Any = _ThreadBoundQueue(threading.current_thread())
        d._queue = queue_stub
        d._enqueue_task_entry(task.priority, task)
        assert queue_stub.items == [(7, task)]

    def test_enqueue_task_entry_puts_directly_when_already_on_daemon_loop_thread(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        task = Task(
            id="same-loop",
            prompt="queued",
            kind=TaskKind.PROMPT,
            priority=42,
            status=TaskStatus.QUEUED,
        )
        queue_stub: Any = _ThreadBoundQueue(threading.current_thread())

        async def _exercise() -> None:
            d._loop = asyncio.get_running_loop()
            d._queue = queue_stub
            d._enqueue_task_entry(task.priority, task)

        asyncio.run(_exercise())
        assert queue_stub.items == [(42, task)]

    def test_submit_routes_queue_put_onto_daemon_loop_thread(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        stub: Any = MagicMock()
        d._task_store = stub

        loop = asyncio.new_event_loop()
        loop_ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop_ready.set()
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, name="daemon-loop", daemon=True)
        loop_thread.start()
        assert loop_ready.wait(timeout=2.0)

        queue_stub: Any = _ThreadBoundQueue(loop_thread)
        d._loop = loop
        d._queue = queue_stub

        try:
            task = d.submit("hi from foreign thread")
            assert queue_stub.put_event.wait(timeout=2.0)
            assert queue_stub.items == [(task.priority, task)]
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5.0)
            loop.close()

    def test_reprioritize_routes_queue_put_onto_daemon_loop_thread(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        stub: Any = MagicMock()
        d._task_store = stub

        loop = asyncio.new_event_loop()
        loop_ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop_ready.set()
            loop.run_forever()

        loop_thread = threading.Thread(target=_run_loop, name="daemon-loop", daemon=True)
        loop_thread.start()
        assert loop_ready.wait(timeout=2.0)

        queue_stub: Any = _ThreadBoundQueue(loop_thread)
        task = Task(
            id="reprio-me",
            prompt="queued",
            kind=TaskKind.PROMPT,
            priority=100,
            status=TaskStatus.QUEUED,
        )
        d._loop = loop
        d._queue = queue_stub
        d._tasks[task.id] = task

        try:
            updated = d.reprioritize_task(task.id, 5)
            assert queue_stub.put_event.wait(timeout=2.0)
            assert updated.priority == 5
            assert queue_stub.items == [(5, task)]
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=5.0)
            loop.close()

    def test_submit_before_daemon_loop_starts_queues_directly_and_skips_event(
        self, minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
    ) -> None:
        d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        stub: Any = MagicMock()
        d._task_store = stub
        queue_stub: Any = _ThreadBoundQueue(threading.current_thread())
        d._queue = queue_stub

        task = d.submit("pre-start submit")

        assert queue_stub.items == [(task.priority, task)]
        assert d._tasks[task.id] is task

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
            except QueueSaturationError:
                pass
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
                    with contextlib.suppress(QueueSaturationError):
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
