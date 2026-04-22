"""Typed models for benchmark-aware model routing."""

from __future__ import annotations

from enum import Enum, IntEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TaskType(str, Enum):
    ISSUE_TRIAGE = "issue_triage"
    PATCH_GENERATION = "patch_generation"
    FINAL_REVIEW = "final_review"


class Capability(str, Enum):
    STRUCTURED_OUTPUT = "structured_output"
    PATCH_GENERATION = "patch_generation"
    LONG_CONTEXT = "long_context"


class DeploymentKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class CostClass(IntEnum):
    FREE_LOCAL = 0
    CHEAP = 1
    STANDARD = 2
    PREMIUM = 3


class ActionRisk(IntEnum):
    READ_ONLY = 0
    REPO_WRITE = 1
    COMMAND_EXECUTION = 2
    EXTERNAL_SIDE_EFFECT = 3


class ModelProfile(BaseModel):
    """A concrete model/backend profile used by routing policy."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=3)
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    deployment: DeploymentKind = DeploymentKind.LOCAL
    enabled: bool = True
    capabilities: set[Capability] = Field(default_factory=set)
    cost_class: CostClass = CostClass.STANDARD
    max_allowed_action_risk: ActionRisk = ActionRisk.REPO_WRITE
    endpoint_ref: str | None = None

    @field_validator("endpoint_ref")
    @classmethod
    def _reject_raw_secrets(cls, v: str | None) -> str | None:
        if v is None:
            return None
        lowered = v.lower()
        secret_markers = ("sk-", "ghp_", "bearer ", "api_key=", "token=")
        if any(marker in lowered for marker in secret_markers):
            raise ValueError("endpoint_ref must be a named reference, not a raw secret value")
        return v


class ModelRoutingPolicy(BaseModel):
    """Routing constraints for a given task category."""

    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    required_capabilities: set[Capability] = Field(default_factory=set)
    max_cost_class: CostClass = CostClass.PREMIUM
    required_action_risk: ActionRisk = ActionRisk.READ_ONLY
    required_benchmark_suite: str | None = None
    min_benchmark_score: float | None = Field(default=None, ge=0.0, le=1.0)
    allow_local_models: bool = True
    allow_remote_models: bool = True

    @model_validator(mode="after")
    def _validate_benchmark_gate(self) -> ModelRoutingPolicy:
        has_suite = self.required_benchmark_suite is not None
        has_min = self.min_benchmark_score is not None
        if has_suite != has_min:
            raise ValueError(
                "required_benchmark_suite and min_benchmark_score must be set together"
            )
        return self
