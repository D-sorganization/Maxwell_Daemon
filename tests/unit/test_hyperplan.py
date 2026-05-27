from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.core.hyperplan import (
    PLAN_SYSTEM_PROMPT_SUFFIX,
    RECONCILE_SYSTEM_PROMPT,
    REVIEW_SYSTEM_PROMPT,
    REVISE_SYSTEM_PROMPT,
    HyperPlanExecutor,
    HyperPlanStrategy,
    PlanStep,
    ReconcileStep,
    ReviewStep,
    ReviseStep,
    cross_review_strategy,
    ensemble_strategy,
    group_by_depth,
    peer_review_strategy,
    standard_strategy,
    validate_strategy,
)
from maxwell_daemon.core.roles import Role, RolePlayer

# ============================================================================
# Mock Backend & Helpers
# ============================================================================


class MockBackend(ILLMBackend):
    name = "mock_backend"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.responses: dict[str, str] = {}
        self.sleep_duration: float = 0.0

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BackendResponse:
        system_content = next((m.content for m in messages if m.role is MessageRole.SYSTEM), "")
        user_content = next((m.content for m in messages if m.role is MessageRole.USER), "")
        self.calls.append((system_content, user_content))

        # Check if we should sleep to simulate network latency
        if self.sleep_duration > 0.0:
            await asyncio.sleep(self.sleep_duration)

        # Match response based on system prompt patterns to avoid false matches on user content
        if 'mode="reconcile"' in system_content:
            content = self.responses.get("reconcile", "Final reconciled plan.")
        elif 'mode="review"' in system_content:
            content = self.responses.get("review", "Peer review.")
        elif "revision_mode" in system_content:
            content = self.responses.get("revise", "Revised plan.")
        elif PLAN_SYSTEM_PROMPT_SUFFIX in system_content:
            content = self.responses.get("Planner", "Initial plan.")
        else:
            content = "Default mock response"

        return BackendResponse(
            content=content,
            finish_reason="stop",
            usage=TokenUsage(),
            model=model,
            backend=self.name,
            raw={},
        )

    def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> Any:
        pass

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(supports_tool_use=True)


def create_mock_player(name: str, backend: MockBackend) -> RolePlayer:
    role = Role(name=name, system_prompt=f"System prompt for {name}")
    return RolePlayer(role=role, backend=backend, model="mock-model")


# ============================================================================
# Tests
# ============================================================================


def test_strategy_validation_valid() -> None:
    backend = MockBackend()
    planner = create_mock_player("Planner", backend)
    reviewer = create_mock_player("Reviewer", backend)
    reconciler = create_mock_player("Reconciler", backend)

    # Standard
    strategy = standard_strategy(planner)
    assert not validate_strategy(strategy)

    # Ensemble
    strategy = ensemble_strategy([planner, planner], reconciler)
    assert not validate_strategy(strategy)

    # Peer Review
    strategy = peer_review_strategy(planner, reviewer)
    assert not validate_strategy(strategy)

    # Cross Review
    strategy = cross_review_strategy(planner, reviewer, reconciler)
    assert not validate_strategy(strategy)


def test_strategy_validation_invalid() -> None:
    backend = MockBackend()
    planner = create_mock_player("Planner", backend)

    # Duplicate IDs
    step1 = PlanStep(id="plan_0", role_player=planner)
    step2 = PlanStep(id="plan_0", role_player=planner)
    strategy = HyperPlanStrategy(
        id="duplicate",
        name="Duplicate",
        description="Duplicate",
        steps=[step1, step2],
        terminal_step_id="plan_0",
    )
    errors = validate_strategy(strategy)
    assert any("Duplicate step IDs" in err for err in errors)

    # Unknown terminal step
    strategy = HyperPlanStrategy(
        id="no_terminal",
        name="No Terminal",
        description="No Terminal",
        steps=[step1],
        terminal_step_id="plan_x",
    )
    errors = validate_strategy(strategy)
    assert any('Terminal step "plan_x" not found' in err for err in errors)

    # Cycle detection
    step_a = PlanStep(id="plan_a", role_player=planner, inputs=["plan_b"])
    step_b = PlanStep(id="plan_b", role_player=planner, inputs=["plan_a"])
    strategy = HyperPlanStrategy(
        id="cycle",
        name="Cycle",
        description="Cycle",
        steps=[step_a, step_b],
        terminal_step_id="plan_a",
    )
    errors = validate_strategy(strategy)
    assert any("Strategy contains a cycle" in err for err in errors)


def test_step_specific_validation() -> None:
    backend = MockBackend()
    planner = create_mock_player("Planner", backend)

    # Plan step with inputs
    step = PlanStep(id="plan_0", role_player=planner, inputs=["some_input"])
    strategy = HyperPlanStrategy(
        id="plan_inputs",
        name="Plan Inputs",
        description="Plan Inputs",
        steps=[step],
        terminal_step_id="plan_0",
    )
    errors = validate_strategy(strategy)
    assert any('Plan step "plan_0" must have no inputs' in err for err in errors)

    # Review step without inputs or with multiple inputs
    step_plan = PlanStep(id="plan_a", role_player=planner)
    step_review = ReviewStep(id="review_b", role_player=planner, inputs=[])
    strategy = HyperPlanStrategy(
        id="review_inputs",
        name="Review Inputs",
        description="Review Inputs",
        steps=[step_plan, step_review],
        terminal_step_id="review_b",
    )
    errors = validate_strategy(strategy)
    assert any('Review step "review_b" must have exactly 1 input' in err for err in errors)


def test_grouping_by_depth() -> None:
    backend = MockBackend()
    p = create_mock_player("Planner", backend)
    r = create_mock_player("Reconciler", backend)

    strategy = cross_review_strategy(p, p, r)
    layers = group_by_depth(strategy)

    assert len(layers) == 3
    # Depth 0: plans
    assert {s.id for s in layers[0]} == {"plan_a", "plan_b"}
    # Depth 1: reviews
    assert {s.id for s in layers[1]} == {"review_a_of_b", "review_b_of_a"}
    # Depth 2: reconcile
    assert {s.id for s in layers[2]} == {"reconcile_0"}


def test_prompt_generation() -> None:
    backend = MockBackend()
    p = create_mock_player("Planner", backend)
    r = create_mock_player("Reviewer", backend)

    # Plan Step
    plan_step = PlanStep("plan_0", p)
    sys, user = plan_step.build_prompt("Task desc", {})
    assert PLAN_SYSTEM_PROMPT_SUFFIX in sys
    assert "System prompt for Planner" in sys
    assert user == "Task desc"

    # Review Step
    review_step = ReviewStep("review_0", r, inputs=["plan_0"])
    sys, user = review_step.build_prompt("Task desc", {"plan_0": "Original plan text"})
    assert sys == REVIEW_SYSTEM_PROMPT
    assert '<plan_to_review id="plan_0">' in user
    assert "Original plan text" in user

    # Revise Step
    revise_step = ReviseStep("revise_0", p, inputs=["review_0"], resume_step_id="plan_0")
    sys, user = revise_step.build_prompt(
        "Task desc", {"plan_0": "Original plan text", "review_0": "Review feedback"}
    )
    assert sys == REVISE_SYSTEM_PROMPT
    assert '<original_plan id="plan_0">' in user
    assert "Original plan text" in user
    assert '<peer_review from="review_0">' in user
    assert "Review feedback" in user

    # Reconcile Step
    reconcile_step = ReconcileStep("reconcile_0", p, inputs=["plan_0", "review_0"])
    sys, user, _mapping = reconcile_step.build_reconcile_prompt(
        task_description="Task desc",
        step_results={"plan_0": "Plan text", "review_0": "Review text"},
        step_primitives={"plan_0": "plan", "review_0": "review"},
        reviewed_plan_map={"review_0": "plan_0"},
        shuffle=False,
    )
    assert sys == RECONCILE_SYSTEM_PROMPT
    assert "Task desc" in user
    assert '<plan id="A">' in user
    assert 'reviews="Plan A"' in user
    assert "Plan text" in user
    assert "Review text" in user


@pytest.mark.asyncio
async def test_parallel_execution() -> None:
    backend = MockBackend()
    backend.sleep_duration = 0.1
    backend.responses = {
        "Planner": "Initial plan.",
        "review": "Peer review.",
        "reconcile": "Final reconciled plan.",
    }

    planner = create_mock_player("Planner", backend)
    reviewer = create_mock_player("Reviewer", backend)
    reconciler = create_mock_player("Reconciler", backend)

    strategy = cross_review_strategy(planner, reviewer, reconciler)
    executor = HyperPlanExecutor(strategy)

    start_time = time.monotonic()
    result = await executor.execute("Implement task X")
    duration = time.monotonic() - start_time

    # With sleep of 0.1s:
    # Layer 0 (plan_a, plan_b) -> 0.1s
    # Layer 1 (review_a_of_b, review_b_of_a) -> 0.1s
    # Layer 2 (reconcile_0) -> 0.1s
    # Total time should be around 0.3s. If execution was sequential, it would be 5 * 0.1s = 0.5s.
    assert duration < 0.45
    assert result == "Final reconciled plan."
    assert len(executor.step_results) == 5
    assert executor.step_results["plan_a"] == "Initial plan."
    assert executor.step_results["reconcile_0"] == "Final reconciled plan."
