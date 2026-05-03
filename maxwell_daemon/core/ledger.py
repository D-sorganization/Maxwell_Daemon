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
import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    def __init__(self, db_path: Path | str, pool_size: int = 5) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._pool_size = pool_size
        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=pool_size)

        # Initialize the DB schema using a temporary connection
        with self._create_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

        for _ in range(pool_size):
            self._pool.put_nowait(self._create_conn())

        # threading.Lock guards writes; reads can happen concurrently.
        self._write_lock = threading.Lock()

        from maxwell_daemon.metrics import MAXWELL_LEDGER_CONNECTIONS_IN_USE

        MAXWELL_LEDGER_CONNECTIONS_IN_USE.set_function(lambda: self._pool_size - self._pool.qsize())

    def _create_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path),
            isolation_level=None,  # autocommit
            check_same_thread=False,
            timeout=30.0,
        )
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _get_conn(self) -> Iterator[sqlite3.Connection]:
        try:
            conn = self._pool.get_nowait()
        except queue.Empty:
            conn = self._create_conn()
        try:
            yield conn
        finally:
            try:
                self._pool.put_nowait(conn)
            except queue.Full:
                conn.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._path), isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # ── Sync helpers (called inside thread-pool workers) ─────────────────────

    def _record_sync(self, rec: CostRecord) -> None:
        with self._write_lock, self._get_conn() as conn:
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

    def _total_since_sync(self, since: datetime, end: datetime | None = None) -> float:
        with self._get_conn() as conn:
            query = "SELECT COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE ts >= ?"
            params = [since.isoformat()]
            if end is not None:
                query += " AND ts < ?"
                params.append(end.isoformat())
            row = conn.execute(query, tuple(params)).fetchone()
        return float(row[0])

    def _by_backend_sync(self, since: datetime, end: datetime | None = None) -> dict[str, float]:
        with self._get_conn() as conn:
            query = "SELECT backend, COALESCE(SUM(cost_usd), 0) FROM cost_records WHERE ts >= ?"
            params = [since.isoformat()]
            if end is not None:
                query += " AND ts < ?"
                params.append(end.isoformat())
            query += " GROUP BY backend"
            rows = conn.execute(query, tuple(params)).fetchall()
        return {backend: float(cost) for backend, cost in rows}

    def _cache_metrics_raw_sync(
        self, since: datetime, end: datetime | None = None
    ) -> tuple[int, int, int, int]:
        with self._get_conn() as conn:
            query = """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(cached_tokens), 0),
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0)
                FROM cost_records WHERE ts >= ?
            """
            params = [since.isoformat()]
            if end is not None:
                query += " AND ts < ?"
                params.append(end.isoformat())
            row = conn.execute(query, tuple(params)).fetchone()
            return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def _token_totals_sync(self) -> TokenUsage:
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0)
                FROM cost_records
                """
            ).fetchone()
        prompt_tokens = int(row[0])
        completion_tokens = int(row[1])
        return TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

    def _token_totals_by_agent_sync(self, agent_ids: set[str]) -> dict[str, TokenUsage]:
        if not agent_ids:
            return {}

        placeholders = ",".join("?" for _ in agent_ids)
        with self._get_conn() as conn:
            rows = conn.execute(
                f"SELECT agent_id, "
                f"COALESCE(SUM(prompt_tokens), 0), "
                f"COALESCE(SUM(completion_tokens), 0) "
                f"FROM cost_records "
                f"WHERE agent_id IN ({placeholders}) "
                f"GROUP BY agent_id",  # nosec B608
                tuple(sorted(agent_ids)),
            ).fetchall()

        totals: dict[str, TokenUsage] = {}
        for agent_id, prompt_tokens, completion_tokens in rows:
            prompt_total = int(prompt_tokens)
            completion_total = int(completion_tokens)
            totals[str(agent_id)] = TokenUsage(
                prompt_tokens=prompt_total,
                completion_tokens=completion_total,
                total_tokens=prompt_total + completion_total,
            )
        return totals

    def _prune_sync(self, older_than_days: int, *, now: datetime | None = None) -> int:
        if older_than_days < 0:
            raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)
        with self._write_lock, self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM cost_records WHERE ts < ?",
                (cutoff.isoformat(),),
            )
        return int(cursor.rowcount)

    # ── Sync public API (used by non-async callers such as the dashboard) ─────

    def record(self, rec: CostRecord) -> None:
        self._record_sync(rec)

    def total_since(self, since: datetime, end: datetime | None = None) -> float:
        return self._total_since_sync(since, end)

    def by_backend(self, since: datetime, end: datetime | None = None) -> dict[str, float]:
        return self._by_backend_sync(since, end)

    def cache_metrics_raw(
        self, since: datetime, end: datetime | None = None
    ) -> tuple[int, int, int, int]:
        return self._cache_metrics_raw_sync(since, end)

    def token_totals(self) -> TokenUsage:
        return self._token_totals_sync()

    def token_totals_by_agent(self, agent_ids: set[str]) -> dict[str, TokenUsage]:
        return self._token_totals_by_agent_sync(agent_ids)

    def prune(self, older_than_days: int, *, now: datetime | None = None) -> int:
        """Delete ledger records older than the retention cutoff."""
        return self._prune_sync(older_than_days, now=now)

    # ── Async public API (used by the agent loop and request handlers) ────────

    async def arecord(self, rec: CostRecord) -> None:
        """Non-blocking version of :meth:`record` for use in async code."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._record_sync, rec)

    async def atotal_since(self, since: datetime, end: datetime | None = None) -> float:
        """Non-blocking version of :meth:`total_since` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._total_since_sync, since, end)

    async def aby_backend(self, since: datetime, end: datetime | None = None) -> dict[str, float]:
        """Non-blocking version of :meth:`by_backend` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._by_backend_sync, since, end)

    async def acache_metrics_raw(
        self, since: datetime, end: datetime | None = None
    ) -> tuple[int, int, int, int]:
        """Non-blocking version of :meth:`cache_metrics_raw` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._cache_metrics_raw_sync, since, end)

    async def aprune(self, older_than_days: int, *, now: datetime | None = None) -> int:
        """Non-blocking version of :meth:`prune` for use in async code."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self._prune_sync(older_than_days, now=now))

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
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except queue.Empty:
                break
