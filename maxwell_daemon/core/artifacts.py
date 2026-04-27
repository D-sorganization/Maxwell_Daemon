"""Typed task and work-item artifacts with content integrity checks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.contracts import require

__all__ = [
    "Artifact",
    "ArtifactIntegrityError",
    "ArtifactKind",
    "ArtifactStore",
]


class ArtifactKind(str, Enum):
    PLAN = "plan"
    DIFF = "diff"
    COMMAND_LOG = "command_log"
    TEST_RESULT = "test_result"
    CHECK_RESULT = "check_result"
    SANDBOX_EXECUTION = "sandbox_execution"
    SCREENSHOT = "screenshot"
    BROWSER_CONSOLE = "browser_console"
    PAGE_ERROR = "page_error"
    TRANSCRIPT = "transcript"
    HANDOFF = "handoff"
    PR_BODY = "pr_body"
    METADATA = "metadata"


class ArtifactIntegrityError(RuntimeError):
    """Raised when artifact metadata no longer matches stored bytes."""


class Artifact(BaseModel):
    """Metadata for one durable artifact blob."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    work_item_id: str | None = Field(default=None, min_length=1)
    kind: ArtifactKind
    name: str = Field(..., min_length=1)
    media_type: str = Field(..., min_length=1)
    path: Path
    sha256: str = Field(..., min_length=64, max_length=64)
    size_bytes: int = Field(..., ge=0)
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def _sha256_is_hex(cls, value: str) -> str:
        int(value, 16)
        return value.lower()

    @model_validator(mode="after")
    def _has_exactly_one_owner(self) -> Artifact:
        owners = [self.task_id, self.work_item_id]
        if sum(owner is not None for owner in owners) != 1:
            raise ValueError("artifact must belong to exactly one task or work item")
        return self


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    work_item_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_work_item ON artifacts(work_item_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_kind ON artifacts(kind);
"""


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _parse_iso_required(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extension_for_media_type(media_type: str) -> str:
    normalized = media_type.split(";", 1)[0].strip().lower()
    return {
        "application/json": ".json",
        "text/markdown": ".md",
        "text/plain": ".txt",
        "text/x-diff": ".diff",
    }.get(normalized, ".bin")


class ArtifactStore:
    """SQLite metadata plus filesystem blob storage for task evidence."""

    def __init__(self, db_path: Path | str, *, blob_root: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._blob_root = Path(blob_root).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._blob_root.mkdir(parents=True, exist_ok=True)
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

    def put_text(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        text: str,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "text/plain; charset=utf-8",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put_bytes(
            kind=kind,
            name=name,
            data=text.encode("utf-8"),
            task_id=task_id,
            work_item_id=work_item_id,
            media_type=media_type,
            metadata=metadata,
        )

    def put_json(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        value: object,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "application/json",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        return self.put_text(
            kind=kind,
            name=name,
            text=_json(value),
            task_id=task_id,
            work_item_id=work_item_id,
            media_type=media_type,
            metadata=metadata,
        )

    def put_bytes(
        self,
        *,
        kind: ArtifactKind,
        name: str,
        data: bytes,
        task_id: str | None = None,
        work_item_id: str | None = None,
        media_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        require(bool(name), "ArtifactStore.put_bytes: name must be non-empty")
        require(bool(media_type), "ArtifactStore.put_bytes: media_type must be non-empty")
        artifact_id = uuid.uuid4().hex
        relative_path = self._relative_blob_path(
            artifact_id=artifact_id,
            task_id=task_id,
            work_item_id=work_item_id,
            media_type=media_type,
        )
        artifact = Artifact(
            id=artifact_id,
            task_id=task_id,
            work_item_id=work_item_id,
            kind=kind,
            name=name,
            media_type=media_type,
            path=relative_path,
            sha256=_hash_bytes(data),
            size_bytes=len(data),
            created_at=datetime.now(timezone.utc),
            metadata=metadata or {},
        )
        blob_path = self._blob_path(relative_path)
        self._write_blob_atomic(blob_path, data)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, task_id, work_item_id, kind, name, media_type, path,
                    sha256, size_bytes, created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _artifact_to_row(artifact),
            )
        return artifact

    def get(self, artifact_id: str) -> Artifact | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        return _row_to_artifact(row) if row else None

    def read_text(self, artifact_id: str) -> str:
        return self.read_bytes(artifact_id).decode("utf-8")

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        blob_path = self._blob_path(artifact.path)
        data = blob_path.read_bytes()
        if len(data) != artifact.size_bytes or _hash_bytes(data) != artifact.sha256:
            raise ArtifactIntegrityError(f"artifact {artifact_id} failed integrity check")
        return data

    def list_for_task(
        self,
        task_id: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        return self._list(owner_column="task_id", owner_id=task_id, kind=kind)

    def list_for_work_item(
        self,
        work_item_id: str,
        *,
        kind: ArtifactKind | None = None,
    ) -> list[Artifact]:
        return self._list(owner_column="work_item_id", owner_id=work_item_id, kind=kind)

    def close(self) -> None:
        return None

    def _list(
        self,
        *,
        owner_column: str,
        owner_id: str,
        kind: ArtifactKind | None,
    ) -> list[Artifact]:
        if owner_column not in {"task_id", "work_item_id"}:
            raise ValueError(f"unsupported artifact owner column: {owner_column}")
        query = f"SELECT * FROM artifacts WHERE {owner_column} = ?"
        args: list[object] = [owner_id]
        if kind is not None:
            query += " AND kind = ?"
            args.append(kind.value)
        query += " ORDER BY created_at ASC, id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_artifact(row) for row in rows]

    def _relative_blob_path(
        self,
        *,
        artifact_id: str,
        task_id: str | None,
        work_item_id: str | None,
        media_type: str,
    ) -> Path:
        if task_id is not None and work_item_id is not None:
            raise ValueError("artifact cannot belong to both task and work item")
        if task_id is None and work_item_id is None:
            raise ValueError("artifact must belong to a task or work item")
        filename = artifact_id + _extension_for_media_type(media_type)
        if task_id is not None:
            return Path("tasks") / task_id / filename
        return Path("work-items") / str(work_item_id) / filename

    def _blob_path(self, relative_path: Path) -> Path:
        if relative_path.is_absolute():
            raise ArtifactIntegrityError(f"artifact path must be relative: {relative_path}")
        root = self._blob_root.resolve()
        candidate = (root / relative_path).resolve()
        if candidate != root and root not in candidate.parents:
            raise ArtifactIntegrityError(f"artifact path escapes blob root: {relative_path}")
        return candidate

    @staticmethod
    def _write_blob_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            tmp_path.replace(path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()


def _artifact_to_row(artifact: Artifact) -> tuple[object, ...]:
    return (
        artifact.id,
        artifact.task_id,
        artifact.work_item_id,
        artifact.kind.value,
        artifact.name,
        artifact.media_type,
        artifact.path.as_posix(),
        artifact.sha256,
        artifact.size_bytes,
        artifact.created_at.isoformat(),
        _json(artifact.metadata),
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        task_id=row["task_id"],
        work_item_id=row["work_item_id"],
        kind=ArtifactKind(row["kind"]),
        name=row["name"],
        media_type=row["media_type"],
        path=Path(row["path"]),
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        created_at=_parse_iso_required(row["created_at"]),
        metadata=json.loads(row["metadata"]),
    )
