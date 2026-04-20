"""Daemon concurrency — ``_tasks`` dict must survive parallel readers/writers.

Historically :meth:`Daemon.submit` mutated ``self._tasks`` without a lock while
:meth:`Daemon.state` iterated a snapshot via ``dict(self._tasks)``. Under
real concurrency that pattern raises
``RuntimeError: dictionary changed size during iteration``.

The test below fires dozens of submit() + state() calls across a thread pool
and asserts the dict never tears.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

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
