"""SQLite-backed store for JWT session revocation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class AuthSessionStore:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._path,
            isolation_level=None,
            check_same_thread=False,
            timeout=10.0,
        )
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    jti TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    revoked_at TEXT
                )
                """
            )
            # Index for revoking all by subject
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_auth_sessions_subject ON auth_sessions(subject)"
            )

    def record_session(self, jti: str, subject: str, issued_at: datetime) -> None:
        """Record a new token issuance."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO auth_sessions (jti, subject, issued_at)
                VALUES (?, ?, ?)
                """,
                (jti, subject, issued_at.isoformat()),
            )

    def revoke(self, jti: str) -> None:
        """Revoke a specific token by JTI."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE jti = ? AND revoked_at IS NULL
                """,
                (now, jti),
            )
            # Also insert just in case it wasn't recorded (e.g. legacy token)
            conn.execute(
                """
                INSERT OR IGNORE INTO auth_sessions (jti, subject, issued_at, revoked_at)
                VALUES (?, ?, ?, ?)
                """,
                (jti, "unknown", now, now),
            )

    def revoke_all_for_subject(self, subject: str) -> int:
        """Revoke all tokens for a subject. Returns number of tokens revoked."""
        now = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                UPDATE auth_sessions
                SET revoked_at = ?
                WHERE subject = ? AND revoked_at IS NULL
                """,
                (now, subject),
            )
            return cursor.rowcount

    def is_revoked(self, jti: str) -> bool:
        """Check if a token has been revoked."""
        with self._get_conn() as conn:
            cursor = conn.execute(
                "SELECT 1 FROM auth_sessions WHERE jti = ? AND revoked_at IS NOT NULL", (jti,)
            )
            return cursor.fetchone() is not None
