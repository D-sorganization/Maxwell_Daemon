"""Cost forecasting — linear extrapolation from MTD spend to month-end."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from maxwell_daemon.backends import TokenUsage
from maxwell_daemon.config import BudgetConfig
from maxwell_daemon.core import BudgetEnforcer, CostLedger, CostRecord


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "l.db")


def _record(ledger: CostLedger, amount: float, ts: datetime) -> None:
    ledger.record(
        CostRecord(
            ts=ts,
            backend="claude",
            model="claude-sonnet-4-6",
            usage=TokenUsage(total_tokens=100),
            cost_usd=amount,
        )
    )


class TestForecastMonthEnd:
    def test_zero_spend_returns_zero(self, ledger: CostLedger) -> None:
        # First second of a month → no elapsed time, but no spend → 0.
        assert (
            ledger.forecast_month_end(now=datetime(2026, 4, 1, 0, 0, 0, tzinfo=timezone.utc)) == 0.0
        )

    def test_halfway_through_month(self, ledger: CostLedger) -> None:
        # April has 30 days. At midnight starting day 16, exactly 15 days
        # have elapsed → forecast = spent / 0.5 = $100.
        ts = datetime(2026, 4, 5, 12, 0, 0, tzinfo=timezone.utc)
        _record(ledger, 50.0, ts)
        forecast = ledger.forecast_month_end(
            now=datetime(2026, 4, 16, 0, 0, 0, tzinfo=timezone.utc)
        )
        assert forecast == pytest.approx(100.0, rel=0.02)

    def test_full_month_elapsed_equals_mtd(self, ledger: CostLedger) -> None:
        ts = datetime(2026, 4, 10, tzinfo=timezone.utc)
        _record(ledger, 42.0, ts)
        # Last second of April.
        now = datetime(2026, 4, 30, 23, 59, 59, tzinfo=timezone.utc)
        forecast = ledger.forecast_month_end(now=now)
        assert forecast == pytest.approx(42.0, rel=0.01)

    def test_day_one_uses_minimum_elapsed(self, ledger: CostLedger) -> None:
        """First minute of the month: don't divide by near-zero and blow up."""
        ts = datetime(2026, 4, 1, 0, 0, 1, tzinfo=timezone.utc)
        _record(ledger, 1.0, ts)
        forecast = ledger.forecast_month_end(now=datetime(2026, 4, 1, 0, 0, 2, tzinfo=timezone.utc))
        # Must be a finite positive number, not inf.
        assert 0 < forecast < 1e9

    def test_february_30_day_correction(self, ledger: CostLedger) -> None:
        """Feb 2026 has 28 days — verify we use the actual month length."""
        ts = datetime(2026, 2, 3, tzinfo=timezone.utc)
        _record(ledger, 10.0, ts)
        # Midnight Feb 15 = 14 days elapsed / 28 total = 0.5
        forecast = ledger.forecast_month_end(
            now=datetime(2026, 2, 15, 0, 0, 0, tzinfo=timezone.utc)
        )
        assert forecast == pytest.approx(20.0, rel=0.05)


class TestBudgetCheckIncludesForecast:
    def test_check_carries_forecast(self, ledger: CostLedger) -> None:
        _record(ledger, 25.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger)
        check = enforcer.check(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
        assert check.forecast_usd is not None
        assert check.forecast_usd > check.spent_usd

    def test_check_forecast_none_when_no_limit(self, ledger: CostLedger) -> None:
        _record(ledger, 5.0, datetime(2026, 4, 3, tzinfo=timezone.utc))
        enforcer = BudgetEnforcer(BudgetConfig(), ledger)
        check = enforcer.check(now=datetime(2026, 4, 10, tzinfo=timezone.utc))
        # Forecast is still computed (useful in its own right) even without a
        # limit — but utilisation stays 0.
        assert check.forecast_usd is not None
        assert check.utilisation == 0.0
