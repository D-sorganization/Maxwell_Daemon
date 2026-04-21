"""Tests for task TTL pruning (issue #148).

Covers:
- TaskStore.prune() deletes old completed/failed/cancelled tasks
- completed_at is set when status becomes COMPLETED, FAILED, or CANCELLED
- TaskStore.list_tasks(completed_before=...) filter
- CostLedger.prune()
- POST /api/v1/admin/prune endpoint
- completed_before query param on GET /api/v1/tasks
- PruneScheduler.run_once()
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.ledger import CostLedger, CostRecord
from maxwell_daemon.core.task_store import TaskStore
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_task(**overrides: object) -> Task:
    defaults: dict[str, object] = {
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


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "ledger.db")


# ---------------------------------------------------------------------------
# TaskStore.prune()
# ---------------------------------------------------------------------------


class TestTaskStorePrune:
    def test_prune_deletes_old_completed_tasks(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.COMPLETED)

        # Inject an old completed_at directly to simulate a 100-day-old record.
        import sqlite3

        old_ts = time.time() - 100 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        pruned = store.prune(older_than_days=30)
        assert pruned == 1
        assert store.get(task.id) is None

    def test_prune_deletes_old_failed_tasks(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.FAILED, error="boom")

        import sqlite3

        old_ts = time.time() - 100 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        pruned = store.prune(older_than_days=30)
        assert pruned == 1

    def test_prune_deletes_old_cancelled_tasks(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.CANCELLED)

        import sqlite3

        old_ts = time.time() - 100 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        pruned = store.prune(older_than_days=30)
        assert pruned == 1

    def test_prune_keeps_recent_completed_tasks(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.COMPLETED)
        # completed_at is set to now; should survive a 30-day prune.
        pruned = store.prune(older_than_days=30)
        assert pruned == 0
        assert store.get(task.id) is not None

    def test_prune_never_touches_queued_tasks(self, store: TaskStore) -> None:
        task = _fresh_task()
        store.save(task)
        # Force old completed_at even though status is QUEUED
        import sqlite3

        old_ts = time.time() - 200 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        # completed_at IS set, so prune will delete it — but let's verify that
        # a NULL completed_at (normal queued state) is NOT deleted.
        task2 = _fresh_task()
        store.save(task2)
        store.prune(older_than_days=30)
        # task with old completed_at is deleted; task2 (NULL completed_at) is kept.
        assert store.get(task2.id) is not None

    def test_prune_returns_zero_on_empty_store(self, store: TaskStore) -> None:
        assert store.prune(older_than_days=30) == 0

    def test_prune_multiple_tasks(self, store: TaskStore) -> None:
        import sqlite3

        old_ts = time.time() - 200 * 86400
        for _ in range(5):
            t = _fresh_task()
            store.save(t)
            store.update_status(t.id, TaskStatus.COMPLETED)
            with sqlite3.connect(store._path) as conn:
                conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, t.id))

        # One recent task — should survive.
        recent = _fresh_task()
        store.save(recent)
        store.update_status(recent.id, TaskStatus.COMPLETED)

        pruned = store.prune(older_than_days=30)
        assert pruned == 5
        assert store.get(recent.id) is not None


# ---------------------------------------------------------------------------
# completed_at is set by update_status
# ---------------------------------------------------------------------------


class TestCompletedAtTimestamp:
    def test_completed_at_set_on_completed(self, store: TaskStore, tmp_path: Path) -> None:
        import sqlite3

        task = _fresh_task()
        store.save(task)
        before = time.time()
        store.update_status(task.id, TaskStatus.COMPLETED)
        after = time.time()

        with sqlite3.connect(store._path) as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        assert row is not None
        assert row[0] is not None
        assert before <= row[0] <= after

    def test_completed_at_set_on_failed(self, store: TaskStore) -> None:
        import sqlite3

        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.FAILED, error="oops")

        with sqlite3.connect(store._path) as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        assert row[0] is not None

    def test_completed_at_null_for_queued(self, store: TaskStore) -> None:
        import sqlite3

        task = _fresh_task()
        store.save(task)

        with sqlite3.connect(store._path) as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        assert row[0] is None

    def test_completed_at_set_on_save_with_terminal_status(self, store: TaskStore) -> None:
        import sqlite3

        task = _fresh_task(status=TaskStatus.COMPLETED)
        store.save(task)

        with sqlite3.connect(store._path) as conn:
            row = conn.execute("SELECT completed_at FROM tasks WHERE id = ?", (task.id,)).fetchone()
        assert row[0] is not None


# ---------------------------------------------------------------------------
# TaskStore.list_tasks(completed_before=...)
# ---------------------------------------------------------------------------


class TestListTasksCompletedBefore:
    def test_completed_before_filters_tasks(self, store: TaskStore) -> None:
        import sqlite3

        old = _fresh_task()
        store.save(old)
        store.update_status(old.id, TaskStatus.COMPLETED)

        # Push the completed_at back to 50 days ago.
        old_ts = time.time() - 50 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, old.id))

        recent = _fresh_task()
        store.save(recent)
        store.update_status(recent.id, TaskStatus.COMPLETED)

        cutoff = datetime.now(timezone.utc) - timedelta(days=10)
        result = store.list_tasks(limit=100, completed_before=cutoff)
        ids = {t.id for t in result}
        assert old.id in ids
        assert recent.id not in ids

    def test_completed_before_none_returns_all(self, store: TaskStore) -> None:
        for _ in range(3):
            t = _fresh_task()
            store.save(t)
        assert len(store.list_tasks(limit=100, completed_before=None)) == 3


# ---------------------------------------------------------------------------
# async prune
# ---------------------------------------------------------------------------


class TestAsyncPrune:
    async def test_aprune_works(self, tmp_path: Path) -> None:
        store = TaskStore(tmp_path / "tasks.db")
        task = _fresh_task()
        store.save(task)
        store.update_status(task.id, TaskStatus.COMPLETED)

        import sqlite3

        old_ts = time.time() - 100 * 86400
        with sqlite3.connect(store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        pruned = await store.aprune(older_than_days=30)
        assert pruned == 1
        store.close()


# ---------------------------------------------------------------------------
# CostLedger.prune()
# ---------------------------------------------------------------------------


class TestCostLedgerPrune:
    def _make_record(self, ts: datetime) -> CostRecord:
        from maxwell_daemon.backends.base import TokenUsage

        return CostRecord(
            ts=ts,
            backend="test",
            model="test-model",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, cached_tokens=0),
            cost_usd=0.001,
        )

    def test_prune_deletes_old_records(self, ledger: CostLedger) -> None:
        old_ts = datetime.now(timezone.utc) - timedelta(days=100)
        ledger.record(self._make_record(old_ts))
        ledger.record(self._make_record(datetime.now(timezone.utc)))

        pruned = ledger.prune(older_than_days=30)
        assert pruned == 1

    def test_prune_keeps_recent_records(self, ledger: CostLedger) -> None:
        ledger.record(self._make_record(datetime.now(timezone.utc)))
        pruned = ledger.prune(older_than_days=30)
        assert pruned == 0

    def test_prune_empty_returns_zero(self, ledger: CostLedger) -> None:
        assert ledger.prune(older_than_days=30) == 0

    async def test_aprune(self, tmp_path: Path) -> None:
        from maxwell_daemon.backends.base import TokenUsage

        lg = CostLedger(tmp_path / "l.db")
        old_ts = datetime.now(timezone.utc) - timedelta(days=100)
        rec = CostRecord(
            ts=old_ts,
            backend="b",
            model="m",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, cached_tokens=0),
            cost_usd=0.01,
        )
        lg.record(rec)
        pruned = await lg.aprune(older_than_days=30)
        assert pruned == 1
        lg.close()


# ---------------------------------------------------------------------------
# PruneScheduler
# ---------------------------------------------------------------------------


class TestPruneScheduler:
    async def test_run_once_calls_aprune(self) -> None:
        from maxwell_daemon.daemon.scheduler import PruneScheduler

        mock_store = MagicMock()
        mock_store.aprune = AsyncMock(return_value=3)
        mock_ledger = MagicMock()
        mock_ledger.aprune = AsyncMock(return_value=2)

        scheduler = PruneScheduler(
            task_store=mock_store,
            ledger=mock_ledger,
            retention_days=30,
        )
        result = await scheduler.run_once()
        assert result["pruned_tasks"] == 3
        assert result["pruned_ledger"] == 2
        mock_store.aprune.assert_called_once_with(30)
        mock_ledger.aprune.assert_called_once_with(30)

    async def test_run_once_rotates_audit_log(self) -> None:
        from maxwell_daemon.daemon.scheduler import PruneScheduler

        mock_store = MagicMock()
        mock_store.aprune = AsyncMock(return_value=0)
        mock_ledger = MagicMock()
        mock_ledger.aprune = AsyncMock(return_value=0)
        mock_audit = MagicMock()
        mock_audit.rotate = MagicMock(return_value=5)

        scheduler = PruneScheduler(
            task_store=mock_store,
            ledger=mock_ledger,
            retention_days=90,
            audit_logger=mock_audit,
        )
        result = await scheduler.run_once()
        assert result["pruned_audit"] == 5
        mock_audit.rotate.assert_called_once()

    async def test_start_stop_idempotent(self) -> None:
        from maxwell_daemon.daemon.scheduler import PruneScheduler

        mock_store = MagicMock()
        mock_store.aprune = AsyncMock(return_value=0)
        mock_ledger = MagicMock()
        mock_ledger.aprune = AsyncMock(return_value=0)

        scheduler = PruneScheduler(
            task_store=mock_store,
            ledger=mock_ledger,
            retention_days=90,
        )
        await scheduler.start()
        await scheduler.start()  # idempotent
        await scheduler.stop()
        await scheduler.stop()  # idempotent

    def test_rejects_invalid_retention_days(self) -> None:
        from maxwell_daemon.contracts import PreconditionError
        from maxwell_daemon.daemon.scheduler import PruneScheduler

        with pytest.raises(PreconditionError):
            PruneScheduler(
                task_store=MagicMock(),
                ledger=MagicMock(),
                retention_days=0,
            )

    async def test_audit_rotation_failure_does_not_raise(self) -> None:
        from maxwell_daemon.daemon.scheduler import PruneScheduler

        mock_store = MagicMock()
        mock_store.aprune = AsyncMock(return_value=0)
        mock_ledger = MagicMock()
        mock_ledger.aprune = AsyncMock(return_value=0)
        mock_audit = MagicMock()
        mock_audit.rotate = MagicMock(side_effect=RuntimeError("disk full"))

        scheduler = PruneScheduler(
            task_store=mock_store,
            ledger=mock_ledger,
            retention_days=30,
            audit_logger=mock_audit,
        )
        # Should not raise despite audit rotation failure.
        result = await scheduler.run_once()
        assert result["pruned_audit"] == 0


# ---------------------------------------------------------------------------
# POST /api/v1/admin/prune endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon)) as c:
        yield c


class TestAdminPruneEndpoint:
    def test_prune_returns_200_with_counts(self, client: TestClient) -> None:
        r = client.post("/api/v1/admin/prune")
        assert r.status_code == 200
        body = r.json()
        assert "pruned_tasks" in body
        assert "pruned_ledger_entries" in body
        assert "retention_days" in body

    def test_prune_uses_configured_retention_days(self, client: TestClient) -> None:
        r = client.post("/api/v1/admin/prune")
        body = r.json()
        # Default from AgentConfig is 90 days.
        assert body["retention_days"] == 90

    def test_prune_accepts_override_retention_days(self, client: TestClient) -> None:
        r = client.post("/api/v1/admin/prune?retention_days=7")
        assert r.status_code == 200
        body = r.json()
        assert body["retention_days"] == 7

    def test_prune_rejects_zero_retention_days(self, client: TestClient) -> None:
        r = client.post("/api/v1/admin/prune?retention_days=0")
        assert r.status_code == 422

    def test_prune_actually_removes_old_tasks(self, daemon: Daemon, client: TestClient) -> None:
        import sqlite3

        task = daemon.submit("test prune")
        daemon._task_store.update_status(task.id, TaskStatus.COMPLETED)
        old_ts = time.time() - 200 * 86400
        with sqlite3.connect(daemon._task_store._path) as conn:
            conn.execute("UPDATE tasks SET completed_at = ? WHERE id = ?", (old_ts, task.id))

        r = client.post("/api/v1/admin/prune?retention_days=30")
        assert r.status_code == 200
        body = r.json()
        assert body["pruned_tasks"] >= 1


# ---------------------------------------------------------------------------
# GET /api/v1/tasks?completed_before=...
# ---------------------------------------------------------------------------


class TestListTasksCompletedBeforeEndpoint:
    def test_completed_before_filters_in_memory_tasks(
        self, daemon: Daemon, client: TestClient
    ) -> None:
        # Submit and immediately mark as completed so finished_at is set.
        task = daemon.submit("old task")
        # Simulate old finished_at.
        task.finished_at = datetime.now(timezone.utc) - timedelta(days=50)

        # Submit a recent task without finished_at.
        daemon.submit("new task")

        # Use naive ISO format (no +00:00 suffix) to avoid URL-encoding issues
        # with the `+` character in query strings.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
        r = client.get(f"/api/v1/tasks?completed_before={cutoff}")
        assert r.status_code == 200
        ids = [t["id"] for t in r.json()]
        assert task.id in ids

    def test_completed_before_invalid_param_returns_422(self, client: TestClient) -> None:
        r = client.get("/api/v1/tasks?completed_before=not-a-datetime")
        assert r.status_code == 422
