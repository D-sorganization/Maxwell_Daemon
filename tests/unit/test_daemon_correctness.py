"""Daemon correctness regressions: stall-retry policy, queue-saturation
visibility, and the single-instance storage guard (#971 #972 #975)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.single_instance import InstanceLockError
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


# ── #971: stall-retry gated on RetryPolicy ───────────────────────────────────


class TestStallRetryPolicy:
    def test_side_effects_started_is_not_auto_retried(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        d = _make_daemon(tmp_path, minimal_config)
        task = Task(id="t1", prompt="p", kind=TaskKind.PROMPT, side_effects_started=True)
        d._tasks[task.id] = task
        d._task_store.save(task)

        _run(d._handle_stalled_task(task))

        # Permanently FAILED, never re-queued.
        assert task.status is TaskStatus.FAILED
        assert task.retry_count == 0
        assert d._queue.empty()

    def test_exhausted_retry_budget_fails_permanently(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        d = _make_daemon(tmp_path, minimal_config)
        # Already at the policy ceiling (default max_retries=3).
        task = Task(id="t2", prompt="p", kind=TaskKind.PROMPT, retry_count=3)
        d._tasks[task.id] = task
        d._task_store.save(task)

        _run(d._handle_stalled_task(task))

        assert task.status is TaskStatus.FAILED
        assert d._queue.empty()
        # retry_count not bumped past the ceiling.
        assert task.retry_count == 3

    def test_within_budget_increments_and_requeues(
        self,
        tmp_path: Path,
        minimal_config: MaxwellDaemonConfig,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import maxwell_daemon.daemon.runner as runner_mod
        from maxwell_daemon.daemon.retry_policy import RetryPolicy

        # Zero backoff so the delayed re-enqueue runs on the next loop tick.
        monkeypatch.setattr(
            runner_mod,
            "DEFAULT_RETRY_POLICY",
            RetryPolicy(base_delay_seconds=0.0, max_delay_seconds=0.0),
        )

        async def body() -> None:
            d = _make_daemon(tmp_path, minimal_config)
            task = Task(id="t3", prompt="p", kind=TaskKind.PROMPT, retry_count=0)
            d._tasks[task.id] = task
            d._task_store.save(task)

            await d._handle_stalled_task(task)
            # Let the scheduled zero-delay re-enqueue run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert task.retry_count == 1
            assert not d._queue.empty()
            reloaded = d._task_store.get("t3")
            assert reloaded is not None
            assert reloaded.retry_count == 1
            assert reloaded.status is TaskStatus.QUEUED

        _run(body())


# ── #972: on-loop queue saturation is observable ─────────────────────────────


class TestQueueSaturationVisibility:
    def test_fail_saturated_task_marks_failed_and_emits_event(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        async def body() -> None:
            d = _make_daemon(tmp_path, minimal_config)
            task = Task(id="sat", prompt="p", kind=TaskKind.PROMPT, status=TaskStatus.QUEUED)
            d._tasks[task.id] = task
            d._task_store.save(task)

            subscription = d._events.subscribe()
            try:
                d._fail_saturated_task(task)
                # Allow the fire-and-forget event publish to run, then read the
                # single emitted event without blocking forever.
                await asyncio.sleep(0)
                event = await asyncio.wait_for(subscription.__anext__(), timeout=2.0)
            finally:
                await subscription.aclose()

            assert task.status is TaskStatus.FAILED
            assert "saturated" in (task.error or "")
            reloaded = d._task_store.get("sat")
            assert reloaded is not None and reloaded.status is TaskStatus.FAILED
            assert event.kind == EventKind.TASK_FAILED
            assert event.payload.get("reason") == "queue_saturated"

        _run(body())


# ── #975: single-instance storage guard ──────────────────────────────────────


class TestSingleInstanceGuard:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="same-process flock conflict semantics differ on Windows; CI is Linux",
    )
    def test_second_daemon_same_root_refuses_to_start(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        async def body() -> None:
            first = Daemon(
                minimal_config,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await first.start(worker_count=1)
            try:
                # Same storage root (same task_store parent dir).
                second = Daemon(
                    minimal_config,
                    ledger_path=tmp_path / "ledger2.db",
                    task_store_path=tmp_path / "tasks2.db",
                )
                with pytest.raises(InstanceLockError):
                    await second.start(worker_count=1)
            finally:
                await first.stop()

        _run(body())

    def test_restart_after_stop_succeeds(
        self, tmp_path: Path, minimal_config: MaxwellDaemonConfig
    ) -> None:
        async def body() -> None:
            d1 = Daemon(
                minimal_config,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await d1.start(worker_count=1)
            await d1.stop()

            d2 = Daemon(
                minimal_config,
                ledger_path=tmp_path / "ledger.db",
                task_store_path=tmp_path / "tasks.db",
            )
            await d2.start(worker_count=1)  # must not raise
            await d2.stop()

        _run(body())
