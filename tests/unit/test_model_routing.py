from __future__ import annotations

import math

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
            _profile(
                "remote.expensive",
                deployment=DeploymentKind.REMOTE,
                cost=CostClass.PREMIUM,
            ),
            _profile("local.cheap", deployment=DeploymentKind.LOCAL, cost=CostClass.CHEAP),
        ],
        policy=policy,
    )
    assert decision.plan is not None
    assert decision.plan.primary == "local.cheap"
    assert "remote.expensive" in decision.plan.fallbacks


def test_disabled_profiles_are_never_selected() -> None:
    policy = ModelRoutingPolicy(task_type=TaskType.ISSUE_TRIAGE)
    decision = select_profile(
        profiles=[
            _profile("local.disabled", enabled=False),
            _profile(
                "remote.enabled",
                deployment=DeploymentKind.REMOTE,
                cost=CostClass.STANDARD,
            ),
        ],
        policy=policy,
    )
    assert decision.plan is not None
    assert decision.plan.primary == "remote.enabled"
    assert any(r.profile_id == "local.disabled" for r in decision.rejections)


def test_policy_rejections_report_each_failed_gate() -> None:
    deployment_policy = ModelRoutingPolicy(
        task_type=TaskType.PATCH_GENERATION,
        allow_local_models=False,
        allow_remote_models=False,
    )
    deployment_decision = select_profile(
        profiles=[
            _profile("local.blocked", deployment=DeploymentKind.LOCAL),
            _profile("remote.blocked", deployment=DeploymentKind.REMOTE),
        ],
        policy=deployment_policy,
    )

    capability_policy = ModelRoutingPolicy(
        task_type=TaskType.PATCH_GENERATION,
        required_capabilities={Capability.PATCH_GENERATION},
        max_cost_class=CostClass.STANDARD,
        required_benchmark_suite="maxwell.patch_quality",
        min_benchmark_score=0.75,
    )
    capability_decision = select_profile(
        profiles=[
            _profile(
                "missing.capability",
                capabilities={Capability.STRUCTURED_OUTPUT},
            ),
            _profile(
                "too.expensive",
                cost=CostClass.PREMIUM,
                capabilities={Capability.PATCH_GENERATION},
            ),
            _profile("missing.benchmark", capabilities={Capability.PATCH_GENERATION}),
        ],
        policy=capability_policy,
    )

    assert deployment_decision.plan is None
    assert capability_decision.plan is None
    rejections = {
        r.profile_id: r.reason
        for r in (*deployment_decision.rejections, *capability_decision.rejections)
    }
    assert rejections == {
        "local.blocked": "local_models_not_allowed",
        "remote.blocked": "remote_models_not_allowed",
        "missing.capability": "missing_required_capabilities",
        "too.expensive": "cost_class_exceeds_policy",
        "missing.benchmark": "missing_required_benchmark",
    }


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
    assert decision.plan is not None
    assert decision.plan.primary == "remote.frontier"
    assert any(
        r.profile_id == "local.devstral" and r.reason == "benchmark_below_threshold"
        for r in decision.rejections
    )


@pytest.mark.parametrize("bad_score", [math.nan, math.inf, -math.inf])
def test_benchmark_gate_rejects_non_finite_scores(bad_score: float) -> None:
    policy = ModelRoutingPolicy(
        task_type=TaskType.ISSUE_TRIAGE,
        required_benchmark_suite="maxwell.context_recall",
        min_benchmark_score=0.80,
    )
    decision = select_profile(
        profiles=[
            _profile("local.bad", deployment=DeploymentKind.LOCAL, cost=CostClass.FREE_LOCAL),
            _profile("remote.good", deployment=DeploymentKind.REMOTE, cost=CostClass.STANDARD),
        ],
        policy=policy,
        benchmark_scores={
            ("local.bad", "maxwell.context_recall"): bad_score,
            ("remote.good", "maxwell.context_recall"): 0.92,
        },
    )
    assert decision.plan is not None
    assert decision.plan.primary == "remote.good"
    assert any(
        r.profile_id == "local.bad" and r.reason == "benchmark_not_finite"
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
    assert decision.plan is not None
    assert decision.plan.primary == "remote.exec"
    assert any(
        r.profile_id == "local.safe-only" and r.reason == "action_risk_too_high_for_profile"
        for r in decision.rejections
    )
