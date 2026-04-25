"""Governed work item models and transition rules."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"


class WorkItemStatus(str, Enum):
    DRAFT = "draft"
    NEEDS_REFINEMENT = "needs_refinement"
    REFINED = "refined"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


class AcceptanceCriterion(BaseModel):
    id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    verification: str | None = None


class ScopeBoundary(BaseModel):
    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"

    @field_validator("allowed_paths", "denied_paths", "allowed_commands")
    @classmethod
    def _reject_blank_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("scope entries must be non-empty")
        return values


class WorkItem(BaseModel):
    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    repo: str | None = Field(default=None, pattern=REPO_PATTERN)
    source: Literal["manual", "github_issue", "gaai", "api"] = "manual"
    source_url: str | None = None
    status: WorkItemStatus = WorkItemStatus.DRAFT
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    scope: ScopeBoundary = Field(default_factory=ScopeBoundary)
    required_checks: tuple[str, ...] = ()
    priority: int = Field(default=100, ge=0, le=1000)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    task_ids: tuple[str, ...] = ()

    @field_validator("required_checks", "task_ids")
    @classmethod
    def _reject_blank_tuple_entries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("tuple entries must be non-empty")
        return values

    @model_validator(mode="after")
    def _check_contract(self) -> WorkItem:
        if self.status is WorkItemStatus.REFINED and not self.acceptance_criteria:
            raise ValueError("refined work items require at least one acceptance criterion")
        if self.status is WorkItemStatus.IN_PROGRESS and self.started_at is None:
            raise ValueError("in-progress work items require started_at")
        if self.status is WorkItemStatus.DONE and self.completed_at is None:
            raise ValueError("done work items require completed_at")
        return self


_ALLOWED_TRANSITIONS: dict[WorkItemStatus, frozenset[WorkItemStatus]] = {
    WorkItemStatus.DRAFT: frozenset({WorkItemStatus.NEEDS_REFINEMENT}),
    WorkItemStatus.NEEDS_REFINEMENT: frozenset({WorkItemStatus.REFINED, WorkItemStatus.CANCELLED}),
    WorkItemStatus.REFINED: frozenset({WorkItemStatus.IN_PROGRESS, WorkItemStatus.CANCELLED}),
    WorkItemStatus.IN_PROGRESS: frozenset(
        {WorkItemStatus.DONE, WorkItemStatus.BLOCKED, WorkItemStatus.CANCELLED}
    ),
    WorkItemStatus.BLOCKED: frozenset(
        {
            WorkItemStatus.NEEDS_REFINEMENT,
            WorkItemStatus.REFINED,
            WorkItemStatus.CANCELLED,
        }
    ),
    WorkItemStatus.DONE: frozenset(),
    WorkItemStatus.CANCELLED: frozenset(),
}


def validate_transition(current: WorkItemStatus, target: WorkItemStatus) -> None:
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid work item transition: {current.value} -> {target.value}")


def transition_work_item(
    item: WorkItem,
    target: WorkItemStatus,
    *,
    now: datetime | None = None,
) -> WorkItem:
    validate_transition(item.status, target)
    timestamp = now or datetime.now(timezone.utc)
    updates: dict[str, object] = {"status": target, "updated_at": timestamp}
    if target is WorkItemStatus.REFINED and not item.acceptance_criteria:
        raise ValueError("refined work items require at least one acceptance criterion")
    if target is WorkItemStatus.IN_PROGRESS:
        updates["started_at"] = item.started_at or timestamp
    if target in {WorkItemStatus.DONE, WorkItemStatus.CANCELLED}:
        updates["completed_at"] = item.completed_at or timestamp
    return WorkItem.model_validate(item.model_dump() | updates)
