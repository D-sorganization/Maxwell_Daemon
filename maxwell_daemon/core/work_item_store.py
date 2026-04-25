"""Durable SQLite persistence for governed work items."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from maxwell_daemon.contracts import require
from maxwell_daemon.core.work_items import (
    WorkItem,
    WorkItemStatus,
    transition_work_item,
)

__all__ = ["WorkItemStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    repo TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    status TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    scope TEXT NOT NULL,
    required_checks TEXT NOT NULL,
    priority INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    task_ids TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_items_status ON work_items(status);
CREATE INDEX IF NOT EXISTS idx_work_items_repo ON work_items(repo);
CREATE INDEX IF NOT EXISTS idx_work_items_source ON work_items(source);
CREATE INDEX IF NOT EXISTS idx_work_items_priority ON work_items(priority);
"""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso_required(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_iso(value: str | None) -> datetime | None:
    return _parse_iso_required(value) if value else None


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


class WorkItemStore:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        if str(db_path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def save(self, item: WorkItem) -> None:
        require(bool(item.id), "WorkItemStore.save: item.id must be non-empty")
        now = datetime.now(item.updated_at.tzinfo or timezone.utc)
        item = item.model_copy(update={"updated_at": now})
        row = _item_to_row(item)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO work_items (
                    id, title, body, repo, source, source_url, status,
                    acceptance_criteria, scope, required_checks, priority,
                    created_at, updated_at, started_at, completed_at, task_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    body=excluded.body,
                    repo=excluded.repo,
                    source=excluded.source,
                    source_url=excluded.source_url,
                    status=excluded.status,
                    acceptance_criteria=excluded.acceptance_criteria,
                    scope=excluded.scope,
                    required_checks=excluded.required_checks,
                    priority=excluded.priority,
                    updated_at=excluded.updated_at,
                    started_at=excluded.started_at,
                    completed_at=excluded.completed_at,
                    task_ids=excluded.task_ids
                """,
                row,
            )

    def get(self, item_id: str) -> WorkItem | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def list_items(
        self,
        *,
        limit: int = 100,
        status: WorkItemStatus | None = None,
        repo: str | None = None,
        source: str | None = None,
        max_priority: int | None = None,
    ) -> list[WorkItem]:
        query = "SELECT * FROM work_items"
        args: list[object] = []
        clauses: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value)
        if repo is not None:
            clauses.append("repo = ?")
            args.append(repo)
        if source is not None:
            clauses.append("source = ?")
            args.append(source)
        if max_priority is not None:
            clauses.append("priority <= ?")
            args.append(max_priority)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY priority ASC, created_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_item(row) for row in rows]

    def transition(
        self,
        item_id: str,
        target: WorkItemStatus,
        *,
        now: datetime | None = None,
    ) -> WorkItem:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM work_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise KeyError(item_id)
            updated = transition_work_item(_row_to_item(row), target, now=now)
            conn.execute(
                """
                UPDATE work_items
                SET status = ?, updated_at = ?, started_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    updated.status.value,
                    updated.updated_at.isoformat(),
                    _iso(updated.started_at),
                    _iso(updated.completed_at),
                    item_id,
                ),
            )
        return updated

    async def asave(self, item: WorkItem) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.save, item)

    async def aget(self, item_id: str) -> WorkItem | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get, item_id)

    async def alist_items(
        self,
        *,
        limit: int = 100,
        status: WorkItemStatus | None = None,
        repo: str | None = None,
        source: str | None = None,
        max_priority: int | None = None,
    ) -> list[WorkItem]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.list_items(
                limit=limit,
                status=status,
                repo=repo,
                source=source,
                max_priority=max_priority,
            ),
        )

    async def atransition(self, item_id: str, target: WorkItemStatus) -> WorkItem:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.transition, item_id, target)

    def close(self) -> None:
        return None


def _item_to_row(item: WorkItem) -> tuple[object, ...]:
    return (
        item.id,
        item.title,
        item.body,
        item.repo,
        item.source,
        item.source_url,
        item.status.value,
        _json([criterion.model_dump() for criterion in item.acceptance_criteria]),
        _json(item.scope.model_dump()),
        _json(list(item.required_checks)),
        item.priority,
        item.created_at.isoformat(),
        item.updated_at.isoformat(),
        _iso(item.started_at),
        _iso(item.completed_at),
        _json(list(item.task_ids)),
    )


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        id=row["id"],
        title=row["title"],
        body=row["body"],
        repo=row["repo"],
        source=row["source"],
        source_url=row["source_url"],
        status=WorkItemStatus(row["status"]),
        acceptance_criteria=tuple(json.loads(row["acceptance_criteria"])),
        scope=json.loads(row["scope"]),
        required_checks=tuple(json.loads(row["required_checks"])),
        priority=row["priority"],
        created_at=_parse_iso_required(row["created_at"]),
        updated_at=_parse_iso_required(row["updated_at"]),
        started_at=_parse_iso(row["started_at"]),
        completed_at=_parse_iso(row["completed_at"]),
        task_ids=tuple(json.loads(row["task_ids"])),
    )
