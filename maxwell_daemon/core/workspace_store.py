"""SQLite-backed workspace lifecycle metadata store."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from maxwell_daemon.core.workspaces import (
    TaskWorkspace,
    WorkspaceCheckpoint,
    WorkspaceStatus,
    validate_workspace_transition,
)

__all__ = ["WorkspaceStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL UNIQUE,
    work_item_id TEXT,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    work_branch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    current_head TEXT,
    base_head TEXT,
    checkpoint_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workspaces_status ON workspaces(status);
CREATE INDEX IF NOT EXISTS idx_workspaces_repo ON workspaces(repo, updated_at);

CREATE TABLE IF NOT EXISTS workspace_checkpoints (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    label TEXT NOT NULL,
    git_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    diff_artifact_id TEXT,
    metadata TEXT NOT NULL,
    FOREIGN KEY(workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_workspace_checkpoints_workspace
    ON workspace_checkpoints(workspace_id, created_at);
"""


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class WorkspaceStore:
    """Durable workspace and checkpoint storage with transition validation."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
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
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def create_workspace(self, workspace: TaskWorkspace) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspaces (
                    id, task_id, work_item_id, repo, path, base_branch, work_branch,
                    status, created_at, updated_at, last_used_at, current_head,
                    base_head, checkpoint_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _workspace_to_row(workspace),
            )

    def get_workspace(self, workspace_id: str) -> TaskWorkspace | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        return _row_to_workspace(row) if row else None

    def get_workspace_for_task(self, task_id: str) -> TaskWorkspace | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE task_id = ?", (task_id,)).fetchone()
        return _row_to_workspace(row) if row else None

    def transition(self, workspace_id: str, new_status: WorkspaceStatus) -> TaskWorkspace:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
            if row is None:
                raise KeyError(workspace_id)
            workspace = _row_to_workspace(row)
            validate_workspace_transition(workspace.status, new_status)
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET status = ?, updated_at = ?, last_used_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    new_status.value,
                    now.isoformat(),
                    now.isoformat(),
                    workspace_id,
                    workspace.status.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"workspace {workspace_id} changed concurrently")
            updated = conn.execute(
                "SELECT * FROM workspaces WHERE id = ?",
                (workspace_id,),
            ).fetchone()
        if updated is None:
            raise KeyError(workspace_id)
        return _row_to_workspace(updated)

    def touch(self, workspace_id: str) -> TaskWorkspace:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET updated_at = ?, last_used_at = ?
                WHERE id = ?
                """,
                (now.isoformat(), now.isoformat(), workspace_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(workspace_id)
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return _row_to_workspace(row)

    def update_heads(
        self,
        workspace_id: str,
        *,
        current_head: str | None = None,
        base_head: str | None = None,
    ) -> TaskWorkspace:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET current_head = COALESCE(?, current_head),
                    base_head = COALESCE(?, base_head),
                    updated_at = ?,
                    last_used_at = ?
                WHERE id = ?
                """,
                (
                    current_head,
                    base_head,
                    now.isoformat(),
                    now.isoformat(),
                    workspace_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(workspace_id)
            row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return _row_to_workspace(row)

    def create_checkpoint(self, checkpoint: WorkspaceCheckpoint) -> WorkspaceCheckpoint:
        with self._lock, self._connect() as conn:
            workspace_exists = conn.execute(
                "SELECT 1 FROM workspaces WHERE id = ?",
                (checkpoint.workspace_id,),
            ).fetchone()
            if workspace_exists is None:
                raise KeyError(checkpoint.workspace_id)
            conn.execute(
                """
                INSERT INTO workspace_checkpoints (
                    id, workspace_id, label, git_ref, created_at,
                    diff_artifact_id, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                _checkpoint_to_row(checkpoint),
            )
            conn.execute(
                """
                UPDATE workspaces
                SET checkpoint_count = checkpoint_count + 1,
                    updated_at = ?,
                    last_used_at = ?
                WHERE id = ?
                """,
                (
                    checkpoint.created_at.isoformat(),
                    checkpoint.created_at.isoformat(),
                    checkpoint.workspace_id,
                ),
            )
        return checkpoint

    def list_checkpoints(self, workspace_id: str) -> list[WorkspaceCheckpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workspace_checkpoints
                WHERE workspace_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (workspace_id,),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def close(self) -> None:
        return None


def _workspace_to_row(workspace: TaskWorkspace) -> tuple[object, ...]:
    return (
        workspace.id,
        workspace.task_id,
        workspace.work_item_id,
        workspace.repo,
        workspace.path,
        workspace.base_branch,
        workspace.work_branch,
        workspace.status.value,
        workspace.created_at.isoformat(),
        workspace.updated_at.isoformat(),
        workspace.last_used_at.isoformat(),
        workspace.current_head,
        workspace.base_head,
        workspace.checkpoint_count,
    )


def _row_to_workspace(row: sqlite3.Row) -> TaskWorkspace:
    return TaskWorkspace(
        id=row["id"],
        task_id=row["task_id"],
        work_item_id=row["work_item_id"],
        repo=row["repo"],
        path=row["path"],
        base_branch=row["base_branch"],
        work_branch=row["work_branch"],
        status=WorkspaceStatus(row["status"]),
        created_at=_parse_iso(row["created_at"]),
        updated_at=_parse_iso(row["updated_at"]),
        last_used_at=_parse_iso(row["last_used_at"]),
        current_head=row["current_head"],
        base_head=row["base_head"],
        checkpoint_count=row["checkpoint_count"],
    )


def _checkpoint_to_row(checkpoint: WorkspaceCheckpoint) -> tuple[object, ...]:
    return (
        checkpoint.id,
        checkpoint.workspace_id,
        checkpoint.label,
        checkpoint.git_ref,
        checkpoint.created_at.isoformat(),
        checkpoint.diff_artifact_id,
        _json(checkpoint.metadata),
    )


def _row_to_checkpoint(row: sqlite3.Row) -> WorkspaceCheckpoint:
    return WorkspaceCheckpoint(
        id=row["id"],
        workspace_id=row["workspace_id"],
        label=row["label"],
        git_ref=row["git_ref"],
        created_at=_parse_iso(row["created_at"]),
        diff_artifact_id=row["diff_artifact_id"],
        metadata=json.loads(row["metadata"]),
    )
