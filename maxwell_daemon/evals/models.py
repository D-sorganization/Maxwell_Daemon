"""Typed models for deterministic agent workflow evaluations."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SUPPORTED_SCORING_PROFILE_IDS = frozenset({"default"})


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class EvalSourceType(str, Enum):
    GITHUB_ISSUE = "github_issue"
    GAAI_STORY = "gaai_story"
    MANUAL_TASK = "manual_task"
    FAILING_TEST = "failing_test"
    SECURITY_REVIEW = "security_review"
    DOCS_TASK = "docs_task"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvalStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERRORED = "errored"
    SKIPPED = "skipped"


class FailureCategory(str, Enum):
    NONE = "none"
    CONTEXT = "context"
    PLANNING = "planning"
    TOOL_POLICY = "tool_policy"
    MODEL_QUALITY = "model_quality"
    IMPLEMENTATION = "implementation"
    TESTS = "tests"
    INFRASTRUCTURE = "infrastructure"
    UNKNOWN = "unknown"


class ScoringProfile(BaseModel):
    """Deterministic scoring profile shared by CLI, API, and reports."""

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str
    weights: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_weights(self) -> ScoringProfile:
        total = sum(self.weights.values())
        if not 99.9 <= total <= 100.1:
            raise ValueError(f"scoring profile weights must sum to 100, got {total}")
        if any(value < 0 for value in self.weights.values()):
            raise ValueError("scoring profile weights must be non-negative")
        return self


class EvalScenario(BaseModel):
    """Versioned task definition for a deterministic agent evaluation."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    description: str
    source_type: EvalSourceType
    fixture_repo_ref: str
    task_prompt: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    required_checks: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    time_budget_seconds: int = Field(gt=0)
    token_budget: int | None = Field(default=None, gt=0)
    expected_artifacts: list[str] = Field(default_factory=list)
    scoring_profile_id: str = "default"
    requires_tests: bool = True
    requires_approval: bool = False

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not value or not value.replace("-", "").replace("_", "").isalnum():
            raise ValueError("scenario id must be non-empty and slug-like")
        return value

    @field_validator("fixture_repo_ref")
    @classmethod
    def validate_fixture_ref(cls, value: str) -> str:
        if not value.startswith("fixture://"):
            raise ValueError("fixture_repo_ref must use fixture:// refs by default")
        return value

    @field_validator("expected_artifacts")
    @classmethod
    def validate_artifact_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"artifact path must be relative and contained: {value}")
        return values

    @field_validator("scoring_profile_id")
    @classmethod
    def validate_scoring_profile(cls, value: str) -> str:
        if value not in SUPPORTED_SCORING_PROFILE_IDS:
            raise ValueError(f"unknown scoring profile: {value}")
        return value

    @model_validator(mode="after")
    def validate_tool_policy(self) -> EvalScenario:
        overlap = set(self.allowed_tools) & set(self.disallowed_tools)
        if overlap:
            raise ValueError(f"tools cannot be both allowed and disallowed: {sorted(overlap)}")
        return self


class EvalRun(BaseModel):
    """Durable run metadata for one or more scenarios."""

    model_config = ConfigDict(extra="forbid")

    id: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    scenario_ids: list[str]
    daemon_version: str
    git_commit: str | None = None
    model_profile_ids: list[str] = Field(default_factory=list)
    routing_policy_id: str | None = None
    external_agent_adapter_ids: list[str] = Field(default_factory=list)
    status: EvalStatus = EvalStatus.ERRORED
    summary: str = ""
    artifact_refs: list[str] = Field(default_factory=list)


class EvalResult(BaseModel):
    """Per-scenario result with score and evidence references."""

    model_config = ConfigDict(extra="forbid")

    id: str
    eval_run_id: str
    scenario_id: str
    status: EvalStatus
    score_total: float = Field(ge=0, le=100)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    checks_passed: list[str] = Field(default_factory=list)
    checks_failed: list[str] = Field(default_factory=list)
    diff_summary: str = ""
    trace_id: str | None = None
    cost_summary: dict[str, float] = Field(default_factory=dict)
    failure_category: FailureCategory = FailureCategory.NONE
    artifact_refs: list[str] = Field(default_factory=list)
    unrelated_file_changes: list[str] = Field(default_factory=list)
    missing_tests: bool = False
    disallowed_tool_invocations: list[str] = Field(default_factory=list)
    approval_required: bool = False
    approval_granted: bool = False
    workspace_path: str | None = None


class EvalComparisonItem(BaseModel):
    """Score delta for a scenario present in two eval runs."""

    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    base_score: float
    candidate_score: float
    delta: float
    classification: str


class EvalComparison(BaseModel):
    """Regression/improvement summary between two eval runs."""

    model_config = ConfigDict(extra="forbid")

    base_run_id: str
    candidate_run_id: str
    items: list[EvalComparisonItem]

    @property
    def regressions(self) -> list[EvalComparisonItem]:
        return [item for item in self.items if item.classification == "regression"]

    @property
    def improvements(self) -> list[EvalComparisonItem]:
        return [item for item in self.items if item.classification == "improvement"]
