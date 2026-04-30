"""Deterministic profile selection for a routing policy."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from .models import ModelProfile, ModelRoutingPolicy
from .scorer import RoutingScore, RoutingScorer
from .signature import TaskSignature


@dataclass(slots=True, frozen=True)
class ProfileRejection:
    profile_id: str
    reason: str


@dataclass(slots=True, frozen=True)
class RoutingPlan:
    primary: str
    fallbacks: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class ModelRouteDecision:
    task_type: str
    plan: RoutingPlan | None
    scores: tuple[RoutingScore, ...]
    rejections: tuple[ProfileRejection, ...]


def select_profile(  # noqa: C901
    *,
    profiles: list[ModelProfile],
    policy: ModelRoutingPolicy,
    task_signature: TaskSignature | None = None,
    scorer: RoutingScorer | None = None,
    benchmark_scores: Mapping[tuple[str, str], float] | None = None,
) -> ModelRouteDecision:
    """Return the best profile and fallbacks based on score."""
    rejections: list[ProfileRejection] = []
    accepted: list[ModelProfile] = []
    scores_dict = benchmark_scores or {}

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

        # Benchmark filtering
        if policy.required_benchmark_suite is not None and policy.min_benchmark_score is not None:
            key = (profile.id, policy.required_benchmark_suite)
            if key not in scores_dict:
                rejections.append(ProfileRejection(profile.id, "missing_required_benchmark"))
                continue
            score = scores_dict[key]
            if not math.isfinite(score):
                rejections.append(ProfileRejection(profile.id, "benchmark_not_finite"))
                continue
            if score < policy.min_benchmark_score:
                rejections.append(ProfileRejection(profile.id, "benchmark_below_threshold"))
                continue
        accepted.append(profile)

    if not accepted:
        return ModelRouteDecision(
            task_type=policy.task_type.value,
            plan=None,
            scores=(),
            rejections=tuple(rejections),
        )

    if scorer and task_signature:
        scored_candidates = []
        for profile in accepted:
            route_score = scorer.score(profile, task_signature)
            if route_score.capability_gap:
                rejections.append(ProfileRejection(profile.id, "missing_required_capabilities"))
            else:
                scored_candidates.append(route_score)

        scored_candidates.sort(key=lambda s: s.composite_score, reverse=True)

        if not scored_candidates:
            return ModelRouteDecision(
                task_type=policy.task_type.value,
                plan=None,
                scores=(),
                rejections=tuple(rejections),
            )

        primary = scored_candidates[0].candidate_id
        fallbacks = tuple(s.candidate_id for s in scored_candidates[1:])

        return ModelRouteDecision(
            task_type=policy.task_type.value,
            plan=RoutingPlan(primary=primary, fallbacks=fallbacks),
            scores=tuple(scored_candidates),
            rejections=tuple(rejections),
        )
    else:
        # Fallback to simple sorting if no scorer/signature provided
        accepted.sort(key=lambda p: (int(p.cost_class), p.id))
        primary = accepted[0].id
        fallbacks = tuple(p.id for p in accepted[1:])

        return ModelRouteDecision(
            task_type=policy.task_type.value,
            plan=RoutingPlan(primary=primary, fallbacks=fallbacks),
            scores=(),
            rejections=tuple(rejections),
        )
