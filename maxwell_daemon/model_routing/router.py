"""Deterministic profile selection for a routing policy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .models import ModelProfile, ModelRoutingPolicy


@dataclass(slots=True, frozen=True)
class ProfileRejection:
    profile_id: str
    reason: str


@dataclass(slots=True, frozen=True)
class ModelRouteDecision:
    task_type: str
    selected_profile_id: str | None
    candidate_profile_ids: tuple[str, ...]
    rejections: tuple[ProfileRejection, ...]


def select_profile(
    *,
    profiles: list[ModelProfile],
    policy: ModelRoutingPolicy,
    benchmark_scores: Mapping[tuple[str, str], float] | None = None,
) -> ModelRouteDecision:
    """Return the cheapest qualifying profile with deterministic tie-breaking."""
    rejections: list[ProfileRejection] = []
    accepted: list[ModelProfile] = []
    scores = benchmark_scores or {}

    for profile in profiles:
        if not profile.enabled:
            rejections.append(ProfileRejection(profile.id, "profile_disabled"))
            continue
        if profile.deployment.value == "local" and not policy.allow_local_models:
            rejections.append(ProfileRejection(profile.id, "local_models_not_allowed"))
            continue
        if profile.deployment.value == "remote" and not policy.allow_remote_models:
            rejections.append(ProfileRejection(profile.id, "remote_models_not_allowed"))
            continue
        if not policy.required_capabilities.issubset(profile.capabilities):
            rejections.append(ProfileRejection(profile.id, "missing_required_capabilities"))
            continue
        if profile.cost_class > policy.max_cost_class:
            rejections.append(ProfileRejection(profile.id, "cost_class_exceeds_policy"))
            continue
        if profile.max_allowed_action_risk < policy.required_action_risk:
            rejections.append(ProfileRejection(profile.id, "action_risk_too_high_for_profile"))
            continue
        if policy.required_benchmark_suite is not None and policy.min_benchmark_score is not None:
            key = (profile.id, policy.required_benchmark_suite)
            if key not in scores:
                rejections.append(ProfileRejection(profile.id, "missing_required_benchmark"))
                continue
            score = scores[key]
            if not math.isfinite(score):
                rejections.append(ProfileRejection(profile.id, "benchmark_not_finite"))
                continue
            if score < policy.min_benchmark_score:
                rejections.append(ProfileRejection(profile.id, "benchmark_below_threshold"))
                continue
        accepted.append(profile)

    accepted.sort(key=lambda p: (int(p.cost_class), p.id))
    selected_id = accepted[0].id if accepted else None

    return ModelRouteDecision(
        task_type=policy.task_type.value,
        selected_profile_id=selected_id,
        candidate_profile_ids=tuple(p.id for p in accepted),
        rejections=tuple(rejections),
    )
