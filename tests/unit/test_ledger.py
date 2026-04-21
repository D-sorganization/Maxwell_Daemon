"""Cost ledger — persistence and aggregation."""

from __future__ import annotations

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

    @pytest.mark.asyncio
    async def test_async_methods_match_sync_queries(self, ledger: CostLedger) -> None:
        start = datetime.now(timezone.utc) - timedelta(days=1)

        await ledger.arecord(_record(cost=0.75, backend="claude"))
        await ledger.arecord(_record(cost=0.25, backend="openai"))

        assert await ledger.atotal_since(start) == pytest.approx(1.0)
        by_backend = await ledger.aby_backend(start)
        assert by_backend["claude"] == pytest.approx(0.75)
        assert by_backend["openai"] == pytest.approx(0.25)

    def test_forecast_zero_when_no_spend(self, ledger: CostLedger) -> None:
        now = datetime(2026, 4, 1, 0, 0, 30, tzinfo=timezone.utc)
        assert ledger.forecast_month_end(now=now) == 0.0

    def test_forecast_month_end_extrapolates_from_elapsed_fraction(
        self, ledger: CostLedger
    ) -> None:
        now = datetime(2026, 4, 15, 0, 0, 0, tzinfo=timezone.utc)
        ledger.record(_record(cost=15.0, ts=now))

        assert ledger.forecast_month_end(now=now) == pytest.approx(15.0 * 30 / 14)

    def test_close_releases_connection(self, ledger: CostLedger) -> None:
        ledger.close()
        with pytest.raises(Exception, match="closed"):
            ledger.month_to_date()
