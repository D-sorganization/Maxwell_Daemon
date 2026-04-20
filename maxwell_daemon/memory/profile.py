"""Durable per-repo facts — what the agent has learned about a repository.

Keyed by ``(repo, key)``. Values are free-form strings; the LLM renders them
as bullet-list context. Schema lives in the shared memory DB so operators see
one file per fleet rather than one per memory tier.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from maxwell_daemon.contracts import require

__all__ = ["RepoProfile"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repo_profile (
    repo TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (repo, key)
);
CREATE INDEX IF NOT EXISTS idx_repo_profile_repo ON repo_profile(repo);
"""


class RepoProfile:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        finally:
            conn.close()

    def learn(self, repo: str, key: str, value: str) -> None:
        require(bool(repo), "RepoProfile.learn: repo must be non-empty")
        require(bool(key), "RepoProfile.learn: key must be non-empty")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_profile (repo, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo, key) DO UPDATE SET value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (repo, key, value, now),
            )

    def forget(self, repo: str, key: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM repo_profile WHERE repo = ? AND key = ?",
                (repo, key),
            )
            return cursor.rowcount > 0

    def facts(self, repo: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM repo_profile WHERE repo = ? ORDER BY key",
                (repo,),
            ).fetchall()
        return dict(rows)

    def render(self, repo: str, *, max_chars: int = 4000) -> str:
        items = self.facts(repo)
        if not items:
            return ""
        lines = [f"- {k}: {v}" for k, v in items.items()]
        rendered = "\n".join(lines)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n... (truncated)"
        return rendered
