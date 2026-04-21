"""Cost ledger — records every request and enforces budgets.

Backed by SQLite so the history survives restarts and can be queried from the
dashboard. Kept small on purpose — this is the audit trail, not a hot-path
dependency.

Connection model
----------------
A single ``sqlite3.Connection`` is opened at construction time and kept open
for the lifetime of the ``CostLedger`` object.  WAL journal mode allows
concurrent readers alongside the single writer so dashboard queries don't
block recording.

Threading / async safety
------------------------
All SQLite operations are dispatched through ``asyncio.run_in_executor`` (the
default thread pool) to avoid blocking the event loop.  A single
``threading.Lock`` guards the persistent connection so only one thread
accesses it at a time — this is safe because we never call the lock from
async code directly, only from thread-pool workers.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from maxwell_daemon.backends.base import TokenUsage

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
        # Persistent connection — avoids per-operation open/close overhead.
        self._conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        # threading.Lock guards the connection itself; only used from thread-pool
        # workers dispatched via run_in_executor, never held across await points.
        self._lock = threading.Lock()

    # ── Sync helpers (called inside thread-pool workers) ─────────────────────

    def _record_sync(self, rec: CostRecord) -> None:
        with self._lock:
            self._conn.execute(
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

    def _total_since_sync(self, since: datetime) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE ts >= ?",
                (since.isoformat(),),
            ).fetchone()
        return float(row[0])

    def _by_backend_sync(self, since: datetime) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT backend, COALESCE(SUM(cost_usd), 0)
                FROM cost_records WHERE ts >= ? GROUP BY backend
                """,
                (since.isoformat(),),
            ).fetchall()
        return {backend: float(cost) for backend, cost in rows}

    # ── Sync public API (used by non-async callers such as the dashboard) ─────

    def record(self, rec: CostRecord) -> None:
        self._record_sync(rec)

    def total_since(self, since: datetime) -> float:
        return self._total_since_sync(since)

    def by_backend(self, since: datetime) -> dict[str, float]:
        return self._by_backend_sync(since)

    # ── Async public API (used by the agent loop and request handlers) ────────

    async def arecord(self, rec: CostRecord) -> None:
        """Non-blocking version of :meth:`record` for use in async code."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._record_sync, rec)

    async def atotal_since(self, since: datetime) -> float:
        """Non-blocking version of :meth:`total_since` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._total_since_sync, since)

    async def aby_backend(self, since: datetime) -> dict[str, float]:
        """Non-blocking version of :meth:`by_backend` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._by_backend_sync, since)

    # ── Derived helpers ───────────────────────────────────────────────────────

    def month_to_date(self, *, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.total_since(start)

    def forecast_month_end(self, *, now: datetime | None = None) -> float:
        """Linear extrapolation from MTD spend to the end of the calendar month.

        Guards against divide-by-near-zero at the start of a month by clamping
        the elapsed fraction to at least one minute's worth of the month. That
        means the day-1 forecast is a wild extrapolation, but it's finite.
        """
        import calendar

        now = now or datetime.now(timezone.utc)
        spent = self.month_to_date(now=now)
        if spent <= 0:
            return 0.0

        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_days = calendar.monthrange(now.year, now.month)[1]
        month_total_seconds = month_days * 86400
        elapsed_seconds = (now - start).total_seconds()
        # Clamp to avoid crazy-large forecasts in the first minute of a month.
        min_elapsed = 60.0
        fraction = max(elapsed_seconds, min_elapsed) / month_total_seconds
        return spent / fraction

    def close(self) -> None:
        """Close the persistent connection. Call on daemon shutdown."""
        with self._lock:
            self._conn.close()
