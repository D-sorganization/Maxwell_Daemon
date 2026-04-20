"""Budget enforcement built on top of the cost ledger.

Separated from the ledger itself so the ledger stays a pure record-of-truth;
this module adds *policy* on top of the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from maxwell_daemon.config import BudgetConfig
from maxwell_daemon.core.ledger import CostLedger

__all__ = ["BudgetCheck", "BudgetEnforcer", "BudgetExceededError"]


class BudgetExceededError(RuntimeError):
    """Raised when a hard-stop budget has been exceeded."""


@dataclass(slots=True, frozen=True)
class BudgetCheck:
    status: Literal["ok", "alert", "exceeded"]
    spent_usd: float
    limit_usd: float | None
    utilisation: float  # spent / limit, or 0.0 if limit is None
    threshold_crossed: float | None = None
    forecast_usd: float | None = None  # linear month-end extrapolation


class BudgetEnforcer:
    """Compute the current budget state and enforce hard limits."""

    def __init__(self, config: BudgetConfig, ledger: CostLedger) -> None:
        self._config = config
        self._ledger = ledger

    def check(self, *, now: datetime | None = None) -> BudgetCheck:
        spent = self._ledger.month_to_date(now=now)
        forecast = self._ledger.forecast_month_end(now=now)
        limit = self._config.monthly_limit_usd

        if limit is None:
            return BudgetCheck(
                status="ok",
                spent_usd=spent,
                limit_usd=None,
                utilisation=0.0,
                forecast_usd=forecast,
            )

        utilisation = spent / limit if limit > 0 else 0.0

        if utilisation >= 1.0:
            return BudgetCheck(
                status="exceeded",
                spent_usd=spent,
                limit_usd=limit,
                utilisation=utilisation,
                threshold_crossed=1.0,
                forecast_usd=forecast,
            )

        crossed = max(
            (t for t in self._config.alert_thresholds if utilisation >= t),
            default=None,
        )
        return BudgetCheck(
            status="alert" if crossed is not None else "ok",
            spent_usd=spent,
            limit_usd=limit,
            utilisation=utilisation,
            threshold_crossed=crossed,
            forecast_usd=forecast,
        )

    def require_under_budget(self) -> None:
        """Raise ``BudgetExceededError`` if hard_stop is set and we're over limit."""
        if not self._config.hard_stop:
            return
        check = self.check()
        if check.status == "exceeded":
            raise BudgetExceededError(
                f"Monthly budget exceeded: ${check.spent_usd:.2f} / ${check.limit_usd:.2f} "
                f"({check.utilisation:.0%}). Set hard_stop=false to allow overrun."
            )
