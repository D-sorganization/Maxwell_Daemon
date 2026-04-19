"""Budget enforcement — soft alerts and hard stops based on cost ledger state."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from conductor.backends import TokenUsage
from conductor.config import BudgetConfig
from conductor.core import CostLedger, CostRecord
from conductor.core.budget import (
    BudgetCheck,
    BudgetEnforcer,
    BudgetExceededError,
)


def _spend(ledger: CostLedger, amount: float) -> None:
    ledger.record(
        CostRecord(
            ts=datetime.now(timezone.utc),
            backend="claude",
            model="claude-sonnet-4-6",
            usage=TokenUsage(total_tokens=1000),
            cost_usd=amount,
        )
    )


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "l.db")


class TestBudgetEnforcer:
    def test_no_limit_returns_ok(self, ledger: CostLedger) -> None:
        enforcer = BudgetEnforcer(BudgetConfig(), ledger)
        check = enforcer.check()
        assert check.status == "ok"
        assert check.limit_usd is None

    def test_under_threshold_is_ok(self, ledger: CostLedger) -> None:
        _spend(ledger, 10.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger)
        assert enforcer.check().status == "ok"

    def test_crosses_alert_threshold(self, ledger: CostLedger) -> None:
        _spend(ledger, 75.0)
        enforcer = BudgetEnforcer(
            BudgetConfig(monthly_limit_usd=100.0, alert_thresholds=[0.75, 0.9, 1.0]),
            ledger,
        )
        check = enforcer.check()
        assert check.status == "alert"
        assert check.threshold_crossed == 0.75

    def test_at_hard_limit_returns_exceeded(self, ledger: CostLedger) -> None:
        _spend(ledger, 100.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger)
        check = enforcer.check()
        assert check.status == "exceeded"

    def test_over_limit_returns_exceeded(self, ledger: CostLedger) -> None:
        _spend(ledger, 150.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger)
        assert enforcer.check().status == "exceeded"

    def test_require_under_raises_when_hard_stop_enabled(self, ledger: CostLedger) -> None:
        _spend(ledger, 100.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0, hard_stop=True), ledger)
        with pytest.raises(BudgetExceededError):
            enforcer.require_under_budget()

    def test_require_under_permissive_when_hard_stop_disabled(self, ledger: CostLedger) -> None:
        _spend(ledger, 200.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0, hard_stop=False), ledger)
        enforcer.require_under_budget()  # should not raise

    def test_check_reports_utilisation(self, ledger: CostLedger) -> None:
        _spend(ledger, 40.0)
        enforcer = BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger)
        check = enforcer.check()
        assert check.utilisation == pytest.approx(0.40)
        assert check.spent_usd == pytest.approx(40.0)

    def test_highest_crossed_threshold_wins(self, ledger: CostLedger) -> None:
        _spend(ledger, 92.0)
        enforcer = BudgetEnforcer(
            BudgetConfig(monthly_limit_usd=100.0, alert_thresholds=[0.75, 0.9, 1.0]),
            ledger,
        )
        check = enforcer.check()
        assert check.threshold_crossed == 0.9


class TestBudgetCheck:
    def test_is_dataclass_like(self) -> None:
        c = BudgetCheck(
            status="alert",
            spent_usd=75.0,
            limit_usd=100.0,
            utilisation=0.75,
            threshold_crossed=0.75,
        )
        assert c.status == "alert"
        assert c.limit_usd == 100.0
