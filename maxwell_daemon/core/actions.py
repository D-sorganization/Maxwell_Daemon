"""Action ledger models for reviewable agent side effects."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ActionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    APPLIED = "applied"
    FAILED = "failed"
    REVERTED = "reverted"
    SKIPPED = "skipped"


class ActionKind(str, Enum):
    FILE_WRITE = "file_write"
    FILE_EDIT = "file_edit"
    DIFF_APPLY = "diff_apply"
    COMMAND = "command"
    PR_CREATE = "pr_create"
    PR_UPDATE = "pr_update"
    CHECK_RUN = "check_run"
    EXTERNAL_CALL = "external_call"


class ActionRiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Action(BaseModel):
    """Durable description of one proposed or applied side effect."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    work_item_id: str | None = Field(default=None, min_length=1)
    kind: ActionKind
    status: ActionStatus = ActionStatus.PROPOSED
    summary: str = Field(..., min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    inverse_payload: dict[str, Any] | None = None
    risk_level: ActionRiskLevel = ActionRiskLevel.MEDIUM
    requires_approval: bool = True
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    result_artifact_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _decision_metadata_matches_status(self) -> Action:
        if self.approved_by is not None and self.approved_at is None:
            raise ValueError("approved_at is required when approved_by is set")
        if self.rejected_by is not None and self.rejected_at is None:
            raise ValueError("rejected_at is required when rejected_by is set")
        return self


TERMINAL_ACTION_STATUSES = frozenset(
    {
        ActionStatus.APPLIED,
        ActionStatus.FAILED,
        ActionStatus.REJECTED,
        ActionStatus.REVERTED,
        ActionStatus.SKIPPED,
    }
)

VALID_ACTION_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset(
        {ActionStatus.APPROVED, ActionStatus.REJECTED, ActionStatus.SKIPPED}
    ),
    ActionStatus.APPROVED: frozenset({ActionStatus.RUNNING, ActionStatus.SKIPPED}),
    ActionStatus.RUNNING: frozenset({ActionStatus.APPLIED, ActionStatus.FAILED}),
    ActionStatus.APPLIED: frozenset({ActionStatus.REVERTED}),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.REJECTED: frozenset(),
    ActionStatus.REVERTED: frozenset(),
    ActionStatus.SKIPPED: frozenset(),
}


def validate_action_transition(current: ActionStatus, new: ActionStatus) -> None:
    """Raise ValueError when an action lifecycle transition is not allowed."""

    if new not in VALID_ACTION_TRANSITIONS[current]:
        raise ValueError(f"invalid action transition from {current.value!r} to {new.value!r}")
