"""Tests for token budget accounting and model selection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from maxwell_daemon.config import BudgetConfig, MaxwellDaemonConfig
from maxwell_daemon.core.ledger import CostLedger, CostRecord
from maxwell_daemon.core.token_budget import (
    TokenBudgetAllocator,
)


@pytest.fixture
def mock_config() -> Any:
    """Create a mock config with a $100 monthly budget."""
    return Mock(spec=MaxwellDaemonConfig, budget=BudgetConfig(monthly_limit_usd=100.0))


@pytest.fixture
def mock_ledger(tmp_path: Path) -> CostLedger:
    """Create a real cost ledger for testing."""
    return CostLedger(tmp_path / "test_ledger.db")


def test_estimate_cost_anthropic_models(mock_config: Any, mock_ledger: CostLedger) -> None:
    """Test cost estimation for Anthropic models."""
    allocator = TokenBudgetAllocator(mock_config, mock_ledger)

    # Haiku: cheapest
    haiku_cost = allocator.estimate_cost(
        model="claude-haiku-4-5", prompt_tokens=10000, completion_tokens=2000
    )
    assert haiku_cost.model == "claude-haiku-4-5"
    assert haiku_cost.total_tokens == 12000
    assert haiku_cost.cost_usd > 0
    assert haiku_cost.cost_usd < 0.10  # should be very cheap

    # Sonnet: mid-tier
    sonnet_cost = allocator.estimate_cost(
        model="claude-sonnet-4-6", prompt_tokens=10000, completion_tokens=2000
    )
    assert sonnet_cost.cost_usd > haiku_cost.cost_usd

    # Opus: most expensive
    opus_cost = allocator.estimate_cost(
        model="claude-opus-4-7", prompt_tokens=10000, completion_tokens=2000
    )
    assert opus_cost.cost_usd > sonnet_cost.cost_usd


def test_estimate_cost_unknown_model(mock_config: Any, mock_ledger: CostLedger) -> None:
    """Test that unknown models default to free (local)."""
    allocator = TokenBudgetAllocator(mock_config, mock_ledger)

    unknown_cost = allocator.estimate_cost(
        model="custom-local-model", prompt_tokens=100000, completion_tokens=50000
    )
    assert unknown_cost.cost_usd == 0.0  # local/unknown models are free


def test_check_budget_ok_status(mock_config: Any, mock_ledger: CostLedger) -> None:
    """Test budget check when well under limit."""
    allocator = TokenBudgetAllocator(mock_config, mock_ledger)

    status = allocator.check_budget()
    assert status.status == "ok"
    assert status.monthly_spent_usd == 0.0
    assert status.monthly_limit_usd == 100.0
    assert status.utilization_percent == 0.0
    assert status.remaining_budget_usd == 100.0
    assert status.recommended_model == "claude-haiku-4-5"


def test_check_budget_with_spending(mock_config: Any, mock_ledger: CostLedger) -> None:
    """Test budget check when some money has been spent."""
    from maxwell_daemon.backends import TokenUsage

    now = datetime.now(timezone.utc)
    record = CostRecord(
        ts=now,
        backend="anthropic",
        model="claude-opus-4-7",
        usage=TokenUsage(prompt_tokens=1000, completion_tokens=500),
        cost_usd=50.0,
    )
    mock_ledger.record(record)

    allocator = TokenBudgetAllocator(mock_config, mock_ledger)
    status = allocator.check_budget()

    assert status.status == "ok"
    assert status.monthly_spent_usd == 50.0
    assert status.remaining_budget_usd == 50.0
    assert status.utilization_percent == 50.0


def test_check_budget_tight_status(mock_config: Any, mock_ledger: CostLedger) -> None:
    """Test budget check when approaching limit."""
    from maxwell_daemon.backends import TokenUsage

    now = datetime.now(timezone.utc)
    record = CostRecord(
        ts=now,
        backend="anthropic",
        model="claude-opus-4-7",
        usage=TokenUsage(prompt_tokens=10000, completion_tokens=5000),
        cost_usd=80.0,
    )
    mock_ledger.record(record)

    allocator = TokenBudgetAllocator(mock_config, mock_ledger)
    status = allocator.check_budget()

    assert status.status == "tight"
    assert status.utilization_percent == 80.0
    assert status.recommended_model == "claude-sonnet-4-6"


def test_check_budget_no_limit(mock_ledger: CostLedger) -> None:
    """Test budget check when no limit is set."""
    unlimited_config: Any = Mock(
        spec=MaxwellDaemonConfig,
        budget=BudgetConfig(monthly_limit_usd=None),
    )
    allocator = TokenBudgetAllocator(unlimited_config, mock_ledger)

    status = allocator.check_budget()
    assert status.status == "ok"
    assert status.monthly_limit_usd is None
    assert status.utilization_percent == 0.0
    assert status.remaining_budget_usd == float("inf")
