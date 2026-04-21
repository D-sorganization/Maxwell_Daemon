"""TaskStore — durable task persistence in SQLite."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
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
    defaults.update(overrides)
    return Task(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def store(tmp_path: Path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.db")


class TestSaveAndGet:
    def test_roundtrip(self, store: TaskStore) -> None:
        task = _fresh_task(prompt="do the thing")
        store.save(task)
        loaded = store.get(task.id)
        assert loaded is not None
        assert loaded.id == task.id
        assert loaded.prompt == "do the thing"
        assert loaded.kind is TaskKind.PROMPT
        assert loaded.status is TaskStatus.QUEUED

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
        assert loaded.prompt == "v2"


class TestUpdateStatus:
    def test_transitions_recorded(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.RUNNING, started_at=datetime.now(timezone.utc))
        loaded = store.get(task.id)
        assert loaded.status is TaskStatus.RUNNING
        assert loaded.started_at is not None

    def test_missing_id_raises(self, store: TaskStore) -> None:
        with pytest.raises(KeyError):
            store.update_status("ghost", TaskStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_async_wrappers_roundtrip(self, store: TaskStore) -> None:
        task = _fresh_task()

        await store.asave(task)
        loaded = await store.aget(task.id)
        assert loaded is not None
        assert loaded.status is TaskStatus.QUEUED

        await store.aupdate_status(task.id, TaskStatus.COMPLETED, result="done")
        listed = await store.alist_tasks(limit=10, status=TaskStatus.COMPLETED)
        assert [item.id for item in listed] == [task.id]
        assert (await store.aget(task.id)).result == "done"

    @pytest.mark.asyncio
    async def test_async_save_rejects_empty_id(self, store: TaskStore) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError):
            await store.asave(_fresh_task(id=""))


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
        assert loaded.status is TaskStatus.FAILED
        assert loaded.error is not None
        assert "crashed" in loaded.error.lower()


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
        assert loaded.issue_repo == "o/r"
        assert loaded.issue_number == 42
        assert loaded.issue_mode == "implement"


class TestSchemaMigration:
    def test_create_if_not_exists(self, tmp_path: Path) -> None:
        """Opening an existing DB that already has a tasks table must not error."""
        db = tmp_path / "t.db"
        s1 = TaskStore(db)
        s1.save(_fresh_task(prompt="x"))
        s2 = TaskStore(db)  # second open should be a no-op, not an error
        assert s2.list_tasks(limit=10)[0].prompt == "x"

    def test_adds_ab_group_to_existing_schema(self, tmp_path: Path) -> None:
        import sqlite3

        db = tmp_path / "legacy.db"
        with sqlite3.connect(db) as conn:
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
                    result TEXT,
                    error TEXT,
                    pr_url TEXT,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT
                );
                """
            )

        store = TaskStore(db)
        store.save(_fresh_task(ab_group="a"))
        assert store.get(store.list_tasks()[0].id).ab_group == "a"

    def test_close_is_compatibility_noop(self, store: TaskStore) -> None:
        store.close()
        task = _fresh_task()
        store.save(task)
        assert store.get(task.id) is not None
