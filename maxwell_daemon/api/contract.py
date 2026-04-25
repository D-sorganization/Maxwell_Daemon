"""Stable operator-facing API contract models (surface version 1.0.0).

These Pydantic models define the JSON shapes for the ``/api/`` endpoints
that runner-dashboard (and any other operator tooling) relies on.  The
``CONTRACT_VERSION`` constant must be bumped whenever a breaking change is
made to these shapes.
"""

from __future__ import annotations

from pydantic import BaseModel

CONTRACT_VERSION = "1.0.0"


class VersionResponse(BaseModel):
    daemon: str
    contract: str


class HealthResponse(BaseModel):
    status: str  # "ok" or "degraded"
    uptime_seconds: float
    gate: str  # "open" or "closed"
    strategist_focus: str | None = None


class StatusResponse(BaseModel):
    pipeline_state: str  # "idle", "running", "paused", "error"
    active_task_id: str | None = None
    gate: str
    sandbox: str  # "enabled" or "disabled"


class TaskSummary(BaseModel):
    id: str
    status: str
    created_at: str
    repo: str | None = None
    prompt_preview: str


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
    next_cursor: str | None = None
    total: int


class TaskDetail(BaseModel):
    id: str
    status: str
    created_at: str
    repo: str | None = None
    transcript: list[dict]
    artifacts: list[dict]


class DispatchRequest(BaseModel):
    confirmation_token: str
    prompt: str
    repo: str | None = None
    idempotency_key: str


class DispatchResponse(BaseModel):
    task_id: str
    status: str
    queued_at: str


class ControlRequest(BaseModel):
    confirmation_token: str
    reason: str | None = None


class ControlResponse(BaseModel):
    action: str
    applied_at: str
    previous_state: str
