"""SQLite-backed action ledger."""

from __future__ import annotations

import builtins
import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from maxwell_daemon.core.actions import (
    Action,
    ActionKind,
    ActionRiskLevel,
    ActionStatus,
    validate_action_transition,
)

__all__ = ["ActionStore"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    work_item_id TEXT,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    rejected_by TEXT,
    rejected_at TEXT,
    rejection_reason TEXT,
    result_artifact_id TEXT,
    result TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    inverse_payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_task ON actions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_work_item ON actions(work_item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
"""


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_iso_required(value: str) -> datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError("expected non-empty timestamp")
    return parsed


class ActionStore:
    """Durable action storage with central transition validation."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE actions ADD COLUMN inverse_payload TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def save(self, action: Action) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO actions (
                    id, task_id, work_item_id, kind, status, summary, payload,
                    risk_level, requires_approval, approved_by, approved_at,
                    rejected_by, rejected_at, rejection_reason, result_artifact_id,
                    result, error, created_at, updated_at, inverse_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id=excluded.task_id,
                    work_item_id=excluded.work_item_id,
                    kind=excluded.kind,
                    status=excluded.status,
                    summary=excluded.summary,
                    payload=excluded.payload,
                    risk_level=excluded.risk_level,
                    requires_approval=excluded.requires_approval,
                    approved_by=excluded.approved_by,
                    approved_at=excluded.approved_at,
                    rejected_by=excluded.rejected_by,
                    rejected_at=excluded.rejected_at,
                    rejection_reason=excluded.rejection_reason,
                    result_artifact_id=excluded.result_artifact_id,
                    result=excluded.result,
                    error=excluded.error,
                    updated_at=excluded.updated_at,
                    inverse_payload=excluded.inverse_payload
                """,
                _action_to_row(action),
            )

    def get(self, action_id: str) -> Action | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return _row_to_action(row) if row else None

    def list_for_task(self, task_id: str) -> builtins.list[Action]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE task_id = ? ORDER BY created_at ASC, id ASC",
                (task_id,),
            ).fetchall()
        return [_row_to_action(row) for row in rows]

    def list(
        self,
        *,
        status: ActionStatus | None = None,
        task_id: str | None = None,
        work_item_id: str | None = None,
        limit: int = 100,
    ) -> builtins.list[Action]:
        """Return actions across tasks for queue and audit views."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = "SELECT * FROM actions"
        clauses: builtins.list[str] = []
        args: builtins.list[object] = []
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value)
        if task_id is not None:
            clauses.append("task_id = ?")
            args.append(task_id)
        if work_item_id is not None:
            clauses.append("work_item_id = ?")
            args.append(work_item_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, id ASC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_action(row) for row in rows]

    def list_pending(self, *, limit: int = 100) -> builtins.list[Action]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM actions
                WHERE status = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (ActionStatus.PROPOSED.value, limit),
            ).fetchall()
        return [_row_to_action(row) for row in rows]

    def transition(
        self,
        action_id: str,
        new_status: ActionStatus,
        *,
        actor: str | None = None,
        reason: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        result_artifact_id: str | None = None,
        inverse_payload: dict[str, Any] | None = None,
    ) -> Action:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
            if row is None:
                raise KeyError(action_id)
            action = _row_to_action(row)
            validate_action_transition(action.status, new_status)
            if (
                action.approved_by is not None
                and actor is not None
                and new_status is ActionStatus.APPROVED
            ):
                raise ValueError("approval decision is immutable")

            updates: dict[str, object | None] = {
                "status": new_status.value,
                "updated_at": now.isoformat(),
            }
            if new_status is ActionStatus.APPROVED:
                updates["approved_by"] = actor
                updates["approved_at"] = now.isoformat()
            elif new_status is ActionStatus.REJECTED:
                updates["rejected_by"] = actor
                updates["rejected_at"] = now.isoformat()
                updates["rejection_reason"] = reason
            if result is not None:
                updates["result"] = _json(result)
            if error is not None:
                updates["error"] = error
            if result_artifact_id is not None:
                updates["result_artifact_id"] = result_artifact_id
            if inverse_payload is not None:
                updates["inverse_payload"] = _json(inverse_payload)

            assignments = ", ".join(f"{key} = ?" for key in updates)
            args = [*updates.values(), action_id, action.status.value]
            cursor = conn.execute(
                f"UPDATE actions SET {assignments} WHERE id = ? AND status = ?",  # nosec B608
                args,
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"action {action_id} changed concurrently")
            updated = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if updated is None:
            raise KeyError(action_id)
        return _row_to_action(updated)

    def close(self) -> None:
        return None


def _action_to_row(action: Action) -> tuple[object, ...]:
    return (
        action.id,
        action.task_id,
        action.work_item_id,
        action.kind.value,
        action.status.value,
        action.summary,
        _json(action.payload),
        action.risk_level.value,
        int(action.requires_approval),
        action.approved_by,
        action.approved_at.isoformat() if action.approved_at else None,
        action.rejected_by,
        action.rejected_at.isoformat() if action.rejected_at else None,
        action.rejection_reason,
        action.result_artifact_id,
        _json(action.result),
        action.error,
        action.created_at.isoformat(),
        action.updated_at.isoformat(),
        _json(action.inverse_payload) if action.inverse_payload is not None else None,
    )


def _row_to_action(row: sqlite3.Row) -> Action:
    return Action(
        id=row["id"],
        task_id=row["task_id"],
        work_item_id=row["work_item_id"],
        kind=ActionKind(row["kind"]),
        status=ActionStatus(row["status"]),
        summary=row["summary"],
        payload=json.loads(row["payload"]),
        risk_level=ActionRiskLevel(row["risk_level"]),
        requires_approval=bool(row["requires_approval"]),
        approved_by=row["approved_by"],
        approved_at=_parse_iso(row["approved_at"]),
        rejected_by=row["rejected_by"],
        rejected_at=_parse_iso(row["rejected_at"]),
        rejection_reason=row["rejection_reason"],
        result_artifact_id=row["result_artifact_id"],
        result=json.loads(row["result"]),
        error=row["error"],
        created_at=_parse_iso_required(row["created_at"]),
        updated_at=_parse_iso_required(row["updated_at"]),
        inverse_payload=(json.loads(row["inverse_payload"]) if row["inverse_payload"] else None),
    )
