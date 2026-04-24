"""Shared test helpers, factories, and assertions.

Reduces boilerplate across test files by providing:
- Factory functions for common objects
- Custom pytest assertions
- Mock/patch utilities
- Test data generators
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from maxwell_daemon.config import BudgetConfig, MaxwellDaemonConfig
from maxwell_daemon.core.ledger import CostRecord
from maxwell_daemon.daemon.runner import Task, TaskKind, TaskStatus

__all__ = [
    "ConfigFactory",
    "CostRecordFactory",
    "TaskFactory",
    "assert_cost_record_valid",
    "assert_task_state_valid",
]


class TaskFactory:
    """Create test Task objects with sensible defaults."""

    @staticmethod
    def prompt(
        *,
        task_id: str | None = None,
        prompt: str = "test prompt",
        priority: int = 100,
        backend: str | None = None,
        model: str | None = None,
        **kwargs: Any,
    ) -> Task:
        """Create a prompt task with sensible defaults."""
        return Task(
            id=task_id or f"task-{uuid4().hex[:8]}",
            prompt=prompt,
            kind=TaskKind.PROMPT,
            priority=priority,
            backend=backend,
            model=model,
            status=TaskStatus.QUEUED,
            **kwargs,
        )

    @staticmethod
    def issue(
        *,
        task_id: str | None = None,
        issue_repo: str = "owner/repo",
        issue_number: int = 123,
        issue_mode: str = "plan",
        priority: int = 100,
        **kwargs: Any,
    ) -> Task:
        """Create an issue-based task."""
        return Task(
            id=task_id or f"task-{uuid4().hex[:8]}",
            prompt="",  # Issue tasks get prompt from issue content
            kind=TaskKind.ISSUE,
            issue_repo=issue_repo,
            issue_number=issue_number,
            issue_mode=issue_mode,
            priority=priority,
            status=TaskStatus.QUEUED,
            **kwargs,
        )


class CostRecordFactory:
    """Create test CostRecord objects with sensible defaults."""

    @staticmethod
    def anthropic(
        *,
        model: str = "claude-sonnet-4-6",
        prompt_tokens: int = 1000,
        completion_tokens: int = 500,
        cost_usd: float | None = None,
        **kwargs: Any,
    ) -> CostRecord:
        """Create a cost record for Anthropic models."""
        if cost_usd is None:
            # Rough estimate: 3/$15 per 1M for Sonnet
            cost_usd = (prompt_tokens * 0.003 / 1000) + (completion_tokens * 0.015 / 1000)

        return CostRecord(
            backend="anthropic",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            **kwargs,
        )

    @staticmethod
    def openai(
        *,
        model: str = "gpt-4o",
        prompt_tokens: int = 1000,
        completion_tokens: int = 500,
        cost_usd: float | None = None,
        **kwargs: Any,
    ) -> CostRecord:
        """Create a cost record for OpenAI models."""
        if cost_usd is None:
            # Rough estimate: 5/$15 per 1M for gpt-4o
            cost_usd = (prompt_tokens * 0.005 / 1000) + (completion_tokens * 0.015 / 1000)

        return CostRecord(
            backend="openai",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost_usd,
            **kwargs,
        )

    @staticmethod
    def local(
        *,
        model: str = "ollama:llama2",
        prompt_tokens: int = 1000,
        completion_tokens: int = 500,
        **kwargs: Any,
    ) -> CostRecord:
        """Create a zero-cost record for local models."""
        return CostRecord(
            backend="ollama",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=0.0,
            **kwargs,
        )


class ConfigFactory:
    """Create test configuration objects."""

    @staticmethod
    def default() -> MaxwellDaemonConfig:
        """Create a minimal valid config for testing."""
        return MaxwellDaemonConfig.default()

    @staticmethod
    def with_budget(monthly_limit_usd: float = 100.0) -> MaxwellDaemonConfig:
        """Create a config with a specific budget limit."""
        config = MaxwellDaemonConfig.default()
        config.budget = BudgetConfig(monthly_limit_usd=monthly_limit_usd)
        return config


def assert_cost_record_valid(record: CostRecord) -> None:
    """Assert that a CostRecord meets basic validity constraints.

    Raises AssertionError if any constraint is violated.
    """
    assert record.prompt_tokens >= 0, "prompt_tokens must be non-negative"
    assert record.completion_tokens >= 0, "completion_tokens must be non-negative"
    assert record.total_tokens == record.prompt_tokens + record.completion_tokens, (
        "total_tokens must equal sum of prompt+completion"
    )
    assert record.cost_usd >= 0, "cost_usd must be non-negative"
    assert record.backend, "backend must be non-empty"
    assert record.model, "model must be non-empty"


def assert_task_state_valid(task: Task) -> None:
    """Assert that a Task is in a consistent state.

    Raises AssertionError if any constraint is violated.
    """
    assert task.id, "task.id must be non-empty"
    assert task.prompt or task.kind == TaskKind.ISSUE, "prompt is required for PROMPT tasks"
    assert 0 <= task.priority <= 200, "priority must be in [0, 200]"
    assert task.status in TaskStatus, "status must be a valid TaskStatus"
    if task.kind == TaskKind.ISSUE:
        assert task.issue_repo, "issue_repo required for ISSUE tasks"
        assert task.issue_number is not None, "issue_number required for ISSUE tasks"
