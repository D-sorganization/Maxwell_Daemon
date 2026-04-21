"""Daemon task recovery — queued tasks survive a restart, running tasks fail safe."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus


@pytest.fixture
def cfg() -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"x": {"type": "ollama", "model": "y"}},
            "agent": {"default_backend": "x"},
        }
    )


class TestRecovery:
    def test_queued_task_requeued_on_start(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        # Simulate a previous daemon run by writing directly to the store.
        from datetime import datetime, timezone

        store = TaskStore(task_store_path)
        pending = Task(
            id="previous-run",
            prompt="unfinished",
            kind=TaskKind.PROMPT,
            created_at=datetime.now(timezone.utc),
        )
        store.save(pending)
        del store

        # New Daemon instance — recovery should re-queue it.
        d = Daemon(cfg, ledger_path=tmp_path / "l.db", task_store_path=task_store_path)
        recovered = d.recover()
        assert len(recovered) == 1
        assert recovered[0].id == "previous-run"
        assert d.get_task("previous-run") is not None

    def test_running_task_marked_failed_on_recovery(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        from datetime import datetime, timezone

        task_store_path = tmp_path / "tasks.db"
        store = TaskStore(task_store_path)
        running = Task(
            id="crashed",
            prompt="mid-run",
            kind=TaskKind.PROMPT,
            created_at=datetime.now(timezone.utc),
        )
        store.save(running)
        store.update_status("crashed", TaskStatus.RUNNING)
        del store

        d = Daemon(cfg, ledger_path=tmp_path / "l.db", task_store_path=task_store_path)
        d.recover()
        # The in-memory view won't include it (only queued get re-queued),
        # but the persistent store reflects the failure.
        fresh_store = TaskStore(task_store_path)
        loaded = fresh_store.get("crashed")
        assert loaded is not None
        assert loaded.status is TaskStatus.FAILED
        assert loaded.error is not None
        assert "crashed" in loaded.error.lower()

    def test_submit_persists_immediately(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        d = Daemon(cfg, ledger_path=tmp_path / "l.db", task_store_path=task_store_path)
        task = d.submit("hello")
        # Reopen the store in a fresh object — the task must already be there.
        store = TaskStore(task_store_path)
        assert store.get(task.id) is not None

    def test_cancel_persists_status(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        d = Daemon(cfg, ledger_path=tmp_path / "l.db", task_store_path=task_store_path)
        task = d.submit("hi")
        d.cancel_task(task.id)
        store = TaskStore(task_store_path)
        loaded = store.get(task.id)
        assert loaded.status is TaskStatus.CANCELLED


class TestExecutionPersistence:
    def test_completed_task_persists(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        from maxwell_daemon.backends import registry
        from tests.conftest import RecordingBackend

        registry._factories["rec"] = RecordingBackend
        try:
            cfg2 = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"x": {"type": "rec", "model": "y"}},
                    "agent": {"default_backend": "x"},
                }
            )
            task_store_path = tmp_path / "tasks.db"
            d = Daemon(
                cfg2,
                ledger_path=tmp_path / "l.db",
                task_store_path=task_store_path,
            )

            async def body() -> None:
                await d.start(worker_count=1, recover=False)
                task = d.submit("hi")
                # wait for completion
                for _ in range(100):
                    t = d.get_task(task.id)
                    if t.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                        break
                    await asyncio.sleep(0.02)
                await d.stop()
                # After stop, a fresh store instance should see the final state.
                fresh = TaskStore(task_store_path)
                loaded = fresh.get(task.id)
                assert loaded is not None
                assert loaded.status is TaskStatus.COMPLETED

            asyncio.run(body())
        finally:
            registry._factories.pop("rec", None)
