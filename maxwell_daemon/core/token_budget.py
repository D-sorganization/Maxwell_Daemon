"""Per-task token budgeting and allocation.

Builds on the cost ledger to enable agents to make informed decisions about
model selection and task batching based on remaining budget and estimated costs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.core.ledger import CostLedger

__all__ = [
    "EstimatedCost",
    "TokenBudgetAllocator",
    "TokenBudgetStatus",
]


@dataclass(slots=True, frozen=True)
class EstimatedCost:
    """Estimated token cost for a task."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    model: str
    confidence: Literal["high", "medium", "low"]


@dataclass(slots=True, frozen=True)
class TokenBudgetStatus:
    """Current token budget state for a task."""

    remaining_budget_usd: float
    monthly_spent_usd: float
    monthly_limit_usd: float | None
    utilization_percent: float
    can_afford_model: dict[str, bool]  # model_name -> can_afford
    recommended_model: str  # cheapest model that fits
    status: Literal["ok", "tight", "exhausted"]


_TOKEN_COST_ESTIMATES = {
    # Format: (prompt_cost_per_1k, completion_cost_per_1k)
    # Anthropic pricing as of 2026-04
    "claude-opus-4-7": (0.015, 0.075),  # $15/$75 per 1M
    "claude-sonnet-4-6": (0.003, 0.015),  # $3/$15 per 1M
    "claude-haiku-4-5": (0.0008, 0.004),  # $0.80/$4 per 1M
    # OpenAI pricing as of 2026-04
    "gpt-4-turbo": (0.01, 0.03),  # $10/$30 per 1M
    "gpt-4o": (0.005, 0.015),  # $5/$15 per 1M
    "gpt-4o-mini": (0.00015, 0.0006),  # $0.15/$0.60 per 1M
    # Local models (zero cost)
    "ollama:*": (0.0, 0.0),
}


class TokenBudgetAllocator:
    """Allocate token budgets and recommend models based on cost and availability."""

    def __init__(self, config: MaxwellDaemonConfig, ledger: CostLedger) -> None:
        self._config = config
        self._ledger = ledger

    def estimate_cost(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> EstimatedCost:
        """Estimate the USD cost of a task given model and token counts.

        Estimates are based on published pricing; actual costs may vary.
        Returns an EstimatedCost object with detailed cost breakdown.
        """
        key = model
        if model.startswith("ollama:"):
            key = "ollama:*"

        prompt_cost, completion_cost = _TOKEN_COST_ESTIMATES.get(
            key, (0.0, 0.0)  # unknown model; assume free (local)
        )

        cost = (prompt_tokens * prompt_cost / 1000) + (
            completion_tokens * completion_cost / 1000
        )
        return EstimatedCost(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost,
            model=model,
            confidence="medium",
        )

    def check_budget(self, *, now: datetime | None = None) -> TokenBudgetStatus:
        """Check current budget status and recommend a model.

        Returns a status object with remaining budget, utilization, and model recommendations.
        """
        now = now or datetime.now(timezone.utc)
        limit = self._config.budget.monthly_limit_usd

        if limit is None:
            return TokenBudgetStatus(
                remaining_budget_usd=float("inf"),
                monthly_spent_usd=0.0,
                monthly_limit_usd=None,
                utilization_percent=0.0,
                can_afford_model={},
                recommended_model="claude-opus-4-7",  # default to best
                status="ok",
            )

        spent = self._ledger.month_to_date(now=now)
        remaining = max(0.0, limit - spent)
        utilization = (spent / limit * 100) if limit > 0 else 0.0

        # Check affordability: typical task sizes
        # Conservative estimate: 10k prompt tokens, 2k completion tokens per task
        can_afford = {}
        for model in ["claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"]:
            est = self.estimate_cost(model=model, prompt_tokens=10000, completion_tokens=2000)
            can_afford[model] = est.cost_usd < remaining

        # Recommend cheapest model that fits
        if can_afford.get("claude-haiku-4-5"):
            recommended = "claude-haiku-4-5"
            status = "ok"
        elif can_afford.get("claude-sonnet-4-6"):
            recommended = "claude-sonnet-4-6"
            status = "tight"
        else:
            recommended = "claude-opus-4-7"
            status = "exhausted" if remaining < 1.0 else "tight"

        return TokenBudgetStatus(
            remaining_budget_usd=remaining,
            monthly_spent_usd=spent,
            monthly_limit_usd=limit,
            utilization_percent=utilization,
            can_afford_model=can_afford,
            recommended_model=recommended,
            status=status,
        )
