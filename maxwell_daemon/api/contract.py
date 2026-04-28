"""Stable operator-facing API contract models (surface version 2.0.0).

These Pydantic models define the JSON shapes for the ``/api/`` endpoints
that runner-dashboard (and any other operator tooling) relies on.  The
``CONTRACT_VERSION`` constant must be bumped whenever a breaking change is
made to these shapes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

CONTRACT_VERSION = "2.0.0"


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


class StatusV2Tokens(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class StatusV2RunningTask(BaseModel):
    task_id: str
    session_id: str | None = None
    run_count: int = 0
    last_event: str
    started_at: str | None = None
    dispatched_to: str | None = None
    tokens: StatusV2Tokens


class StatusV2RetryingTask(BaseModel):
    task_id: str
    attempt: int = 0
    due_at: str | None = None
    error: str | None = None


class StatusV2Totals(StatusV2Tokens):
    seconds_running: float = 0.0


class StatusV2Response(BaseModel):
    generated_at: str
    counts: dict[str, int]
    running: list[StatusV2RunningTask]
    retrying: list[StatusV2RetryingTask]
    codex_totals: StatusV2Totals
    rate_limits: dict[str, Any] | None = None


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
    transcript: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]


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
