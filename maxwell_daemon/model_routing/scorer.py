from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from maxwell_daemon.model_routing.models import ModelProfile
from maxwell_daemon.model_routing.signature import TaskSignature


@dataclass(slots=True, frozen=True)
class RoutingScore:
    candidate_id: str
    composite_score: float
    est_cost_usd: float
    capability_gap: bool
    speed_score: float
    quality_score: float
    health_penalty: float
    policy_fit: float


class RoutingScorer:
    """Scores candidate models against a task signature and routing policy."""

    def __init__(
        self,
        preset: Literal["economy", "balanced", "premium", "local-first"] = "balanced",
    ) -> None:
        self.preset = preset

    def score(self, candidate: ModelProfile, task: TaskSignature) -> RoutingScore:
        # Capability gap is a hard fail
        capability_gap = not task.required_capabilities.issubset(candidate.capabilities)

        # Baseline cost calculation
        # If it's local, cost is ~0
        est_cost_usd = 0.0
        if candidate.deployment.value == "remote":
            # Very coarse cost estimate based on cost_class
            est_cost_usd = (
                float(candidate.cost_class.value)
                * 0.001
                * (task.estimated_input_tokens + task.estimated_output_tokens)
                / 1000.0
            )

        # Simple heuristics for now
        speed_score = 1.0 if candidate.deployment.value == "local" else 0.5
        quality_score = (
            float(candidate.cost_class.value) / 3.0
        )  # Assumes cost roughly maps to quality
        health_penalty = 0.0  # Placeholder for live health

        # Preset policy weights
        w_cost, w_quality, w_speed = 0.0, 0.0, 0.0
        if self.preset == "economy":
            w_cost, w_quality, w_speed = -10.0, 1.0, 1.0
        elif self.preset == "premium":
            w_cost, w_quality, w_speed = -1.0, 10.0, 1.0
        elif self.preset == "local-first":
            if candidate.deployment.value == "local":
                w_cost, w_quality, w_speed = 0.0, 0.0, 10.0
            else:
                w_cost, w_quality, w_speed = -1.0, 1.0, 0.0
        else:  # balanced
            w_cost, w_quality, w_speed = -5.0, 5.0, 2.0

        policy_fit = (
            (w_cost * est_cost_usd)
            + (w_quality * quality_score)
            + (w_speed * speed_score)
        )

        # Penalty if it doesn't match expected latency
        if task.expected_latency.value == "interactive" and speed_score < 0.8:
            policy_fit -= 5.0

        # Hard penalty for capability gap
        composite_score = -9999.0 if capability_gap else policy_fit - health_penalty

        return RoutingScore(
            candidate_id=candidate.id,
            composite_score=composite_score,
            est_cost_usd=est_cost_usd,
            capability_gap=capability_gap,
            speed_score=speed_score,
            quality_score=quality_score,
            health_penalty=health_penalty,
            policy_fit=policy_fit,
        )
