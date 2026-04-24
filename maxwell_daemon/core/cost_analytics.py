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

        total_cost = self._ledger.total_since(start, end=end)
        cost_by_backend = self._ledger.by_backend(start, end=end)

        calls, cached, prompt, completion = self._ledger.cache_metrics_raw(start, end=end)
        cache_hit_rate = 0.0
        if prompt > 0:
            cache_hit_rate = cached / prompt

        cache_metrics = CacheHitMetrics(
            total_calls=calls,
            cached_tokens=cached,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_hit_rate=cache_hit_rate,
        )

        return CostSummary(
            period_start=start,
            period_end=end,
            total_cost_usd=total_cost,
            call_count=calls,  # Tracked by cache_metrics_raw
            average_cost_per_call=(total_cost / calls) if calls > 0 else 0.0,
            cost_by_backend=cost_by_backend,
            cost_by_model={},  # Would require DB query
            cache_metrics=cache_metrics,
        )

    def get_cache_hit_rate(
        self, *, since: datetime | None = None, end: datetime | None = None
    ) -> float:
        """Calculate cache hit rate over a period.

        Cache hit rate = cached_tokens / prompt_tokens.
        Returns a value between 0.0 and 1.0.
        """
        if since is None:
            since = datetime.min.replace(tzinfo=timezone.utc)
        calls, cached, prompt, completion = self._ledger.cache_metrics_raw(since, end=end)
        if prompt > 0:
            return cached / prompt
        return 0.0
