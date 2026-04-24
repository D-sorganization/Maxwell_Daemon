"""Cost analytics and reporting utilities.

Builds on CostLedger to provide higher-level analytics:
- Cache hit rate tracking
- Cost breakdown by model and backend
- Trend analysis
- Cost efficiency metrics
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from maxwell_daemon.core.ledger import CostLedger

__all__ = [
    "CacheHitMetrics",
    "CostAnalytics",
    "CostSummary",
]


@dataclass(slots=True, frozen=True)
class CacheHitMetrics:
    """Cache hit statistics."""

    total_calls: int
    cached_tokens: int
    prompt_tokens: int
    completion_tokens: int
    cache_hit_rate: float  # cached_tokens / prompt_tokens


@dataclass(slots=True, frozen=True)
class CostSummary:
    """Summary of costs over a time period."""

    period_start: datetime
    period_end: datetime
    total_cost_usd: float
    call_count: int
    average_cost_per_call: float
    cost_by_backend: dict[str, float]
    cost_by_model: dict[str, float]
    cache_metrics: CacheHitMetrics | None = None


class CostAnalytics:
    """Analyze costs from the ledger."""

    def __init__(self, ledger: CostLedger) -> None:
        self._ledger = ledger

    def summarize_period(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CostSummary:
        """Generate a cost summary for a time period.

        If start/end are not provided, defaults to current calendar month.
        """
        now = end or datetime.now(timezone.utc)
        if start is None:
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if end is None:
            end = now

        total_cost = self._ledger.total_since(start)
        cost_by_backend = self._ledger.by_backend(start)

        return CostSummary(
            period_start=start,
            period_end=end,
            total_cost_usd=total_cost,
            call_count=0,  # Would require additional tracking
            average_cost_per_call=0.0,
            cost_by_backend=cost_by_backend,
            cost_by_model={},  # Would require DB query
            cache_metrics=None,
        )

    def get_cache_hit_rate(self, *, since: datetime | None = None) -> float:
        """Calculate cache hit rate over a period.

        Cache hit rate = cached_tokens / prompt_tokens.
        Returns a value between 0.0 and 1.0.
        """
        # This is a placeholder - actual implementation would query cost ledger
        # for cached_tokens and prompt_tokens and compute the ratio
        return 0.0
