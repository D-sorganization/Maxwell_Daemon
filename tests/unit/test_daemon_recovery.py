"""Daemon task recovery — queued tasks survive a restart, running tasks fail safe."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
        store = TaskStore(task_store_path)
        pending = Task(
            id="previous-run",
            prompt="unfinished",
            kind=TaskKind.PROMPT,
            priority=25,
            created_at=datetime.now(timezone.utc),
        )
        store.save(pending)
        del store

        # New Daemon instance — recovery should re-queue it.
        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )
        recovered = d.recover()
        assert len(recovered) == 1
        assert recovered[0].id == "previous-run"
        assert recovered[0].priority == 25
        assert d.get_task("previous-run") is not None

    def test_running_task_marked_failed_on_recovery(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
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

        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )
        d.recover()
        # The in-memory view won't include it (only queued get re-queued),
        # but the persistent store reflects the failure.
        fresh_store = TaskStore(task_store_path)
        loaded = fresh_store.get("crashed")
        assert loaded is not None
        assert loaded.status is TaskStatus.FAILED
        assert loaded.error is not None
        assert "crashed" in loaded.error.lower()

    def test_dispatched_task_tracked_on_recovery(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        store = TaskStore(task_store_path)
        dispatched = Task(
            id="remote-run",
            prompt="remote work",
            kind=TaskKind.PROMPT,
            priority=50,
            status=TaskStatus.DISPATCHED,
            dispatched_to="worker-a",
            created_at=datetime.now(timezone.utc),
        )
        store.save(dispatched)
        del store

        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )

        recovered = d.recover()

        assert len(recovered) == 1
        assert recovered[0].id == "remote-run"
        assert recovered[0].status is TaskStatus.DISPATCHED
        assert recovered[0].priority == 50
        assert recovered[0].dispatched_to == "worker-a"
        assert d.get_task("remote-run") is not None
        assert d.get_task("remote-run").status is TaskStatus.DISPATCHED  # type: ignore[union-attr]
        assert d._queue.qsize() == 0

    def test_dispatched_task_without_worker_requeued_on_recovery(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        store = TaskStore(task_store_path)
        dispatched = Task(
            id="missing-worker",
            prompt="remote work",
            kind=TaskKind.PROMPT,
            priority=10,
            status=TaskStatus.DISPATCHED,
            dispatched_to=None,
            created_at=datetime.now(timezone.utc),
        )
        store.save(dispatched)
        del store

        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )

        recovered = d.recover()

        assert len(recovered) == 1
        assert recovered[0].status is TaskStatus.QUEUED
        assert recovered[0].priority == 10
        assert recovered[0].dispatched_to is None
        assert d.get_task("missing-worker").status is TaskStatus.QUEUED  # type: ignore[union-attr]
        assert d._queue.qsize() == 1

    def test_task_store_round_trips_priority_and_dispatched_to(
        self, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        store = TaskStore(task_store_path)
        task = Task(
            id="persist-fleet-fields",
            prompt="remote",
            kind=TaskKind.PROMPT,
            priority=5,
            status=TaskStatus.DISPATCHED,
            dispatched_to="worker-b",
            created_at=datetime.now(timezone.utc),
        )

        store.save(task)
        loaded = store.get(task.id)

        assert loaded is not None
        assert loaded.priority == 5
        assert loaded.dispatched_to == "worker-b"
        assert loaded.status is TaskStatus.DISPATCHED

    def test_existing_task_db_migrates_priority_and_dispatched_to(
        self, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        with sqlite3.connect(task_store_path) as conn:
            conn.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    repo TEXT,
                    backend TEXT,
                    model TEXT,
                    issue_repo TEXT,
                    issue_number INTEGER,
                    issue_mode TEXT,
                    ab_group TEXT,
                    result TEXT,
                    error TEXT,
                    pr_url TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    completed_at TEXT
                );
                """
            )

        TaskStore(task_store_path)

        with sqlite3.connect(task_store_path) as conn:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
            }

        assert "priority" in columns
        assert "dispatched_to" in columns

    def test_submit_persists_immediately(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )
        task = d.submit("hello")
        # Reopen the store in a fresh object — the task must already be there.
        store = TaskStore(task_store_path)
        assert store.get(task.id) is not None

    def test_cancel_persists_status(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        task_store_path = tmp_path / "tasks.db"
        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=task_store_path,
            action_store_path=tmp_path / "actions.db",
        )
        task = d.submit("hi")
        d.cancel_task(task.id)
        store = TaskStore(task_store_path)
        loaded = store.get(task.id)
        assert loaded is not None
        assert loaded.status is TaskStatus.CANCELLED

    def test_submit_does_not_enqueue_when_persistence_fails(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        class FailingStore:
            def save(self, _task: Any) -> None:
                raise RuntimeError("disk locked")

            def get(self, _id: str) -> Any:
                return None

            def delete(self, _id: str) -> None:
                pass

            async def aprune(self, _days: int) -> int:
                return 0

        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=tmp_path / "tasks.db",
            action_store_path=tmp_path / "actions.db",
        )
        d._task_store = FailingStore()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="disk locked"):
            d.submit("hello", task_id="persist-first")

        assert d.get_task("persist-first") is None
        assert d._queue.qsize() == 0

    def test_submit_issue_does_not_enqueue_when_persistence_fails(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        class FailingStore:
            def save(self, _task: Any) -> None:
                raise RuntimeError("disk locked")

            def get(self, _id: str) -> Any:
                return None

            def delete(self, _id: str) -> None:
                pass

            async def aprune(self, _days: int) -> int:
                return 0

        d = Daemon(
            cfg,
            ledger_path=tmp_path / "l.db",
            task_store_path=tmp_path / "tasks.db",
            action_store_path=tmp_path / "actions.db",
        )
        d._task_store = FailingStore()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="disk locked"):
            d.submit_issue(
                repo="owner/repo",
                issue_number=350,
                mode="plan",
                task_id="issue-persist-first",
            )

        assert d.get_task("issue-persist-first") is None
        assert d._queue.qsize() == 0

    def test_async_retention_uses_non_blocking_prune_methods(
        self, cfg: MaxwellDaemonConfig, tmp_path: Path
    ) -> None:
        class AsyncPruneStore:
            def __init__(self, count: int) -> None:
                self.count = count
                self.days: int | None = None

            def prune(self, _days: int) -> int:
                raise AssertionError("sync prune should not run from async retention")

            async def aprune(self, days: int) -> int:
                self.days = days
                return self.count

            def get(self, _id: str) -> Any:
                return None

        async def body() -> None:
            d = Daemon(
                cfg,
                ledger_path=tmp_path / "l.db",
                task_store_path=tmp_path / "tasks.db",
                action_store_path=tmp_path / "actions.db",
            )
            task_store = AsyncPruneStore(2)
            ledger = AsyncPruneStore(3)
            d._task_store = task_store  # type: ignore[assignment]
            d._ledger = ledger  # type: ignore[assignment]
            old_done = Task(
                id="old-done",
                prompt="old",
                kind=TaskKind.PROMPT,
                status=TaskStatus.COMPLETED,
                finished_at=datetime.now(timezone.utc) - timedelta(days=45),
            )
            with d._tasks_lock:
                d._tasks[old_done.id] = old_done

            result = await d.aprune_retained_history(30)

            assert result == {"tasks": 2, "ledger_records": 3}
            assert task_store.days == 30
            assert ledger.days == 30
            assert d.get_task(old_done.id) is None

        asyncio.run(body())


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
                action_store_path=tmp_path / "actions.db",
            )

            async def body() -> None:
                await d.start(worker_count=1, recover=False)
                task = d.submit("hi")
                # wait for completion
                for _ in range(100):
                    t = d.get_task(task.id)
                    assert t is not None
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
