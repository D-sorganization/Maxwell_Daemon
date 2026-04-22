"""Model routing primitives for benchmark-aware profile selection.

This module is intentionally small and isolated so issue-driven rollout can
land incrementally without destabilizing existing task execution paths.
"""

from .models import (
    ActionRisk,
    Capability,
    CostClass,
    DeploymentKind,
    ModelProfile,
    ModelRoutingPolicy,
    TaskType,
)
from .router import ModelRouteDecision, ProfileRejection, select_profile

__all__ = [
    "ActionRisk",
    "Capability",
    "CostClass",
    "DeploymentKind",
    "ModelProfile",
    "ModelRouteDecision",
    "ModelRoutingPolicy",
    "ProfileRejection",
    "TaskType",
    "select_profile",
]
