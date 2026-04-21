"""Durable task persistence in SQLite.

Orthogonal to ``CostLedger`` — separate table, separate module, same DB file
(by convention). The in-memory queue remains the hot path during a single run;
this store exists so a daemon restart doesn't lose queued work.

Recovery model
--------------
On startup, ``recover_pending()`` re-queues all ``QUEUED`` tasks and marks any
``RUNNING`` tasks as ``FAILED`` with a "crashed mid-execution" note. We can't
know whether a previously-running task finished before the crash, so we default
to the safe assumption — the human will see the failed task in the UI and can
re-dispatch it if needed.

Connection model
----------------
Each operation opens a short-lived ``sqlite3.Connection`` with a long busy
timeout.  WAL journal mode lets readers proceed concurrently with the single
writer.  A ``threading.Lock`` serialises write operations so thread-pool workers
do not contend inside SQLite during writes.

Async safety
------------
All public ``async`` methods dispatch their SQLite work via
``asyncio.run_in_executor`` so they never block the event loop.  The sync
variants (``save``, ``update_status``, ``get``, ``list_tasks``,
``recover_pending``) remain available for startup / shutdown paths that run
outside an event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from maxwell_daemon.contracts import require

if TYPE_CHECKING:
    from maxwell_daemon.daemon.runner import Task, TaskStatus

__all__ = ["TaskStore"]

_SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS tasks (
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
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# Indexes that depend on migrated-in columns — created after migrations run.
_SCHEMA_POST_MIGRATION = """
CREATE INDEX IF NOT EXISTS idx_tasks_ab_group ON tasks(ab_group);
"""


# Incremental migrations for DBs created before a column existed. SQLite
# doesn't have `ADD COLUMN IF NOT EXISTS`, so we read the column list and only
# add what's missing.
_MIGRATIONS = [
    ("ab_group", "ALTER TABLE tasks ADD COLUMN ab_group TEXT"),
]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class TaskStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            # WAL mode persists at the DB level; set once during initialization
            # to avoid extra lock churn on every short-lived connection.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA_BASE)
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
            for col, ddl in _MIGRATIONS:
                if col not in existing_cols:
                    conn.execute(ddl)
            # Indexes that reference migrated columns run after the migration
            # so old DBs don't explode before they get upgraded.
            conn.executescript(_SCHEMA_POST_MIGRATION)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    # ── Sync internals ────────────────────────────────────────────────────────

    def _save_sync(self, task: Task) -> None:
        now = datetime.now(task.created_at.tzinfo).isoformat()
        row = (
            task.id,
            task.created_at.isoformat(),
            now,
            task.kind.value,
            task.status.value,
            task.prompt,
            task.repo,
            task.backend,
            task.model,
            task.issue_repo,
            task.issue_number,
            task.issue_mode,
            task.ab_group,
            task.result,
            task.error,
            task.pr_url,
            task.cost_usd,
            _iso(task.started_at),
            _iso(task.finished_at),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, created_at, updated_at, kind, status, prompt,
                    repo, backend, model,
                    issue_repo, issue_number, issue_mode, ab_group,
                    result, error, pr_url, cost_usd, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    prompt=excluded.prompt,
                    repo=excluded.repo, backend=excluded.backend, model=excluded.model,
                    issue_repo=excluded.issue_repo,
                    issue_number=excluded.issue_number,
                    issue_mode=excluded.issue_mode,
                    ab_group=excluded.ab_group,
                    result=excluded.result, error=excluded.error, pr_url=excluded.pr_url,
                    cost_usd=excluded.cost_usd,
                    started_at=excluded.started_at, finished_at=excluded.finished_at
                """,
                row,
            )

    def _update_status_sync(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        pr_url: str | None = None,
        cost_usd: float | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        now = datetime.now().isoformat()
        with self._lock, self._connect() as conn:
            cursor = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,))
            if cursor.fetchone() is None:
                raise KeyError(task_id)
            sets = ["status = ?", "updated_at = ?"]
            args: list[object] = [status.value, now]
            for field, value in (
                ("result", result),
                ("error", error),
                ("pr_url", pr_url),
                ("cost_usd", cost_usd),
                ("started_at", _iso(started_at)),
                ("finished_at", _iso(finished_at)),
            ):
                if value is not None:
                    sets.append(f"{field} = ?")
                    args.append(value)
            args.append(task_id)
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",  # nosec B608
                args,
            )

    def _get_sync(self, task_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _list_sync(self, *, limit: int = 100, status: TaskStatus | None = None) -> list[Task]:
        query = "SELECT * FROM tasks"
        args: list[object] = []
        if status is not None:
            query += " WHERE status = ?"
            args.append(status.value)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_task(r) for r in rows]

    def _recover_sync(self) -> list[Task]:
        from maxwell_daemon.daemon.runner import TaskStatus as _TaskStatus

        now = datetime.now().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, finished_at = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    _TaskStatus.FAILED.value,
                    "daemon crashed during execution",
                    now,
                    now,
                    _TaskStatus.RUNNING.value,
                ),
            )
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at",
                (_TaskStatus.QUEUED.value,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    # ── Sync public API ────────────────────────────────────────────────────────

    def save(self, task: Task) -> None:
        require(bool(task.id), "TaskStore.save: task.id must be non-empty")
        self._save_sync(task)

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        pr_url: str | None = None,
        cost_usd: float | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        self._update_status_sync(
            task_id,
            status,
            result=result,
            error=error,
            pr_url=pr_url,
            cost_usd=cost_usd,
            started_at=started_at,
            finished_at=finished_at,
        )

    def get(self, task_id: str) -> Task | None:
        return self._get_sync(task_id)

    def list_tasks(self, *, limit: int = 100, status: TaskStatus | None = None) -> list[Task]:
        return self._list_sync(limit=limit, status=status)

    def recover_pending(self) -> list[Task]:
        """Mark stale RUNNING tasks as FAILED; return anything still QUEUED."""
        return self._recover_sync()

    # ── Async public API ───────────────────────────────────────────────────────

    async def asave(self, task: Task) -> None:
        """Non-blocking version of :meth:`save` for use in async code."""
        require(bool(task.id), "TaskStore.asave: task.id must be non-empty")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._save_sync, task)

    async def aupdate_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        pr_url: str | None = None,
        cost_usd: float | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> None:
        """Non-blocking version of :meth:`update_status` for use in async code."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._update_status_sync(
                task_id,
                status,
                result=result,
                error=error,
                pr_url=pr_url,
                cost_usd=cost_usd,
                started_at=started_at,
                finished_at=finished_at,
            ),
        )

    async def aget(self, task_id: str) -> Task | None:
        """Non-blocking version of :meth:`get` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._get_sync, task_id)

    async def alist_tasks(
        self, *, limit: int = 100, status: TaskStatus | None = None
    ) -> list[Task]:
        """Non-blocking version of :meth:`list_tasks` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._list_sync(limit=limit, status=status))

    def close(self) -> None:
        """Compatibility hook for stores that do not keep an open connection."""
        return None


def _row_to_task(row: sqlite3.Row) -> Task:
    # Local import — avoids a circular dep since maxwell_daemon.daemon.runner imports
    # TaskStore at module load.
    from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus

    # ab_group was added later — missing on older DBs.
    try:
        ab_group = row["ab_group"]
    except (IndexError, KeyError):
        ab_group = None

    return Task(
        id=row["id"],
        prompt=row["prompt"],
        kind=TaskKind(row["kind"]),
        status=TaskStatus(row["status"]),
        repo=row["repo"],
        backend=row["backend"],
        model=row["model"],
        issue_repo=row["issue_repo"],
        issue_number=row["issue_number"],
        issue_mode=row["issue_mode"],
        ab_group=ab_group,
        result=row["result"],
        error=row["error"],
        pr_url=row["pr_url"],
        cost_usd=row["cost_usd"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_iso(row["started_at"]),
        finished_at=_parse_iso(row["finished_at"]),
    )
