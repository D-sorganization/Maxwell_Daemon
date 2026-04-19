"""Cost ledger — records every request and enforces budgets.

Backed by SQLite so the history survives restarts and can be queried from the
dashboard. Kept small and synchronous on purpose — this is the audit trail, not
a hot-path dependency.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from conductor.backends.base import TokenUsage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    repo TEXT,
    agent_id TEXT,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_records(ts);
CREATE INDEX IF NOT EXISTS idx_cost_backend ON cost_records(backend);
CREATE INDEX IF NOT EXISTS idx_cost_repo ON cost_records(repo);
"""


@dataclass(slots=True, frozen=True)
class CostRecord:
    ts: datetime
    backend: str
    model: str
    usage: TokenUsage
    cost_usd: float
    repo: str | None = None
    agent_id: str | None = None


class CostLedger:
    def __init__(self, db_path: Path | str) -> None:
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

    def record(self, rec: CostRecord) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cost_records
                  (ts, backend, model, repo, agent_id,
                   prompt_tokens, completion_tokens, cached_tokens, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.ts.isoformat(),
                    rec.backend,
                    rec.model,
                    rec.repo,
                    rec.agent_id,
                    rec.usage.prompt_tokens,
                    rec.usage.completion_tokens,
                    rec.usage.cached_tokens,
                    rec.cost_usd,
                ),
            )

    def total_since(self, since: datetime) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE ts >= ?",
                (since.isoformat(),),
            ).fetchone()
        return float(row[0])

    def by_backend(self, since: datetime) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT backend, COALESCE(SUM(cost_usd), 0)
                FROM cost_records WHERE ts >= ? GROUP BY backend
                """,
                (since.isoformat(),),
            ).fetchall()
        return {backend: float(cost) for backend, cost in rows}

    def month_to_date(self) -> float:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.total_since(start)
