"""Cost ledger — persistence and aggregation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maxwell_daemon.backends import TokenUsage
from maxwell_daemon.core import CostLedger, CostRecord


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "ledger.db")


def _record(
    cost: float = 0.10,
    backend: str = "claude",
    repo: str = "UpstreamDrift",
    ts: datetime | None = None,
) -> CostRecord:
    return CostRecord(
        ts=ts or datetime.now(timezone.utc),
        backend=backend,
        model="claude-sonnet-4-6",
        usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        cost_usd=cost,
        repo=repo,
        agent_id="test-agent",
    )


class TestLedger:
    def test_empty_totals_zero(self, ledger: CostLedger) -> None:
        assert ledger.month_to_date() == 0.0

    def test_record_and_total(self, ledger: CostLedger) -> None:
        ledger.record(_record(cost=0.10))
        ledger.record(_record(cost=0.25))
        assert ledger.month_to_date() == pytest.approx(0.35)

    def test_by_backend(self, ledger: CostLedger) -> None:
        ledger.record(_record(cost=1.00, backend="claude"))
        ledger.record(_record(cost=0.50, backend="claude"))
        ledger.record(_record(cost=0.00, backend="ollama"))
        start = datetime.now(timezone.utc) - timedelta(days=1)
        by = ledger.by_backend(start)
        assert by["claude"] == pytest.approx(1.50)
        assert by["ollama"] == pytest.approx(0.0)

    def test_excludes_prior_periods(self, ledger: CostLedger) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=60)
        ledger.record(_record(cost=99.0, ts=old))
        ledger.record(_record(cost=0.5))
        assert ledger.month_to_date() == pytest.approx(0.5)

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        db = tmp_path / "l.db"
        a = CostLedger(db)
        a.record(_record(cost=1.23))
        b = CostLedger(db)
        assert b.month_to_date() == pytest.approx(1.23)

    def test_close_terminates_connection(self, ledger: CostLedger) -> None:
        """close() should not raise and should close the sqlite connection."""
        ledger.record(_record(cost=0.01))
        ledger.close()
        # After close, attempting to use the connection should raise
        with pytest.raises(sqlite3.ProgrammingError):
            ledger._conn.execute("SELECT 1")


class TestAsyncAPI:
    async def test_arecord_and_atotal(self, tmp_path: Path) -> None:
        ledger = CostLedger(tmp_path / "ledger.db")
        await ledger.arecord(_record(cost=0.42))
        start = datetime.now(timezone.utc) - timedelta(days=1)
        total = await ledger.atotal_since(start)
        assert total == pytest.approx(0.42)
        ledger.close()

    async def test_aby_backend(self, tmp_path: Path) -> None:
        ledger = CostLedger(tmp_path / "ledger.db")
        await ledger.arecord(_record(cost=1.00, backend="openai"))
        await ledger.arecord(_record(cost=0.50, backend="claude"))
        start = datetime.now(timezone.utc) - timedelta(days=1)
        by = await ledger.aby_backend(start)
        assert by["openai"] == pytest.approx(1.00)
        assert by["claude"] == pytest.approx(0.50)
        ledger.close()
