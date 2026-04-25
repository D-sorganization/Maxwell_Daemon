"""Workspace lifecycle models for isolated task execution."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkspaceStatus(str, Enum):
    CREATING = "creating"
    READY = "ready"
    DIRTY = "dirty"
    COMMITTED = "committed"
    FAILED = "failed"
    ARCHIVED = "archived"
    DELETED = "deleted"


class TaskWorkspace(BaseModel):
    """Durable metadata for one task-owned checkout."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    work_item_id: str | None = Field(default=None, min_length=1)
    repo: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    base_branch: str = Field(..., min_length=1)
    work_branch: str = Field(..., min_length=1)
    status: WorkspaceStatus = WorkspaceStatus.CREATING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    current_head: str | None = Field(default=None, min_length=1)
    base_head: str | None = Field(default=None, min_length=1)
    checkpoint_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _timestamps_are_monotonic(self) -> TaskWorkspace:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if self.last_used_at < self.created_at:
            raise ValueError("last_used_at cannot be before created_at")
        return self


class WorkspaceCheckpoint(BaseModel):
    """Named git-native recovery point for a workspace."""

    id: str = Field(..., min_length=1)
    workspace_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    git_ref: str = Field(..., min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    diff_artifact_id: str | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


ACTIVE_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.CREATING,
        WorkspaceStatus.READY,
        WorkspaceStatus.DIRTY,
    }
)

TERMINAL_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.FAILED,
        WorkspaceStatus.ARCHIVED,
        WorkspaceStatus.DELETED,
    }
)

VALID_WORKSPACE_TRANSITIONS: dict[WorkspaceStatus, frozenset[WorkspaceStatus]] = {
    WorkspaceStatus.CREATING: frozenset({WorkspaceStatus.READY, WorkspaceStatus.FAILED}),
    WorkspaceStatus.READY: frozenset(
        {
            WorkspaceStatus.DIRTY,
            WorkspaceStatus.COMMITTED,
            WorkspaceStatus.FAILED,
            WorkspaceStatus.ARCHIVED,
        }
    ),
    WorkspaceStatus.DIRTY: frozenset(
        {
            WorkspaceStatus.READY,
            WorkspaceStatus.COMMITTED,
            WorkspaceStatus.FAILED,
            WorkspaceStatus.ARCHIVED,
        }
    ),
    WorkspaceStatus.COMMITTED: frozenset({WorkspaceStatus.DIRTY, WorkspaceStatus.ARCHIVED}),
    WorkspaceStatus.FAILED: frozenset({WorkspaceStatus.ARCHIVED, WorkspaceStatus.DELETED}),
    WorkspaceStatus.ARCHIVED: frozenset({WorkspaceStatus.DELETED}),
    WorkspaceStatus.DELETED: frozenset(),
}


def validate_workspace_transition(current: WorkspaceStatus, new: WorkspaceStatus) -> None:
    """Raise ValueError when a workspace lifecycle transition is not allowed."""

    if new not in VALID_WORKSPACE_TRANSITIONS[current]:
        raise ValueError(f"invalid workspace transition from {current.value!r} to {new.value!r}")
