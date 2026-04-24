"""Tests for pre-flight cost estimation and workspace cost rollup."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from maxwell_daemon.backends import TokenUsage
from maxwell_daemon.core.cost_estimator import (
    CostEstimate,
    WorkspaceCostRollup,
    estimate_task_cost,
    workspace_cost_rollup,
)
from maxwell_daemon.core.ledger import CostLedger, CostRecord

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "est.db")


_MESSAGES = [
    {"role": "user", "content": "Summarize the following Python file in one paragraph."},
    {"role": "assistant", "content": "Sure, I will summarize it now."},
]


# ---------------------------------------------------------------------------
# estimate_task_cost
# ---------------------------------------------------------------------------


class TestEstimateTaskCost:
    def test_returns_cost_estimate_type(self) -> None:
        result = estimate_task_cost("claude", "claude-sonnet-4-6", _MESSAGES)
        assert isinstance(result, CostEstimate)

    def test_prompt_tokens_derived_from_content_length(self) -> None:
        result = estimate_task_cost("claude", "claude-sonnet-4-6", _MESSAGES)
        total_chars = sum(len(m["content"]) for m in _MESSAGES)
        expected_tokens = max(1, int(total_chars / 4.0))
        assert result.estimated_prompt_tokens == expected_tokens

    def test_completion_tokens_use_ratio(self) -> None:
        result = estimate_task_cost(
            "claude", "claude-sonnet-4-6", _MESSAGES, expected_completion_ratio=1.0
        )
        assert result.estimated_completion_tokens == result.estimated_prompt_tokens

    def test_cost_is_nonzero_for_known_model(self) -> None:
        result = estimate_task_cost("claude", "claude-sonnet-4-6", _MESSAGES)
        assert result.estimated_cost_usd > 0.0

    def test_more_expensive_model_costs_more(self) -> None:
        cheap = estimate_task_cost("claude", "claude-haiku-4-5", _MESSAGES)
        expensive = estimate_task_cost("claude", "claude-opus-4-7", _MESSAGES)
        assert expensive.estimated_cost_usd > cheap.estimated_cost_usd

    def test_free_provider_returns_zero_cost(self) -> None:
        result = estimate_task_cost("ollama", "llama3.1", _MESSAGES)
        assert result.is_free is True
        assert result.estimated_cost_usd == 0.0

    def test_unknown_model_falls_back_to_zero(self) -> None:
        result = estimate_task_cost("claude", "no-such-model-xyz", _MESSAGES)
        assert result.estimated_cost_usd == 0.0

    def test_empty_messages_does_not_crash(self) -> None:
        result = estimate_task_cost("openai", "gpt-4o", [])
        assert result.estimated_prompt_tokens >= 1

    def test_message_objects_with_content_attr(self) -> None:
        class Msg:
            def __init__(self, content: str) -> None:
                self.content = content

        msgs = [Msg("Hello world"), Msg("How can I help?")]
        result = estimate_task_cost("openai", "gpt-4o", msgs)
        assert result.estimated_prompt_tokens > 0

    def test_provider_and_model_stored_on_result(self) -> None:
        result = estimate_task_cost("openai", "gpt-4o", _MESSAGES)
        assert result.provider == "openai"
        assert result.model == "gpt-4o"


# ---------------------------------------------------------------------------
# workspace_cost_rollup
# ---------------------------------------------------------------------------


class TestWorkspaceCostRollup:
    def test_returns_rollup_type(self, ledger: CostLedger) -> None:
        result = workspace_cost_rollup(ledger, "ws-abc")
        assert isinstance(result, WorkspaceCostRollup)

    def test_workspace_id_preserved(self, ledger: CostLedger) -> None:
        result = workspace_cost_rollup(ledger, "ws-test-123")
        assert result.workspace_id == "ws-test-123"

    def test_empty_ledger_returns_zero_cost(self, ledger: CostLedger) -> None:
        result = workspace_cost_rollup(ledger, "ws-empty")
        assert result.total_cost_usd == 0.0

    def test_includes_costs_recorded_in_period(self, ledger: CostLedger) -> None:
        now = datetime.now(timezone.utc)
        ledger.record(
            CostRecord(
                ts=now,
                backend="claude",
                model="claude-sonnet-4-6",
                usage=TokenUsage(total_tokens=100),
                cost_usd=0.05,
            )
        )
        result = workspace_cost_rollup(
            ledger, "ws-x", since=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        assert result.total_cost_usd == pytest.approx(0.05)

    def test_period_bounds_respected(self, ledger: CostLedger) -> None:
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        end = datetime(2026, 1, 31, tzinfo=timezone.utc)
        result = workspace_cost_rollup(ledger, "ws-y", since=start, until=end)
        assert result.period_start == start
        assert result.period_end == end
