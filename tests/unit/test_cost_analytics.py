"""Tests for cost analytics and reporting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from maxwell_daemon.backends import TokenUsage
from maxwell_daemon.core.cost_analytics import CostAnalytics
from maxwell_daemon.core.ledger import CostLedger, CostRecord


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    """Create a test cost ledger."""
    return CostLedger(tmp_path / "ledger.db")


def test_empty_period_summary(ledger: CostLedger) -> None:
    """Test summary when no costs recorded."""
    analytics = CostAnalytics(ledger)

    now = datetime.now(timezone.utc)
    summary = analytics.summarize_period(start=now, end=now)

    assert summary.total_cost_usd == 0.0
    assert summary.cost_by_backend == {}


def test_period_summary_with_costs(ledger: CostLedger) -> None:
    """Test summary with recorded costs."""
    analytics = CostAnalytics(ledger)

    now = datetime.now(timezone.utc)
    record1 = CostRecord(
        ts=now,
        backend="anthropic",
        model="claude-opus-4-7",
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=500),
        cost_usd=0.05,
    )
    record2 = CostRecord(
        ts=now,
        backend="openai",
        model="gpt-4o",
        usage=TokenUsage(prompt_tokens=2000, completion_tokens=1000),
        cost_usd=0.10,
    )

    ledger.record(record1)
    ledger.record(record2)

    summary = analytics.summarize_period(start=now - timedelta(seconds=1), end=now + timedelta(seconds=1))

    assert summary.total_cost_usd == 0.15
    assert summary.cost_by_backend == {"anthropic": 0.05, "openai": 0.10}


def test_cache_hit_rate(ledger: CostLedger) -> None:
    """Test cache hit rate calculation."""
    analytics = CostAnalytics(ledger)

    # Placeholder test - actual implementation would compute from ledger data
    rate = analytics.get_cache_hit_rate()
    assert isinstance(rate, float)
    assert 0.0 <= rate <= 1.0
