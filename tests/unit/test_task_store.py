"""TaskStore — durable task persistence in SQLite."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus


def _fresh_task(**overrides: object) -> Task:
    defaults = {
        "id": uuid.uuid4().hex[:12],
        "prompt": "hello",
        "kind": TaskKind.PROMPT,
        "repo": None,
        "backend": None,
        "model": None,
    }
    defaults.update(overrides)  # type: ignore[arg-type]
    return Task(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db")


class TestSaveAndGet:
    def test_roundtrip(self, store: TaskStore) -> None:
        task = _fresh_task(
            prompt="do the thing",
            backend="primary",
            model="gpt-4.1",
            route_reason="repo override for owner/repo",
        )
        store.save(task)
        loaded = store.get(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.prompt == "do the thing"
        assert loaded.kind is TaskKind.PROMPT
        assert loaded.status is TaskStatus.QUEUED
        assert loaded.backend == "primary"
        assert loaded.model == "gpt-4.1"
        assert loaded.route_reason == "repo override for owner/repo"

    def test_get_missing_returns_none(self, store: TaskStore) -> None:
        assert store.get("nope") is None

    def test_save_rejects_empty_id(self, store: TaskStore) -> None:
        from maxwell_daemon.contracts import PreconditionError

        task = _fresh_task(id="")
        with pytest.raises(PreconditionError):
            store.save(task)

    def test_upsert_updates_existing(self, store: TaskStore) -> None:
        task = _fresh_task(prompt="v1")
        store.save(task)
        task.prompt = "v2"
        store.save(task)
        loaded = store.get(task.id)
        assert loaded.prompt == "v2"  # type: ignore[union-attr]


class TestUpdateStatus:
    def test_transitions_recorded(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(
            task.id, TaskStatus.RUNNING, started_at=datetime.now(timezone.utc)
        )
        loaded = store.get(task.id)
        assert loaded.status is TaskStatus.RUNNING  # type: ignore[union-attr]
        assert loaded.started_at is not None  # type: ignore[union-attr]

    def test_missing_id_raises(self, store: TaskStore) -> None:
        with pytest.raises(KeyError):
            store.update_status("ghost", TaskStatus.COMPLETED)


class TestList:
    def test_lists_newest_first(self, store: TaskStore) -> None:
        a = _fresh_task()
        store.save(a)
        b = _fresh_task()
        store.save(b)
        listed = store.list_tasks(limit=10)
        assert listed[0].id == b.id
        assert listed[1].id == a.id

    def test_respects_limit(self, store: TaskStore) -> None:
        for _ in range(5):
            store.save(_fresh_task())
        assert len(store.list_tasks(limit=3)) == 3

    def test_filter_by_status(self, store: TaskStore) -> None:
        a = _fresh_task()
        b = _fresh_task()
        store.save(a)
        store.save(b)
        store.update_status(a.id, TaskStatus.COMPLETED)
        completed = store.list_tasks(limit=10, status=TaskStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == a.id

    def test_filter_by_completed_before(self, store: TaskStore) -> None:
        old = _fresh_task(finished_at=datetime.now(timezone.utc) - timedelta(days=8))
        recent = _fresh_task(finished_at=datetime.now(timezone.utc))
        store.save(old)
        store.save(recent)
        store.update_status(old.id, TaskStatus.COMPLETED, finished_at=old.finished_at)
        store.update_status(
            recent.id, TaskStatus.COMPLETED, finished_at=recent.finished_at
        )

        listed = store.list_tasks(
            limit=10,
            completed_before=datetime.now(timezone.utc) - timedelta(days=1),
        )

        assert [task.id for task in listed] == [old.id]


class TestRecoverPending:
    def test_recovers_queued(self, store: TaskStore) -> None:
        queued = _fresh_task()
        done = _fresh_task()
        store.save(queued)
        store.save(done)
        store.update_status(done.id, TaskStatus.COMPLETED)

        recovered = store.recover_pending()
        ids = {t.id for t in recovered}
        assert queued.id in ids
        assert done.id not in ids

    def test_marks_stale_running_as_failed(self, store: TaskStore) -> None:
        running = _fresh_task()
        store.save(running)
        store.update_status(running.id, TaskStatus.RUNNING)

        store.recover_pending()
        loaded = store.get(running.id)
        assert loaded.status is TaskStatus.FAILED  # type: ignore[union-attr]
        assert loaded.error is not None  # type: ignore[union-attr]
        assert "crashed" in loaded.error.lower()  # type: ignore[union-attr]


class TestPrune:
    def test_deletes_terminal_tasks_older_than_threshold(
        self, store: TaskStore
    ) -> None:
        old_done = _fresh_task(
            finished_at=datetime.now(timezone.utc) - timedelta(days=45)
        )
        recent_done = _fresh_task(
            finished_at=datetime.now(timezone.utc) - timedelta(days=1)
        )
        queued = _fresh_task(
            finished_at=datetime.now(timezone.utc) - timedelta(days=45)
        )
        store.save(old_done)
        store.save(recent_done)
        store.save(queued)
        store.update_status(
            old_done.id, TaskStatus.COMPLETED, finished_at=old_done.finished_at
        )
        store.update_status(
            recent_done.id,
            TaskStatus.COMPLETED,
            finished_at=recent_done.finished_at,
        )

        removed = store.prune(older_than_days=30)

        assert removed == 1
        assert store.get(old_done.id) is None
        assert store.get(recent_done.id) is not None
        assert store.get(queued.id) is not None

    def test_deletes_terminal_tasks_with_null_completed_at_and_old_finished_at(
        self, tmp_path: Path
    ) -> None:
        import sqlite3

        db = tmp_path / "tasks.db"
        store = TaskStore(db)
        finished_at = datetime.now(timezone.utc) - timedelta(days=45)
        old_done = _fresh_task(finished_at=finished_at)
        store.save(old_done)
        store.update_status(old_done.id, TaskStatus.COMPLETED, finished_at=finished_at)
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE tasks SET completed_at = NULL WHERE id = ?", (old_done.id,)
            )
            conn.commit()

        removed = store.prune(older_than_days=30)

        assert removed == 1
        assert store.get(old_done.id) is None


class TestIssueFields:
    def test_preserves_issue_metadata(self, store: TaskStore) -> None:
        task = _fresh_task(
            kind=TaskKind.ISSUE,
            issue_repo="o/r",
            issue_number=42,
            issue_mode="implement",
        )
        store.save(task)
        loaded = store.get(task.id)
        assert loaded.issue_repo == "o/r"  # type: ignore[union-attr]
        assert loaded.issue_number == 42  # type: ignore[union-attr]
        assert loaded.issue_mode == "implement"  # type: ignore[union-attr]


class TestSchemaMigration:
    def test_create_if_not_exists(self, tmp_path: Path) -> None:
        """Opening an existing DB that already has a tasks table must not error."""
        db = tmp_path / "t.db"
        s1 = TaskStore(db)
        s1.save(_fresh_task(prompt="x"))
        s2 = TaskStore(db)  # second open should be a no-op, not an error
        assert s2.list_tasks(limit=10)[0].prompt == "x"


class TestClose:
    def test_close_is_idempotent(self) -> None:
        store = TaskStore(":memory:")  # type: ignore[arg-type]
        store.close()
        store.close()  # second close must not raise

    def test_close_terminates_connection(self, store: TaskStore) -> None:
        """close() is a compatibility no-op — must not raise."""

        task = _fresh_task()
        store.save(task)
        store.close()
        assert store.get(task.id) is not None


class TestTimezoneAwareTimestamps:
    """Regression tests for issue #147.

    ``update_status`` and ``recover_pending`` used to write naive
    ``datetime.now()`` strings, which then failed to compare against aware
    datetimes produced elsewhere in the codebase.
    """

    def test_update_status_writes_aware_timestamp(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.RUNNING)

        loaded = store.get(task.id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        # Must be comparable to an aware "now" without TypeError.
        assert loaded.created_at <= datetime.now(timezone.utc)

    def test_recover_pending_writes_aware_timestamp(self, store: TaskStore) -> None:
        running = _fresh_task()
        store.save(running)
        store.update_status(running.id, TaskStatus.RUNNING)

        store.recover_pending()
        loaded = store.get(running.id)
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        # Cross-module comparison that used to raise TypeError.
        assert loaded.created_at <= datetime.now(timezone.utc)

    def test_legacy_naive_timestamps_are_read_as_utc(self, tmp_path: Path) -> None:
        """DBs written before the fix contain naive ISO strings. Reads must
        promote them to aware UTC so downstream comparisons still work."""
        import sqlite3

        db = tmp_path / "legacy.db"
        store = TaskStore(db)
        # Directly inject a row with a naive ISO timestamp to simulate legacy data.
        with sqlite3.connect(db) as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, created_at, updated_at, kind, status, prompt)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-id",
                    "2024-01-01T12:00:00",  # naive, no +00:00 suffix
                    "2024-01-01T12:00:00",
                    TaskKind.PROMPT.value,
                    TaskStatus.QUEUED.value,
                    "legacy",
                ),
            )
            conn.commit()

        loaded = store.get("legacy-id")
        assert loaded is not None
        assert loaded.created_at.tzinfo is not None
        assert loaded.created_at <= datetime.now(timezone.utc)


class TestAsyncAPI:
    async def test_asave_and_aget(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks.db")
        task = _fresh_task(prompt="async test")
        await store.asave(task)
        loaded = await store.aget(task.id)
        assert loaded is not None
        assert loaded.prompt == "async test"
        store.close()

    async def test_aupdate_status(self, tmp_path: Path) -> None:
        from datetime import datetime, timezone

        store = TaskStore(tmp_path / "tasks.db")
        task = _fresh_task()
        await store.asave(task)
        await store.aupdate_status(
            task.id,
            TaskStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        loaded = await store.aget(task.id)
        assert loaded is not None
        assert loaded.status is TaskStatus.RUNNING
        store.close()

    async def test_alist_tasks(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks.db")
        task1 = _fresh_task()
        task2 = _fresh_task()
        await store.asave(task1)
        await store.asave(task2)
        tasks = await store.alist_tasks(limit=10)
        assert len(tasks) == 2
        store.close()

    async def test_aget_missing_returns_none(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks.db")
        result = await store.aget("nonexistent-id-xyz")
        assert result is None
        store.close()

    async def test_asave_rejects_empty_id(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        store = TaskStore(tmp_path / "tasks.db")
        task = _fresh_task(id="")
        with pytest.raises(PreconditionError):
            await store.asave(task)
        store.close()
