from __future__ import annotations

import pytest
from pydantic import ValidationError

from maxwell_daemon.model_routing.models import (
    ActionRisk,
    Capability,
    CostClass,
    DeploymentKind,
    ModelProfile,
    ModelRoutingPolicy,
    TaskType,
)
from maxwell_daemon.model_routing.router import select_profile


def _profile(
    profile_id: str,
    *,
    deployment: DeploymentKind = DeploymentKind.LOCAL,
    cost: CostClass = CostClass.CHEAP,
    enabled: bool = True,
    capabilities: set[Capability] | None = None,
    max_risk: ActionRisk = ActionRisk.EXTERNAL_SIDE_EFFECT,
) -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        provider="ollama" if deployment is DeploymentKind.LOCAL else "openai",
        model="m",
        deployment=deployment,
        enabled=enabled,
        capabilities=capabilities or {Capability.STRUCTURED_OUTPUT},
        cost_class=cost,
        max_allowed_action_risk=max_risk,
    )


def test_profile_rejects_raw_secret_like_endpoint_ref() -> None:
    with pytest.raises(ValidationError, match="named reference"):
        ModelProfile(
            id="local.devstral",
            provider="ollama",
            model="devstral:24b",
            endpoint_ref="sk-abc123",
        )


def test_policy_requires_suite_and_threshold_together() -> None:
    with pytest.raises(ValidationError, match="set together"):
        ModelRoutingPolicy(task_type=TaskType.ISSUE_TRIAGE, required_benchmark_suite="suite-only")


def test_selects_cheapest_eligible_profile() -> None:
    policy = ModelRoutingPolicy(
        task_type=TaskType.ISSUE_TRIAGE,
        required_capabilities={Capability.STRUCTURED_OUTPUT},
        max_cost_class=CostClass.PREMIUM,
    )
    decision = select_profile(
        profiles=[
            _profile("remote.expensive", deployment=DeploymentKind.REMOTE, cost=CostClass.PREMIUM),
            _profile("local.cheap", deployment=DeploymentKind.LOCAL, cost=CostClass.CHEAP),
        ],
        policy=policy,
    )
    assert decision.selected_profile_id == "local.cheap"
    assert decision.candidate_profile_ids == ("local.cheap", "remote.expensive")


def test_disabled_profiles_are_never_selected() -> None:
    policy = ModelRoutingPolicy(task_type=TaskType.ISSUE_TRIAGE)
    decision = select_profile(
        profiles=[
            _profile("local.disabled", enabled=False),
            _profile("remote.enabled", deployment=DeploymentKind.REMOTE, cost=CostClass.STANDARD),
        ],
        policy=policy,
    )
    assert decision.selected_profile_id == "remote.enabled"
    assert any(r.profile_id == "local.disabled" for r in decision.rejections)


def test_benchmark_gate_escalates_to_qualified_remote() -> None:
    policy = ModelRoutingPolicy(
        task_type=TaskType.ISSUE_TRIAGE,
        required_benchmark_suite="maxwell.context_recall",
        min_benchmark_score=0.80,
    )
    local = _profile("local.devstral", deployment=DeploymentKind.LOCAL, cost=CostClass.FREE_LOCAL)
    remote = _profile("remote.frontier", deployment=DeploymentKind.REMOTE, cost=CostClass.STANDARD)
    decision = select_profile(
        profiles=[local, remote],
        policy=policy,
        benchmark_scores={
            ("local.devstral", "maxwell.context_recall"): 0.50,
            ("remote.frontier", "maxwell.context_recall"): 0.92,
        },
    )
    assert decision.selected_profile_id == "remote.frontier"
    assert any(
        r.profile_id == "local.devstral" and r.reason == "benchmark_below_threshold"
        for r in decision.rejections
    )


def test_rejects_profiles_that_cannot_handle_required_risk() -> None:
    policy = ModelRoutingPolicy(
        task_type=TaskType.PATCH_GENERATION,
        required_action_risk=ActionRisk.COMMAND_EXECUTION,
    )
    decision = select_profile(
        profiles=[
            _profile("local.safe-only", max_risk=ActionRisk.REPO_WRITE),
            _profile(
                "remote.exec",
                deployment=DeploymentKind.REMOTE,
                max_risk=ActionRisk.COMMAND_EXECUTION,
            ),
        ],
        policy=policy,
    )
    assert decision.selected_profile_id == "remote.exec"
    assert any(
        r.profile_id == "local.safe-only" and r.reason == "action_risk_too_high_for_profile"
        for r in decision.rejections
    )
