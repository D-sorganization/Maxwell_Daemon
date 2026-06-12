"""Stable operator-facing API contract models (surface version 2.0.0).

These Pydantic models define the JSON shapes for the ``/api/`` endpoints
that runner-dashboard (and any other operator tooling) relies on.  The
``CONTRACT_VERSION`` constant must be bumped whenever a breaking change is
made to these shapes.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "2.0.0"

# -- Canonical connection profile (single source of truth, #996) -------------
#
# The default loopback port the daemon listens on (``serve --port`` /
# ``APIConfig.port``). Consumers (e.g. Runner_Dashboard) MUST import/vendor
# these values rather than hard-coding a guess. There is no built-in token:
# auth is operator-configured (static ``api.auth_token`` or ``api.jwt_secret``)
# and the daemon runs open only when neither is set.
DEFAULT_API_PORT = 8080
DEFAULT_API_HOST = "127.0.0.1"
SYSTEMD_UNIT_NAME = "maxwell-daemon.service"
HEALTH_ENDPOINT = "/api/health"
VERSION_ENDPOINT = "/api/version"


class ConnectionProfile(BaseModel):
    """Machine-readable statement of how to connect to this daemon (#996).

    Published at ``GET /api/version`` adjacent metadata and importable by
    consumers so the default port / health probe / version-negotiation
    endpoint are a single source of truth instead of a hard-coded guess.
    """

    model_config = ConfigDict(extra="forbid")

    contract: str = CONTRACT_VERSION
    default_host: str = DEFAULT_API_HOST
    default_port: int = DEFAULT_API_PORT
    systemd_unit: str = SYSTEMD_UNIT_NAME
    health_endpoint: str = HEALTH_ENDPOINT
    version_endpoint: str = VERSION_ENDPOINT
    # Auth is operator-configured; there is no shipped default token. A
    # consumer presenting a placeholder token is rejected (401), never
    # silently admitted.
    auth_required_when_configured: bool = True


def connection_profile() -> ConnectionProfile:
    """Return the canonical connection profile for this daemon build."""
    return ConnectionProfile()


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
    # ``transcript`` is reserved for a future transcript store and is always
    # an empty list today (no transcript is persisted yet); it is NOT a silent
    # stub — consumers should treat empty as "no transcript available" (#998).
    transcript: list[dict[str, Any]]
    # ``artifacts`` is populated from the artifact store: each entry is
    # ``{"id", "kind", "created_at"}`` (#998).
    artifacts: list[dict[str, Any]]


class DispatchRequest(BaseModel):
    # Contract-surface request: reject unknown fields with a 422 naming the
    # offending key rather than silently dropping them (#994).
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str
    prompt: str
    repo: str | None = None
    idempotency_key: str


class DispatchResponse(BaseModel):
    task_id: str
    status: str
    queued_at: str


class ControlRequest(BaseModel):
    # Contract-surface request: reject unknown fields loudly (#994).
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str
    reason: str | None = None


class ControlResponse(BaseModel):
    action: str
    applied_at: str
    previous_state: str


class CostSummary(BaseModel):
    month_to_date_usd: float
    by_backend: dict[str, float]
