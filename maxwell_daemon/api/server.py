"""FastAPI app exposing the daemon over HTTP.

Endpoints (v1):
    GET  /health
    GET  /api/v1/backends
    GET  /api/v1/backends/available
    POST /api/v1/tasks           — submit a task
    GET  /api/v1/tasks           — list tasks
    GET  /api/v1/tasks/{id}      — fetch a task
    GET  /api/v1/cost            — cost summary (month-to-date + by backend)

Auth: simple bearer token from `api.auth_token` in config. For production,
terminate TLS at the reverse proxy (nginx, caddy) or enable TLS in uvicorn.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Annotated, Any, Literal

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field, field_validator

from maxwell_daemon import __version__
from maxwell_daemon.api.validation import PriorityField, PromptField, RepoField, TaskIdField
from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.backends import BackendManifest, registry
from maxwell_daemon.core.actions import Action, ActionStatus
from maxwell_daemon.core.artifacts import Artifact, ArtifactIntegrityError, ArtifactKind
from maxwell_daemon.core.delegate_lifecycle import DelegateSessionSnapshot, DelegateSessionStatus
from maxwell_daemon.core.work_items import (
    REPO_PATTERN,
    AcceptanceCriterion,
    ScopeBoundary,
    WorkItem,
    WorkItemStatus,
)
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import DuplicateTaskIdError, Task, TaskStatus
from maxwell_daemon.director import (
    GraphStatus,
    NodeRun,
    TaskGraph,
    TaskGraphExecutorUnavailableError,
    TaskGraphRecord,
    TaskGraphTemplate,
)
from maxwell_daemon.logging import bind_context
from maxwell_daemon.metrics import mount_metrics_endpoint

_UI_DIR = _Path(__file__).parent / "ui"


def _mount_web_ui(app: FastAPI) -> None:
    """Serve the vanilla-JS dashboard at ``/ui/``.

    Uses ``StaticFiles`` so there's no per-request Python overhead and
    content-types are set correctly from the file extension.
    """
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    if not _UI_DIR.is_dir():
        return  # Missing assets — skip mounting rather than fail startup.

    app.mount("/ui/", StaticFiles(directory=_UI_DIR, html=True), name="maxwell-daemon-ui")

    @app.get("/ui", include_in_schema=False)
    async def _ui_no_slash() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=308)


def _coerce_datetime_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_delegate_status(value: str | None) -> DelegateSessionStatus | None:
    if value is None:
        return None
    try:
        return DelegateSessionStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"invalid delegate session status: {value}",
        ) from exc


class TaskSubmit(BaseModel):
    prompt: PromptField
    task_id: TaskIdField | None = None
    kind: str = "prompt"
    repo: RepoField | None = None
    backend: str | None = None
    model: str | None = None
    issue_repo: RepoField | None = None
    issue_number: int | None = None
    issue_mode: Literal["plan", "implement"] | None = None
    priority: PriorityField = 100


class WorkItemCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    repo: RepoField | None = None
    source: str = Field(default="api", pattern=r"^(manual|github_issue|gaai|api)$")
    source_url: str | None = None
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    scope: ScopeBoundary = Field(default_factory=ScopeBoundary)
    required_checks: tuple[str, ...] = ()
    priority: int = Field(default=100, ge=0, le=1000)
    task_ids: tuple[str, ...] = ()


class WorkItemPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    body: str | None = None
    repo: RepoField | None = None
    source_url: str | None = None
    acceptance_criteria: tuple[AcceptanceCriterion, ...] | None = None
    scope: ScopeBoundary | None = None
    required_checks: tuple[str, ...] | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    task_ids: tuple[str, ...] | None = None


class WorkItemTransition(BaseModel):
    status: WorkItemStatus


class WorkItemView(BaseModel):
    id: str
    title: str
    body: str
    repo: str | None
    source: str
    source_url: str | None
    status: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    scope: ScopeBoundary
    required_checks: tuple[str, ...]
    priority: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    task_ids: tuple[str, ...]

    @classmethod
    def from_item(cls, item: WorkItem) -> WorkItemView:
        return cls(
            id=item.id,
            title=item.title,
            body=item.body,
            repo=item.repo,
            source=item.source,
            source_url=item.source_url,
            status=item.status.value,
            acceptance_criteria=item.acceptance_criteria,
            scope=item.scope,
            required_checks=item.required_checks,
            priority=item.priority,
            created_at=item.created_at,
            updated_at=item.updated_at,
            started_at=item.started_at,
            completed_at=item.completed_at,
            task_ids=item.task_ids,
        )


class TaskGraphCreate(BaseModel):
    work_item_id: str = Field(..., min_length=1)
    id: str | None = Field(default=None, min_length=1)
    template: TaskGraphTemplate | None = None
    labels: tuple[str, ...] = ()


class NodeRunView(BaseModel):
    id: str
    graph_id: str
    node_id: str
    status: str
    task_id: str | None
    artifact_ids: tuple[str, ...]
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: float
    attempts: int
    error: str | None

    @classmethod
    def from_run(cls, run: NodeRun) -> NodeRunView:
        return cls(
            id=run.id,
            graph_id=run.graph_id,
            node_id=run.node_id,
            status=run.status.value,
            task_id=run.task_id,
            artifact_ids=run.artifact_ids,
            started_at=run.started_at,
            finished_at=run.finished_at,
            cost_usd=run.cost_usd,
            attempts=run.attempts,
            error=run.error,
        )


class TaskGraphView(BaseModel):
    graph: TaskGraph
    node_runs: tuple[NodeRunView, ...]

    @classmethod
    def from_record(cls, record: TaskGraphRecord) -> TaskGraphView:
        return cls(
            graph=record.graph,
            node_runs=tuple(NodeRunView.from_run(run) for run in record.node_runs),
        )


class ArtifactView(BaseModel):
    id: str
    task_id: str | None
    work_item_id: str | None
    kind: str
    name: str
    media_type: str
    path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    metadata: dict[str, Any]

    @classmethod
    def from_artifact(cls, artifact: Artifact) -> ArtifactView:
        return cls(
            id=artifact.id,
            task_id=artifact.task_id,
            work_item_id=artifact.work_item_id,
            kind=artifact.kind.value,
            name=artifact.name,
            media_type=artifact.media_type,
            path=artifact.path.as_posix(),
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            created_at=artifact.created_at,
            metadata=artifact.metadata,
        )


class ActionView(BaseModel):
    id: str
    task_id: str
    work_item_id: str | None
    kind: str
    status: str
    summary: str
    payload: dict[str, Any]
    risk_level: str
    requires_approval: bool
    approval_contract: Literal["proposal_only"] = "proposal_only"
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    rejection_reason: str | None
    result_artifact_id: str | None
    result: dict[str, Any]
    error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_action(cls, action: Action) -> ActionView:
        return cls(
            id=action.id,
            task_id=action.task_id,
            work_item_id=action.work_item_id,
            kind=action.kind.value,
            status=action.status.value,
            summary=action.summary,
            payload=action.payload,
            risk_level=action.risk_level.value,
            requires_approval=action.requires_approval,
            approval_contract="proposal_only",
            approved_by=action.approved_by,
            approved_at=action.approved_at,
            rejected_by=action.rejected_by,
            rejected_at=action.rejected_at,
            rejection_reason=action.rejection_reason,
            result_artifact_id=action.result_artifact_id,
            result=action.result,
            error=action.error,
            created_at=action.created_at,
            updated_at=action.updated_at,
        )


class ActionRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class GateRetryRequest(BaseModel):
    target_id: str = Field(..., min_length=1)
    expected_status: Literal["failed"] = "failed"


class GateCancelRequest(BaseModel):
    target_id: str = Field(..., min_length=1)
    expected_status: Literal["queued"] = "queued"


class GateWaiverRequest(BaseModel):
    target_id: str = Field(..., min_length=1)
    expected_status: Literal["failed"] = "failed"
    actor: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=1000)


class IssueCreate(BaseModel):
    repo: RepoField
    title: str = Field(..., min_length=1)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    dispatch: bool = False
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")


class IssueDispatch(BaseModel):
    repo: RepoField
    number: int = Field(..., ge=1)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")
    backend: str | None = None
    model: str | None = None
    priority: PriorityField = 100


class IssueBatchDispatch(BaseModel):
    items: list[IssueDispatch] = Field(..., min_length=1, max_length=100)


class IssueAbDispatch(BaseModel):
    repo: RepoField
    number: int = Field(..., ge=1)
    backends: list[str] = Field(..., min_length=2, max_length=4)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")

    @field_validator("backends")
    @classmethod
    def _check_distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("A/B backends must be distinct")
        return v


class TaskView(BaseModel):
    id: str
    prompt: str
    kind: str
    repo: str | None
    backend: str | None
    model: str | None
    route_reason: str | None = None
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None
    ab_group: str | None = None
    priority: int = 100
    pr_url: str | None = None
    dispatched_to: str | None = None
    status: str
    result: str | None
    error: str | None
    waived_by: str | None = None
    waiver_reason: str | None = None
    waived_at: datetime | None = None
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_task(cls, t: Task) -> TaskView:
        return cls(
            id=t.id,
            prompt=t.prompt,
            kind=t.kind.value,
            repo=t.repo,
            backend=t.backend,
            model=t.model,
            route_reason=t.route_reason,
            issue_repo=t.issue_repo,
            issue_number=t.issue_number,
            issue_mode=t.issue_mode,
            ab_group=t.ab_group,
            priority=getattr(t, "priority", 100),
            pr_url=t.pr_url,
            dispatched_to=t.dispatched_to,
            status=t.status.value,
            result=t.result,
            error=t.error,
            waived_by=t.waived_by,
            waiver_reason=t.waiver_reason,
            waived_at=t.waived_at,
            cost_usd=t.cost_usd,
            created_at=t.created_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
        )


class BackendCatalogEntryView(BaseModel):
    name: str
    display_name: str
    description: str
    requires_api_key: bool
    local_only: bool
    default_endpoint: str | None = None
    api_key_env_var: str | None = None
    endpoint_env_var: str | None = None
    install_extra: str | None = None
    command: str | None = None
    configured_aliases: tuple[str, ...] = ()
    loaded: bool
    connected: bool

    @classmethod
    def from_manifest(
        cls,
        manifest: BackendManifest,
        *,
        configured_aliases: tuple[str, ...],
        loaded: bool,
        connected: bool,
    ) -> BackendCatalogEntryView:
        return cls(
            name=manifest.name,
            display_name=manifest.display_name,
            description=manifest.description,
            requires_api_key=manifest.requires_api_key,
            local_only=manifest.local_only,
            default_endpoint=manifest.default_endpoint,
            api_key_env_var=manifest.api_key_env_var,
            endpoint_env_var=manifest.endpoint_env_var,
            install_extra=manifest.install_extra,
            command=manifest.command,
            configured_aliases=configured_aliases,
            loaded=loaded,
            connected=connected,
        )


class BackendCatalogResponse(BaseModel):
    backends: tuple[BackendCatalogEntryView, ...]


class GateTimelineEntry(BaseModel):
    id: str
    name: str
    status: Literal["passed", "failed", "blocked", "waived", "running", "pending"]
    evidence_links: tuple[str, ...] = ()
    next_action: str | None = None
    retry_allowed: bool = False
    waiver_allowed: bool = False


class CriticFindingView(BaseModel):
    severity: Literal["blocker", "warning", "note"]
    critic: str
    message: str
    file: str | None = None
    line: int | None = None
    evidence: str | None = None


class DelegateSessionView(BaseModel):
    id: str
    role: str
    status: str
    machine: str | None = None
    backend: str | None = None
    latest_checkpoint: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float | None = None


class ResourceRoutingView(BaseModel):
    selected_backend: str | None
    selected_model: str | None = None
    selection_reason: str | None = None
    alternatives_considered: tuple[str, ...] = ()
    warning: str | None = None


class ControlPlaneActionView(BaseModel):
    kind: Literal["retry", "waive", "cancel"]
    label: str
    path: str
    target_id: str
    expected_status: Literal["failed", "queued"]
    requires_reason: bool = False
    requires_actor: bool = False


class ControlPlaneWorkItemView(BaseModel):
    work_item_id: str | None = None
    work_item_status: str | None = None
    task_id: str
    title: str
    status: str
    final_decision: Literal["pass", "fail", "blocked", "running", "pending", "cancelled", "waived"]
    current_gate: str | None
    next_action: str
    gates: tuple[GateTimelineEntry, ...]
    critic_findings: tuple[CriticFindingView, ...] = ()
    delegates: tuple[DelegateSessionView, ...]
    resource_routing: ResourceRoutingView
    actions: tuple[ControlPlaneActionView, ...] = ()


class CostSummary(BaseModel):
    month_to_date_usd: float
    by_backend: dict[str, float]


class AssembleMemoryRequest(BaseModel):
    repo: str = Field(..., min_length=1)
    issue_title: str = ""
    issue_body: str = ""
    task_id: str = Field(..., min_length=1)
    max_chars: int = 8000


class RecordMemoryOutcome(BaseModel):
    task_id: str = Field(..., min_length=1)
    repo: str = Field(..., min_length=1)
    issue_number: int
    issue_title: str = ""
    issue_body: str = ""
    plan: str = ""
    applied_diff: bool = False
    pr_url: str = ""
    outcome: str = ""


class SSHConnectRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    password: str | None = None


class SSHRunRequest(BaseModel):
    host: str
    port: int = 22
    user: str
    command: str
    timeout_seconds: float = 30.0


class TokenRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="viewer", pattern=r"^(admin|operator|viewer|developer)$")
    expiry_seconds: int | None = Field(default=None, ge=1, le=86400 * 30)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


def _auth_dep(token: str | None) -> Any:
    async def _check(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        presented = authorization.removeprefix("Bearer ").strip()
        # Constant-time comparison — prevents leaking token via response timing.
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    return _check


def _make_rbac_dep(
    minimum: Role,
    static_token: str | None,
    jwt_config: JWTConfig | None,
) -> Any:
    """Return a FastAPI dependency that enforces *minimum* role.

    Accepts EITHER:
    - A valid static admin bearer token (treated as Role.admin), OR
    - A valid JWT bearer token whose role is >= *minimum*.

    When neither JWT config nor static token is configured, all requests pass
    (open/dev mode — same behaviour as the existing ``_auth_dep(None)``).
    """

    async def _dep(authorization: Annotated[str | None, Header()] = None) -> None:
        # Open mode — nothing to enforce.
        if static_token is None and jwt_config is None:
            return

        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")

        raw = authorization.removeprefix("Bearer ").strip()

        # Fast path: static admin token — always grants admin-level access.
        if static_token is not None and hmac.compare_digest(raw.encode(), static_token.encode()):
            return  # admitted as admin

        # JWT path.
        if jwt_config is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

        try:
            claims = jwt_config.decode_token(raw)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc

        if not claims.has_role(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
            )

    return _dep


async def _websocket_auth_or_close(
    ws: WebSocket,
    minimum: Role,
    static_token: str | None,
    jwt_config: JWTConfig | None,
) -> bool:
    """Authenticate a WebSocket query token against static/JWT auth policy."""
    if static_token is None and jwt_config is None:
        return True

    presented = ws.query_params.get("token") or ""
    if not presented:
        await ws.close(code=1008)
        return False

    if static_token is not None and hmac.compare_digest(presented.encode(), static_token.encode()):
        return True

    if jwt_config is None:
        await ws.close(code=1008)
        return False

    try:
        claims = jwt_config.decode_token(presented)
    except Exception:  # nosec B110 - invalid WebSocket token, close below.
        await ws.close(code=1008)
        return False

    if not claims.has_role(minimum):
        await ws.close(code=1008)
        return False
    return True


def _task_title(task: Task) -> str:
    if task.issue_repo and task.issue_number is not None:
        return f"{task.issue_repo}#{task.issue_number}"
    return task.prompt[:80]


def _duration_seconds(task: Task) -> float | None:
    if task.started_at is None:
        return None
    end = task.finished_at or datetime.now(timezone.utc)
    return max(0.0, (end - task.started_at).total_seconds())


def _task_is_waived(task: Task) -> bool:
    return bool(task.waived_by and task.waiver_reason)


def _control_plane_actions_for_task(task: Task) -> tuple[ControlPlaneActionView, ...]:
    if task.status.value == "queued":
        return (
            ControlPlaneActionView(
                kind="cancel",
                label="Cancel",
                path=f"/api/v1/control-plane/gauntlet/{task.id}/cancel",
                target_id=task.id,
                expected_status="queued",
            ),
        )
    if task.status.value != "failed" or _task_is_waived(task):
        return ()
    return (
        ControlPlaneActionView(
            kind="retry",
            label="Retry",
            path=f"/api/v1/control-plane/gauntlet/{task.id}/retry",
            target_id=task.id,
            expected_status="failed",
        ),
        ControlPlaneActionView(
            kind="waive",
            label="Waive",
            path=f"/api/v1/control-plane/gauntlet/{task.id}/waive",
            target_id=task.id,
            expected_status="failed",
            requires_reason=True,
            requires_actor=True,
        ),
    )


def _gate_statuses_for_task(task: Task) -> tuple[GateTimelineEntry, ...]:
    status_value = task.status.value
    target = _task_title(task)
    evidence = (task.pr_url,) if task.pr_url else ()
    intake = GateTimelineEntry(
        id="intake",
        name="Work intake",
        status="passed",
        evidence_links=evidence,
    )
    if status_value == "queued":
        return (
            intake,
            GateTimelineEntry(
                id="delegate",
                name="Delegate session",
                status="pending",
                next_action="Waiting for a delegate slot",
            ),
            GateTimelineEntry(id="verification", name="Verification", status="pending"),
        )
    if status_value in {"running", "dispatched"}:
        return (
            intake,
            GateTimelineEntry(
                id="delegate",
                name="Delegate session",
                status="running",
                next_action="Delegate is still working",
            ),
            GateTimelineEntry(
                id="verification",
                name="Verification",
                status="blocked",
                next_action="Wait for delegate output before retrying verification",
            ),
        )
    if status_value == "completed":
        return (
            intake,
            GateTimelineEntry(
                id="delegate",
                name="Delegate session",
                status="passed",
                evidence_links=evidence,
            ),
            GateTimelineEntry(
                id="verification",
                name="Verification",
                status="passed",
                evidence_links=evidence,
            ),
        )
    if status_value == "failed":
        if _task_is_waived(task):
            return (
                intake,
                GateTimelineEntry(
                    id="delegate",
                    name="Delegate session",
                    status="waived",
                    evidence_links=evidence,
                    next_action=f"Waived by {task.waived_by}: {task.waiver_reason}",
                ),
                GateTimelineEntry(
                    id="verification",
                    name="Verification",
                    status="blocked",
                    next_action="Waived failures stay visible until the task is retried",
                ),
            )
        return (
            intake,
            GateTimelineEntry(
                id="delegate",
                name="Delegate session",
                status="failed",
                evidence_links=evidence,
                next_action="Inspect failure evidence and retry if policy allows",
                retry_allowed=True,
                waiver_allowed=True,
            ),
            GateTimelineEntry(
                id="verification",
                name="Verification",
                status="blocked",
                next_action="Blocked by failed delegate session",
            ),
        )
    if status_value == "cancelled":
        return (
            intake,
            GateTimelineEntry(
                id="delegate",
                name="Delegate session",
                status="waived",
                evidence_links=evidence,
                next_action=f"{target} was cancelled by policy or operator action",
            ),
            GateTimelineEntry(id="verification", name="Verification", status="blocked"),
        )
    return (
        intake,
        GateTimelineEntry(
            id="delegate",
            name="Delegate session",
            status="blocked",
            next_action=f"Unknown task status {status_value!r}",
        ),
    )


def _critic_findings_for_task(task: Task) -> tuple[CriticFindingView, ...]:
    findings: list[CriticFindingView] = []
    if task.status.value == "failed":
        findings.append(
            CriticFindingView(
                severity="blocker",
                critic="runtime",
                message=task.error or "Delegate failed without a recorded error",
                evidence=task.result,
            )
        )
    if task.status.value in {"queued", "running", "dispatched"}:
        findings.append(
            CriticFindingView(
                severity="note",
                critic="control-plane",
                message="No critic verdict is available until the delegate reaches a gate.",
            )
        )
    priority = {"blocker": 0, "warning": 1, "note": 2}
    return tuple(sorted(findings, key=lambda finding: priority[finding.severity]))


def _delegate_snapshots_for_task(
    daemon: Daemon, task: Task, *, limit: int = 20
) -> tuple[DelegateSessionSnapshot, ...]:
    snapshots = daemon.delegate_lifecycle.list_sessions(limit=limit, task_id=task.id)
    ordered = sorted(
        snapshots,
        key=lambda snapshot: (
            snapshot.session.updated_at,
            snapshot.latest_checkpoint.created_at
            if snapshot.latest_checkpoint
            else snapshot.session.created_at,
        ),
        reverse=True,
    )
    return tuple(ordered)


def _delegate_views_for_task(
    daemon: Daemon, task: Task, snapshots: tuple[DelegateSessionSnapshot, ...]
) -> tuple[DelegateSessionView, ...]:
    if snapshots:
        views: list[DelegateSessionView] = []
        for snapshot in snapshots:
            session = snapshot.session
            checkpoint = snapshot.latest_checkpoint
            latest_checkpoint = None
            if checkpoint is not None:
                latest_checkpoint = checkpoint.current_plan
                if checkpoint.failures_and_learnings:
                    latest_checkpoint = (
                        f"{checkpoint.current_plan} | {checkpoint.failures_and_learnings[0]}"
                    )
            metadata_cost = session.metadata.get("cost_usd")
            try:
                cost_usd = float(metadata_cost) if metadata_cost is not None else 0.0
            except (TypeError, ValueError):
                cost_usd = 0.0
            duration_seconds = max(0.0, (session.updated_at - session.created_at).total_seconds())
            views.append(
                DelegateSessionView(
                    id=session.id,
                    role=session.delegate_id,
                    status=session.status.value,
                    machine=session.machine_ref,
                    backend=session.backend_ref,
                    latest_checkpoint=latest_checkpoint,
                    cost_usd=cost_usd,
                    duration_seconds=duration_seconds,
                )
            )
        return tuple(views)

    backend = task.backend or task.model
    return (
        DelegateSessionView(
            id=f"{task.id}:delegate",
            role="implementer" if task.kind.value == "issue" else "operator",
            status=task.status.value,
            machine=task.dispatched_to,
            backend=backend,
            latest_checkpoint=task.result or task.error,
            cost_usd=task.cost_usd,
            duration_seconds=_duration_seconds(task),
        ),
    )


def _work_item_context_for_task(
    daemon: Daemon, snapshots: tuple[DelegateSessionSnapshot, ...]
) -> tuple[str | None, str | None]:
    work_item_id = next(
        (
            snapshot.session.work_item_id
            for snapshot in snapshots
            if snapshot.session.work_item_id is not None
        ),
        None,
    )
    if work_item_id is None:
        return None, None
    item = daemon.get_work_item(work_item_id)
    return work_item_id, item.status.value if item is not None else None


def _control_plane_view_from_task(daemon: Daemon, task: Task) -> ControlPlaneWorkItemView:
    gates = _gate_statuses_for_task(task)
    snapshots = _delegate_snapshots_for_task(daemon, task)
    work_item_id, work_item_status = _work_item_context_for_task(daemon, snapshots)
    current_gate = next(
        (gate.name for gate in gates if gate.status in {"failed", "blocked", "running", "pending"}),
        None,
    )
    decision_by_status: dict[
        str, Literal["pass", "fail", "blocked", "running", "pending", "cancelled", "waived"]
    ] = {
        "completed": "pass",
        "failed": "waived" if _task_is_waived(task) else "fail",
        "cancelled": "cancelled",
        "running": "running",
        "dispatched": "running",
        "queued": "pending",
    }
    next_action_by_status = {
        "completed": "Review artifacts and merge only if policy allows",
        "failed": (
            f"Waived by {task.waived_by}: {task.waiver_reason}"
            if _task_is_waived(task)
            else "Inspect blocker evidence, then retry or waive with a reason"
        ),
        "cancelled": "No action required unless the task should be requeued",
        "running": "Wait for the delegate to reach the next gate",
        "dispatched": "Wait for the assigned worker heartbeat or recovery timeout",
        "queued": "Assign or wait for an available delegate",
    }
    backend = task.backend
    return ControlPlaneWorkItemView(
        work_item_id=work_item_id,
        work_item_status=work_item_status,
        task_id=task.id,
        title=_task_title(task),
        status=task.status.value,
        final_decision=decision_by_status.get(task.status.value, "blocked"),
        current_gate=current_gate,
        next_action=next_action_by_status.get(task.status.value, "Inspect task state"),
        gates=gates,
        critic_findings=_critic_findings_for_task(task),
        delegates=_delegate_views_for_task(daemon, task, snapshots),
        resource_routing=ResourceRoutingView(
            selected_backend=backend,
            selected_model=task.model,
            selection_reason=task.route_reason,
            alternatives_considered=(),
            warning=None if backend else "No backend has been selected yet",
        ),
        actions=_control_plane_actions_for_task(task),
    )


def create_app(
    daemon: Daemon,
    *,
    auth_token: str | None = None,
    github_client: Any = None,
    audit_log_path: _Path | None = None,
    jwt_config: JWTConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `github_client` is an optional injection point for tests — if omitted, the
    handlers construct a fresh ``GitHubClient()`` on demand.
    """
    app = FastAPI(
        title="Maxwell-Daemon API",
        version=__version__,
        description="Remote control plane for Maxwell-Daemon daemons.",
    )
    mount_metrics_endpoint(app)
    _mount_web_ui(app)

    from fastapi.responses import JSONResponse

    from maxwell_daemon.daemon.runner import QueueSaturationError

    @app.exception_handler(QueueSaturationError)
    async def queue_saturation_exception_handler(request: Request, exc: QueueSaturationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.backoff_seconds)},
        )

    # When jwt_config is provided, RBAC deps handle all auth (both static and JWT).
    # The `auth` dep becomes a pass-through so endpoints with Depends(auth) still
    # work with JWT tokens without double-checking the static token.
    auth = _auth_dep(None if jwt_config is not None else auth_token)

    # RBAC dependency factories — only active when jwt_config is provided.
    # When jwt_config is None the daemon falls back to static bearer-token auth
    # (``auth`` dep above) and role enforcement is skipped.
    def _require_viewer() -> Any:
        if jwt_config is not None:
            return _make_rbac_dep(Role.viewer, auth_token, jwt_config)
        return auth

    def _require_operator() -> Any:
        if jwt_config is not None:
            return _make_rbac_dep(Role.operator, auth_token, jwt_config)
        return auth

    def _require_admin() -> Any:
        if jwt_config is not None:
            return _make_rbac_dep(Role.admin, auth_token, jwt_config)
        return auth

    _audit: AuditLogger | None = (
        AuditLogger(audit_log_path, retention_days=daemon._config.agent.task_retention_days)
        if audit_log_path is not None
        else None
    )

    # Rate-limit middleware — installs only when config declares a default group.
    api_cfg = daemon._config.api
    if api_cfg.rate_limit_default is not None:
        from maxwell_daemon.api.rate_limit import install_rate_limiter

        install_rate_limiter(
            app,
            default_rate=api_cfg.rate_limit_default.rate,
            default_burst=api_cfg.rate_limit_default.burst,
            groups={
                name: {"rate": g.rate, "burst": g.burst}
                for name, g in api_cfg.rate_limit_groups.items()
            },
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        """Attach a UUID request-id to every request + response + log line."""
        incoming = request.headers.get("x-request-id", "")
        try:
            request_id = str(uuid.UUID(incoming)) if incoming else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())
        with bind_context(request_id=request_id):
            response: Response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if _audit is not None and not request.url.path.startswith("/ui"):
            auth_header = request.headers.get("authorization", "")
            # Extract only the auth scheme prefix (e.g. "Bearer") — never
            # persist the actual token value in the audit log (#234).
            if auth_header.lower().startswith("bearer "):
                user = "Bearer ***"
            elif auth_header:
                scheme = auth_header.split(" ", 1)[0]
                user = f"{scheme} ***"
            else:
                user = None
            _audit.log_api_call(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                user=user,
                request_id=request_id,
            )
        return response

    def _gh() -> Any:
        if github_client is not None:
            return github_client
        from maxwell_daemon.gh import GitHubClient

        return GitHubClient()

    # ── JWT auth endpoints ────────────────────────────────────────────────────

    @app.post("/api/v1/auth/token", dependencies=[Depends(_require_admin())])
    async def issue_token(payload: Annotated[TokenRequest, Body()]) -> TokenResponse:
        """Issue a JWT with the requested role.

        Requires an admin credential.  The resulting JWT can then be used in
        place of the static token for role-scoped access.
        """
        if jwt_config is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "JWT not configured — set api.jwt_secret in config",
            )
        ttl = payload.expiry_seconds or jwt_config.expiry_seconds
        role = Role(payload.role)
        token = jwt_config.create_token(payload.subject, role, expiry_seconds=ttl)
        return TokenResponse(access_token=token, expires_in=ttl, role=role.value)

    @app.get("/api/v1/auth/me")
    async def whoami(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Decode and return the caller's JWT claims (or static-token identity)."""
        if jwt_config is not None and authorization and authorization.startswith("Bearer "):
            raw = authorization.removeprefix("Bearer ").strip()
            try:
                claims = jwt_config.decode_token(raw)
                return {
                    "sub": claims.sub,
                    "role": claims.role.value,
                    "exp": claims.exp.isoformat(),
                }
            except Exception:  # nosec B110 — invalid/expired JWT, fall through to token check
                pass
        if auth_token is not None and authorization:
            raw = authorization.removeprefix("Bearer ").strip()
            if hmac.compare_digest(raw.encode(), auth_token.encode()):
                return {"sub": "static-token", "role": "admin", "exp": None}
        return {"sub": "anonymous", "role": None, "exp": None}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        state = daemon.state()
        return {
            "status": "ok",
            "version": state.version,
            "uptime_seconds": (datetime.now(timezone.utc) - state.started_at).total_seconds(),
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        state = daemon.state()
        if not state.backends_available:
            raise HTTPException(status_code=503, detail="no backends available")
        return {"status": "ready"}

    @app.get("/api/v1/backends", dependencies=[Depends(_require_viewer())])
    async def list_backends() -> dict[str, Any]:
        return {"backends": daemon.state().backends_available}

    @app.get(
        "/api/v1/backends/available",
        dependencies=[Depends(_require_viewer())],
        response_model=BackendCatalogResponse,
    )
    async def list_available_backends() -> BackendCatalogResponse:
        configured_aliases_by_type: dict[str, list[str]] = {}
        for alias, backend_cfg in daemon._config.backends.items():
            configured_aliases_by_type.setdefault(backend_cfg.type, []).append(alias)

        connected_aliases = set(daemon.state().backends_available)
        loaded_backend_types = set(registry.available())
        catalog = tuple(
            BackendCatalogEntryView.from_manifest(
                manifest,
                configured_aliases=tuple(sorted(configured_aliases_by_type.get(manifest.name, ()))),
                loaded=manifest.name in loaded_backend_types,
                connected=any(
                    alias in connected_aliases
                    for alias in configured_aliases_by_type.get(manifest.name, ())
                ),
            )
            for manifest in registry.catalog()
        )
        return BackendCatalogResponse(backends=catalog)

    @app.post(
        "/api/v1/tasks",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_task(payload: TaskSubmit) -> TaskView:
        try:
            if payload.kind == "issue":
                if not payload.issue_repo or payload.issue_number is None:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        "issue_repo and issue_number are required when kind is 'issue'",
                    )
                try:
                    task = daemon.submit_issue(
                        repo=payload.issue_repo,
                        issue_number=payload.issue_number,
                        mode=payload.issue_mode or "plan",
                        backend=payload.backend,
                        model=payload.model,
                        priority=payload.priority,
                        task_id=payload.task_id,
                    )
                except ValueError as exc:
                    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
            else:
                task = daemon.submit(
                    payload.prompt,
                    repo=payload.repo,
                    backend=payload.backend,
                    model=payload.model,
                    priority=payload.priority,
                    task_id=payload.task_id,
                )
        except DuplicateTaskIdError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return TaskView.from_task(task)

    @app.get("/api/v1/tasks", dependencies=[Depends(_require_viewer())])
    async def list_tasks(
        status: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        repo: Annotated[str | None, Query()] = None,
        completed_before: Annotated[datetime | None, Query()] = None,
        completed_before_camel: Annotated[datetime | None, Query(alias="completedBefore")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[TaskView]:
        tasks = list(daemon.state().tasks.values())
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        if kind:
            tasks = [t for t in tasks if t.kind.value == kind]
        if repo:
            tasks = [t for t in tasks if t.repo == repo or t.issue_repo == repo]
        completed_before_filter = completed_before or completed_before_camel
        if completed_before_filter is not None:
            cutoff = _coerce_datetime_to_utc(completed_before_filter)
            tasks = [
                t
                for t in tasks
                if t.finished_at is not None and _coerce_datetime_to_utc(t.finished_at) < cutoff
            ]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [TaskView.from_task(t) for t in tasks[:limit]]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(_require_viewer())])
    async def get_task(task_id: str) -> TaskView:
        t = daemon.get_task(task_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return TaskView.from_task(t)

    @app.get(
        "/api/v1/control-plane/gauntlet",
        response_model=tuple[ControlPlaneWorkItemView, ...],
        dependencies=[Depends(_require_viewer())],
    )
    async def control_plane_gauntlet(
        task_id: Annotated[str | None, Query()] = None,
        status_filter: Annotated[str | None, Query(alias="status")] = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> tuple[ControlPlaneWorkItemView, ...]:
        tasks = list(daemon.state().tasks.values())
        if task_id:
            tasks = [task for task in tasks if task.id == task_id]
        if status_filter:
            tasks = [task for task in tasks if task.status.value == status_filter]
        tasks.sort(key=lambda task: task.created_at, reverse=True)
        return tuple(_control_plane_view_from_task(daemon, task) for task in tasks[:limit])

    @app.post(
        "/api/v1/control-plane/gauntlet/{task_id}/retry",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def retry_control_plane_gate(
        task_id: str, payload: GateRetryRequest
    ) -> ControlPlaneWorkItemView:
        if payload.target_id != task_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "target_id does not match the route task_id"
            )
        task = daemon.get_task(task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        if task.status.value != payload.expected_status:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"task {task_id} is {task.status.value}; expected {payload.expected_status}",
            )
        if _task_is_waived(task):
            raise HTTPException(status.HTTP_409_CONFLICT, f"task {task_id} is already waived")
        try:
            task = daemon.retry_task(task_id, expected_status=TaskStatus(payload.expected_status))
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from exc
        return _control_plane_view_from_task(daemon, task)

    @app.post(
        "/api/v1/control-plane/gauntlet/{task_id}/cancel",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def cancel_control_plane_gate(
        task_id: str, payload: GateCancelRequest
    ) -> ControlPlaneWorkItemView:
        if payload.target_id != task_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "target_id does not match the route task_id"
            )
        task = daemon.get_task(task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        if task.status.value != payload.expected_status:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"task {task_id} is {task.status.value}; expected {payload.expected_status}",
            )
        try:
            task = daemon.cancel_task(task_id)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from exc
        return _control_plane_view_from_task(daemon, task)

    @app.post(
        "/api/v1/control-plane/gauntlet/{task_id}/waive",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def waive_control_plane_gate(
        task_id: str,
        payload: GateWaiverRequest,
    ) -> ControlPlaneWorkItemView:
        if payload.target_id != task_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "target_id does not match the route task_id"
            )
        task = daemon.get_task(task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        if task.status.value != payload.expected_status:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"task {task_id} is {task.status.value}; expected {payload.expected_status}",
            )
        if _task_is_waived(task):
            raise HTTPException(status.HTTP_409_CONFLICT, f"task {task_id} is already waived")
        try:
            task = daemon.waive_task(
                task_id,
                expected_status=TaskStatus(payload.expected_status),
                actor=payload.actor,
                reason=payload.reason,
            )
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from exc
        return _control_plane_view_from_task(daemon, task)

    @app.get("/api/v1/tasks/{task_id}/artifacts", dependencies=[Depends(_require_viewer())])
    async def list_task_artifacts(
        task_id: str,
        kind: Annotated[ArtifactKind | None, Query()] = None,
    ) -> list[ArtifactView]:
        return [
            ArtifactView.from_artifact(artifact)
            for artifact in daemon.list_task_artifacts(task_id, kind=kind)
        ]

    @app.get("/api/v1/tasks/{task_id}/actions", dependencies=[Depends(_require_viewer())])
    async def list_task_actions(task_id: str) -> list[ActionView]:
        return [ActionView.from_action(action) for action in daemon.list_task_actions(task_id)]

    @app.get("/api/v1/actions", dependencies=[Depends(_require_viewer())])
    async def list_actions(
        status_filter: Annotated[ActionStatus | None, Query(alias="status")] = None,
        task_id: Annotated[str | None, Query()] = None,
        work_item_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[ActionView]:
        return [
            ActionView.from_action(action)
            for action in daemon.list_actions(
                status=status_filter,
                task_id=task_id,
                work_item_id=work_item_id,
                limit=limit,
            )
        ]

    @app.get("/api/v1/actions/{action_id}", dependencies=[Depends(_require_viewer())])
    async def get_action(action_id: str) -> ActionView:
        action = daemon.get_action(action_id)
        if action is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
        return ActionView.from_action(action)

    @app.post(
        "/api/v1/actions/{action_id}/approve",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def approve_action(action_id: str) -> ActionView:
        try:
            action = daemon.approve_action(action_id, actor="api", audit=_audit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return ActionView.from_action(action)

    @app.post(
        "/api/v1/actions/{action_id}/reject",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def reject_action(action_id: str, payload: ActionRejectRequest) -> ActionView:
        try:
            action = daemon.reject_action(
                action_id,
                actor="api",
                reason=payload.reason,
                audit=_audit,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return ActionView.from_action(action)

    @app.post(
        "/api/v1/work-items",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_work_item(payload: WorkItemCreate) -> WorkItemView:
        item = WorkItem(
            id=payload.id or uuid.uuid4().hex[:12],
            title=payload.title,
            body=payload.body,
            repo=payload.repo,
            source=payload.source,  # type: ignore[arg-type]
            source_url=payload.source_url,
            acceptance_criteria=payload.acceptance_criteria,
            scope=payload.scope,
            required_checks=payload.required_checks,
            priority=payload.priority,
            task_ids=payload.task_ids,
        )
        try:
            saved = daemon.create_work_item(item)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return WorkItemView.from_item(saved)

    @app.get("/api/v1/work-items", dependencies=[Depends(_require_viewer())])
    async def list_work_items(
        status_filter: Annotated[WorkItemStatus | None, Query(alias="status")] = None,
        repo: Annotated[str | None, Query(pattern=REPO_PATTERN)] = None,
        source: Annotated[str | None, Query(pattern=r"^(manual|github_issue|gaai|api)$")] = None,
        max_priority: Annotated[int | None, Query(ge=0, le=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[WorkItemView]:
        items = daemon.list_work_items(
            limit=limit,
            status=status_filter,
            repo=repo,
            source=source,
            max_priority=max_priority,
        )
        return [WorkItemView.from_item(item) for item in items]

    @app.get("/api/v1/work-items/{item_id}", dependencies=[Depends(_require_viewer())])
    async def get_work_item(item_id: str) -> WorkItemView:
        item = daemon.get_work_item(item_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
        return WorkItemView.from_item(item)

    @app.get("/api/v1/work-items/{item_id}/artifacts", dependencies=[Depends(_require_viewer())])
    async def list_work_item_artifacts(
        item_id: str,
        kind: Annotated[ArtifactKind | None, Query()] = None,
    ) -> list[ArtifactView]:
        return [
            ArtifactView.from_artifact(artifact)
            for artifact in daemon.list_work_item_artifacts(item_id, kind=kind)
        ]

    @app.patch(
        "/api/v1/work-items/{item_id}",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def patch_work_item(item_id: str, payload: WorkItemPatch) -> WorkItemView:
        item = daemon.get_work_item(item_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
        updates = {
            key: value
            for key, value in payload.model_dump(exclude_unset=True).items()
            if value is not None
        }
        updates["updated_at"] = datetime.now(timezone.utc)
        try:
            updated = daemon.update_work_item(WorkItem.model_validate(item.model_dump() | updates))
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return WorkItemView.from_item(updated)

    @app.post(
        "/api/v1/work-items/{item_id}/transition",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def transition_work_item_endpoint(
        item_id: str,
        payload: WorkItemTransition,
    ) -> WorkItemView:
        try:
            item = daemon.transition_work_item(item_id, payload.status)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found") from None
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return WorkItemView.from_item(item)

    @app.post(
        "/api/v1/task-graphs",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_task_graph(payload: TaskGraphCreate) -> TaskGraphView:
        try:
            record = daemon.create_task_graph(
                payload.work_item_id,
                template=payload.template,
                graph_id=payload.id,
                labels=payload.labels,
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return TaskGraphView.from_record(record)

    @app.get("/api/v1/task-graphs", dependencies=[Depends(_require_viewer())])
    async def list_task_graphs(
        work_item_id: Annotated[str | None, Query()] = None,
        status_filter: Annotated[GraphStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[TaskGraphView]:
        return [
            TaskGraphView.from_record(record)
            for record in daemon.list_task_graphs(
                work_item_id=work_item_id,
                status=status_filter,
                limit=limit,
            )
        ]

    @app.get("/api/v1/task-graphs/{graph_id}", dependencies=[Depends(_require_viewer())])
    async def get_task_graph(graph_id: str) -> TaskGraphView:
        record = daemon.get_task_graph(graph_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task graph not found")
        return TaskGraphView.from_record(record)

    @app.post(
        "/api/v1/task-graphs/{graph_id}/start",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def start_task_graph(graph_id: str) -> TaskGraphView:
        try:
            record = daemon.start_task_graph(graph_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task graph not found") from exc
        except TaskGraphExecutorUnavailableError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return TaskGraphView.from_record(record)

    @app.post(
        "/api/v1/tasks/{task_id}/cancel",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def cancel_task(task_id: str) -> TaskView:
        try:
            cancelled = daemon.cancel_task(task_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from None
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
        return TaskView.from_task(cancelled)

    @app.post(
        "/api/v1/memory/assemble",
        dependencies=[Depends(auth), Depends(_require_operator())],
    )
    async def assemble_memory(payload: AssembleMemoryRequest) -> dict[str, Any]:
        """Assemble repo/task context from the coordinator's shared memory store."""
        context = daemon._memory.assemble_context(
            repo=payload.repo,
            issue_title=payload.issue_title,
            issue_body=payload.issue_body,
            task_id=payload.task_id,
            max_chars=payload.max_chars,
        )
        return {"context": context}

    @app.post(
        "/api/v1/memory/record",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_201_CREATED,
    )
    async def record_memory(payload: RecordMemoryOutcome) -> dict[str, Any]:
        """Record a completed task's outcome to the coordinator's shared memory store."""
        daemon._memory.record_outcome(
            task_id=payload.task_id,
            repo=payload.repo,
            issue_number=payload.issue_number,
            issue_title=payload.issue_title,
            issue_body=payload.issue_body,
            plan=payload.plan,
            applied_diff=payload.applied_diff,
            pr_url=payload.pr_url,
            outcome=payload.outcome,
        )
        return {"status": "recorded"}

    @app.post(
        "/api/v1/issues",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_issue(payload: IssueCreate) -> dict[str, Any]:
        """Create a GitHub issue. Optionally dispatch the daemon immediately."""
        gh = _gh()
        url = await gh.create_issue(
            payload.repo,
            title=payload.title,
            body=payload.body,
            labels=payload.labels or None,
        )
        result: dict[str, Any] = {"url": url}

        if payload.dispatch:
            import re

            # Anchor to end-of-string so we only match the trailing issue
            # number that ``gh issue create`` actually returns (one URL per
            # line), not an ``/issues/NN`` fragment that happens to appear in
            # the issue body or in a repo slug like ``org/x-issues``.
            match = re.search(r"/issues/(\d+)/?\s*$", url.strip())
            if match:
                number = int(match.group(1))
                task = daemon.submit_issue(
                    repo=payload.repo, issue_number=number, mode=payload.mode
                )
                result["task_id"] = task.id
        return result

    @app.post(
        "/api/v1/issues/dispatch",
        dependencies=[Depends(auth), Depends(_require_admin())],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def dispatch_issue(payload: IssueDispatch) -> TaskView:
        """Queue an existing issue for the daemon to draft a PR for."""
        task = daemon.submit_issue(
            repo=payload.repo,
            issue_number=payload.number,
            mode=payload.mode,
            backend=payload.backend,
            model=payload.model,
            priority=payload.priority,
        )
        return TaskView.from_task(task)

    @app.get("/api/v1/artifacts/{artifact_id}", dependencies=[Depends(_require_viewer())])
    async def get_artifact(artifact_id: str) -> ArtifactView:
        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        return ArtifactView.from_artifact(artifact)

    @app.get("/api/v1/artifacts/{artifact_id}/content", dependencies=[Depends(_require_viewer())])
    async def get_artifact_content(artifact_id: str) -> Response:
        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        try:
            content = daemon.read_artifact_bytes(artifact_id)
        except ArtifactIntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return Response(content=content, media_type=artifact.media_type)

    @app.post(
        "/api/v1/issues/ab-dispatch",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ab_dispatch_issue(payload: IssueAbDispatch) -> dict[str, Any]:
        """Race multiple backends on the same issue — reviewer picks the winner."""
        available = set(daemon.state().backends_available)
        unknown = [b for b in payload.backends if b not in available]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown backend(s): {', '.join(unknown)}; available: {sorted(available)}",
            )
        try:
            tasks = daemon.submit_issue_ab(
                repo=payload.repo,
                issue_number=payload.number,
                backends=payload.backends,
                mode=payload.mode,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        return {
            "ab_group": tasks[0].ab_group,
            "tasks": [TaskView.from_task(t).model_dump(mode="json") for t in tasks],
        }

    @app.post(
        "/api/v1/issues/batch-dispatch",
        dependencies=[Depends(auth), Depends(_require_operator())],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def batch_dispatch_issues(payload: IssueBatchDispatch) -> dict[str, Any]:
        """Queue up to 100 issues in one call.

        Per-item failures (e.g. submit_issue raising ValueError) are recorded
        but don't abort the batch — callers get back separate dispatched/
        failed counts plus per-item details.
        """
        dispatched: list[TaskView] = []
        failures: list[dict[str, Any]] = []
        for item in payload.items:
            try:
                task = daemon.submit_issue(
                    repo=item.repo,
                    issue_number=item.number,
                    mode=item.mode,
                    backend=item.backend,
                    model=item.model,
                    priority=item.priority,
                )
                dispatched.append(TaskView.from_task(task))
            except Exception as e:
                failures.append({"repo": item.repo, "number": item.number, "error": str(e)})
        return {
            "dispatched": len(dispatched),
            "failed": len(failures),
            "tasks": [t.model_dump(mode="json") for t in dispatched],
            "failures": failures,
        }

    @app.get("/api/v1/issues/{owner}/{name}", dependencies=[Depends(_require_viewer())])
    async def list_repo_issues(
        owner: str, name: str, state: str = "open", limit: int = 25
    ) -> list[dict[str, Any]]:
        gh = _gh()
        issues = await gh.list_issues(f"{owner}/{name}", state=state, limit=limit)
        return [
            {
                "number": i.number,
                "title": i.title,
                "state": i.state,
                "labels": i.labels,
                "url": i.url,
            }
            for i in issues
        ]

    @app.post("/api/v1/heartbeat", dependencies=[Depends(auth)])
    async def worker_heartbeat(request: Request) -> dict[str, Any]:
        """Workers POST here every heartbeat_seconds to stay registered as alive.

        Body: {"machine_name": "<name>"}
        Coordinators use last-seen timestamps to detect dead workers and requeue
        their DISPATCHED tasks.
        """
        body = await request.json()
        machine_name = str(body.get("machine_name") or "")
        if not machine_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "machine_name required")
        daemon.record_worker_heartbeat(machine_name)
        return {
            "machine_name": machine_name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/fleet", dependencies=[Depends(_require_viewer())])
    async def fleet_overview() -> dict[str, Any]:
        """Return fleet manifest data merged with live task counts per repo.

        When role == "coordinator", also aggregates per-machine health summaries
        from remote workers via RemoteDaemonClient health checks.
        When role == "worker" or "standalone", shows local tasks only.
        """
        import os

        import yaml

        candidates = [
            os.environ.get("MAXWELL_FLEET_CONFIG") or "",
            "./fleet.yaml",
            str(_Path.home() / ".maxwell-daemon" / "fleet.yaml"),
        ]
        raw: dict[str, Any] = {}
        for path in candidates:
            if path:
                p = _Path(path)
                if p.is_file():
                    with p.open(encoding="utf-8") as fh:
                        raw = yaml.safe_load(fh) or {}
                    break

        fleet_section: dict[str, Any] = raw.get("fleet", {})
        repos_raw: list[dict[str, Any]] = raw.get("repos", [])

        default_slots: int = fleet_section.get("default_slots", 2)
        default_budget: float = fleet_section.get("default_budget_per_story", 0.50)
        default_branch: str = fleet_section.get("default_pr_target_branch", "staging")
        default_labels: list[str] = fleet_section.get("default_watch_labels", [])

        tasks = list(daemon.state().tasks.values())
        active_by_repo: dict[str, int] = {}
        cost_by_repo: dict[str, float] = {}
        for t in tasks:
            repo_name = (t.issue_repo or "").split("/")[-1] or t.repo or ""
            if not repo_name:
                continue
            if t.status.value in ("queued", "running", "dispatched"):
                active_by_repo[repo_name] = active_by_repo.get(repo_name, 0) + 1
            cost_by_repo[repo_name] = cost_by_repo.get(repo_name, 0.0) + t.cost_usd

        # In coordinator mode, include per-machine health summary from the fleet config.
        machines_summary: list[dict[str, Any]] = []
        if daemon._config.role == "coordinator" and daemon._config.fleet_machines:
            from maxwell_daemon.fleet.client import RemoteDaemonClient
            from maxwell_daemon.fleet.dispatcher import MachineState

            initial = tuple(
                MachineState(
                    name=m.name,
                    host=m.host,
                    port=m.port,
                    capacity=m.capacity,
                    tags=tuple(m.tags),
                )
                for m in daemon._config.fleet_machines
            )
            fleet_client = RemoteDaemonClient(auth_token=daemon._config.api_auth_token)
            try:
                probed = await fleet_client.refresh_all(initial)
            except Exception:
                probed = initial  # fall back to config data on probe failure

            # Count tasks dispatched to each machine.
            dispatched_per_machine: dict[str, int] = {}
            for t in tasks:
                from maxwell_daemon.daemon.runner import TaskStatus

                if t.status is TaskStatus.DISPATCHED and t.dispatched_to:
                    dispatched_per_machine[t.dispatched_to] = (
                        dispatched_per_machine.get(t.dispatched_to, 0) + 1
                    )

            for m in probed:
                last_seen = daemon._worker_last_seen.get(m.name)
                machines_summary.append(
                    {
                        "name": m.name,
                        "host": m.host,
                        "port": m.port,
                        "capacity": m.capacity,
                        "healthy": m.healthy,
                        "dispatched_tasks": dispatched_per_machine.get(m.name, 0),
                        "last_seen": last_seen.isoformat() if last_seen else None,
                    }
                )

        repos: list[dict[str, Any]] = []
        for r in repos_raw:
            name: str = r.get("name", "")
            org: str = r.get("org", "")
            repos.append(
                {
                    "name": name,
                    "org": org,
                    "github_url": (f"https://github.com/{org}/{name}" if org and name else None),
                    "slots": r.get("slots", default_slots),
                    "budget_per_story": r.get("budget_per_story", default_budget),
                    "pr_target_branch": r.get("pr_target_branch", default_branch),
                    "watch_labels": r.get("watch_labels", default_labels),
                    "active_tasks": active_by_repo.get(name, 0),
                    "total_cost_usd": round(cost_by_repo.get(name, 0.0), 6),
                }
            )

        result: dict[str, Any] = {
            "role": daemon._config.role,
            "fleet": {
                "name": fleet_section.get("name", ""),
                "auto_promote_staging": fleet_section.get("auto_promote_staging", False),
                "discovery_interval_seconds": fleet_section.get("discovery_interval_seconds", 300),
            },
            "repos": repos,
        }
        if machines_summary:
            result["machines"] = machines_summary
        return result

    @app.get(
        "/api/v1/fleet/capabilities",
        dependencies=[Depends(_require_viewer())],
    )
    @app.get("/api/v1/fleet/nodes", dependencies=[Depends(_require_viewer())])
    async def fleet_capabilities(
        repo: str = Query(..., min_length=1),
        tool: str = Query(..., min_length=1),
        required_capability: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        """Return a redacted capability registry snapshot for dispatch decisions."""

        status_view = daemon.fleet_registry.describe(
            repo=repo,
            tool=tool,
            required_capabilities=tuple(required_capability or ()),
        )
        return status_view.to_dict()

    @app.get("/api/v1/audit", dependencies=[Depends(_require_viewer())])
    async def audit_log(
        limit: int = Query(default=200, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """Return paginated audit log entries (oldest first)."""
        if _audit is None:
            return {"entries": [], "audit_enabled": False}
        return {
            "entries": _audit.entries(limit=limit, offset=offset),
            "audit_enabled": True,
        }

    @app.get("/api/v1/audit/verify", dependencies=[Depends(_require_viewer())])
    async def audit_verify() -> dict[str, Any]:
        """Verify the audit log hash chain.  Returns violations (empty = clean)."""
        from maxwell_daemon.audit import verify_chain

        if _audit is None or audit_log_path is None:
            return {"clean": True, "violations": [], "audit_enabled": False}
        violations = verify_chain(audit_log_path)
        return {
            "clean": len(violations) == 0,
            "violations": violations,
            "audit_enabled": True,
        }

    @app.post(
        "/api/reload",
        dependencies=[Depends(_require_operator())],
    )
    async def reload_config() -> dict[str, Any]:
        """Reload daemon config from disk without restarting.

        Atomically swaps the in-memory config and router so running workers are
        not interrupted. Requires operator role (or static bearer token when JWT
        is not configured).

        Returns the path that was reloaded and an ISO-8601 timestamp.
        """
        try:
            path = daemon.reload_config()
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"config reload failed: {exc}",
            ) from exc
        return {
            "status": "reloaded",
            "config_path": str(path),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/workers", dependencies=[Depends(_require_viewer())])
    async def workers_status() -> dict[str, Any]:
        """Return current worker count and queue depth."""
        state = daemon.state()
        return {
            "worker_count": state.worker_count,
            "queue_depth": state.queue_depth,
        }

    @app.get(
        "/api/v1/delegate-sessions",
        dependencies=[Depends(_require_viewer())],
        response_model=list[DelegateSessionSnapshot],
    )
    async def list_delegate_sessions(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        delegate_id: Annotated[str | None, Query()] = None,
        work_item_id: Annotated[str | None, Query()] = None,
        task_id: Annotated[str | None, Query()] = None,
        status: Annotated[str | None, Query()] = None,
    ) -> list[DelegateSessionSnapshot]:
        """List durable delegate sessions and their latest recovery evidence."""

        parsed_status = _parse_delegate_status(status)
        return daemon.delegate_lifecycle.list_sessions(
            limit=limit,
            delegate_id=delegate_id,
            work_item_id=work_item_id,
            task_id=task_id,
            status=parsed_status,
        )

    @app.get(
        "/api/v1/delegate-sessions/{session_id}",
        dependencies=[Depends(_require_viewer())],
        response_model=DelegateSessionSnapshot,
    )
    async def get_delegate_session(session_id: str) -> DelegateSessionSnapshot:
        """Return one durable delegate session snapshot."""

        snapshot = daemon.delegate_lifecycle.snapshot(session_id)
        return snapshot

    @app.put("/api/v1/workers", dependencies=[Depends(_require_operator())])
    async def set_workers(count: int) -> dict[str, Any]:
        """Rescale the worker pool to *count* workers."""
        try:
            await daemon.set_worker_count(count)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        return {"worker_count": count}

    @app.get("/api/v1/admin/prune", dependencies=[Depends(_require_admin())])
    async def prune_history(
        older_than_days: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, Any]:
        """Run retention pruning on demand."""
        days = (
            daemon._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        result = daemon.prune_retained_history(days)
        audit_removed = _audit.rotate() if _audit is not None else 0
        return {
            "older_than_days": days,
            "tasks_pruned": result["tasks"],
            "ledger_records_pruned": result["ledger_records"],
            "audit_entries_pruned": audit_removed,
        }

    @app.get("/api/v1/cost", dependencies=[Depends(_require_viewer())])
    async def cost_summary() -> CostSummary:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return CostSummary(
            month_to_date_usd=daemon._ledger.month_to_date(),
            by_backend=daemon._ledger.by_backend(start),
        )

    @app.post("/api/v1/webhooks/github")
    async def github_webhook(request: Request) -> Response:
        """Receive GitHub webhook events.

        Authenticated with HMAC-SHA256 via X-Hub-Signature-256. No bearer-token
        dependency is applied so GitHub's retry delivery system isn't double-gated.
        """
        import json as _json

        from fastapi.responses import JSONResponse

        from maxwell_daemon.gh.webhook import (
            WebhookConfig,
            WebhookRoute,
            WebhookRouter,
            verify_signature,
        )

        body = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")
        event_type = request.headers.get("x-github-event", "")

        config_secret = daemon._config.github_webhook_secret_value()
        if config_secret is None:
            return JSONResponse(
                {"detail": "webhooks disabled", "disabled": True},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not verify_signature(config_secret, body, signature):
            return JSONResponse(
                {"detail": "invalid signature"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            payload = _json.loads(body) if body else {}
        except _json.JSONDecodeError:
            return JSONResponse(
                {"detail": "malformed json"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        routes = [
            WebhookRoute(
                event=r.event,
                action=r.action,
                mode=r.mode,  # type: ignore[arg-type]
                label=r.label,
                trigger=r.trigger,
            )
            for r in daemon._config.github_routes
        ]
        router = WebhookRouter(
            WebhookConfig(
                secret=config_secret,
                allowed_repos=daemon._config.github_allowed_repos,
                routes=routes,
            ),
            daemon=daemon,
        )
        dispatches = router.handle(event_type=event_type, payload=payload)
        return JSONResponse(
            {"event": event_type, "dispatched": len(dispatches)},
            status_code=status.HTTP_200_OK,
        )

    # ── SSH endpoints ────────────────────────────────────────────────────────
    # asyncssh is optional (pip install maxwell-daemon[ssh]).  All SSH routes
    # return 503 if it is not installed.

    _ssh_pool_ref: dict[str, Any] = {}  # lazy singleton

    def _ssh_pool() -> Any:
        if "pool" not in _ssh_pool_ref:
            try:
                import asyncssh as _asyncssh  # noqa: F401 — presence check only

                from maxwell_daemon.ssh.session import SSHSessionPool

                _ssh_pool_ref["pool"] = SSHSessionPool()
            except ImportError:
                _ssh_pool_ref["pool"] = None
        return _ssh_pool_ref.get("pool")

    def _ssh_unavailable() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "SSH support not installed — pip install maxwell-daemon[ssh]"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/api/v1/ssh/sessions", dependencies=[Depends(_require_admin())])
    async def ssh_sessions() -> Any:
        """List active SSH sessions."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        return {"sessions": pool.sessions()}

    @app.get("/api/v1/ssh/keys", dependencies=[Depends(_require_admin())])
    async def ssh_list_keys() -> Any:
        """List machines that have stored SSH keys."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        return {"machines": store.list_machines()}

    @app.get("/api/v1/ssh/keys/{machine}", dependencies=[Depends(_require_admin())])
    async def ssh_get_key(machine: str) -> Any:
        """Return the public key for *machine*, generating it if absent."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        _, pub = store.get_or_generate(machine)
        return {"machine": machine, "public_key": pub}

    @app.delete("/api/v1/ssh/keys/{machine}", dependencies=[Depends(_require_admin())])
    async def ssh_delete_key(machine: str) -> Any:
        """Remove stored SSH keys for *machine*."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        SSHKeyStore().remove(machine)
        return {"machine": machine, "deleted": True}

    @app.post("/api/v1/ssh/connect", dependencies=[Depends(auth), Depends(_require_admin())])
    async def ssh_connect(payload: SSHConnectRequest) -> Any:
        """Open (or reuse) an SSH session and return its summary."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(
            payload.host,
            user=payload.user,
            port=payload.port,
            password=payload.password,
        )
        return {
            "host": payload.host,
            "port": payload.port,
            "user": payload.user,
            "age_seconds": round(session.age_seconds, 1),
        }

    @app.post("/api/v1/ssh/run", dependencies=[Depends(auth), Depends(_require_admin())])
    async def ssh_run(payload: SSHRunRequest) -> Any:
        """Run a command on a remote machine and return its output."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(payload.host, user=payload.user, port=payload.port)
        result = await session.run(payload.command, timeout=payload.timeout_seconds)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    @app.get("/api/v1/ssh/files", dependencies=[Depends(_require_admin())])
    async def ssh_list_files(
        host: str = Query(...),
        user: str = Query(...),
        port: int = Query(default=22),
        path: str = Query(default="/"),
    ) -> Any:
        """List files on a remote machine via SFTP."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(host, user=user, port=port)
        entries = await session.list_dir(path)
        return {
            "path": path,
            "entries": [
                {
                    "name": e.name,
                    "path": e.path,
                    "size": e.size,
                    "is_dir": e.is_dir,
                    "modified": e.modified,
                }
                for e in entries
            ],
        }

    # Whitelist of shell commands that are permitted over the SSH WebSocket.
    # Only bare command names (no arguments) are accepted — the interactive
    # shell session itself handles all subsequent user input.
    _ssh_allowed_commands: frozenset[str] = frozenset({"bash", "sh", "zsh", "fish", "rbash"})

    @app.websocket("/api/v1/ssh/shell")
    async def ssh_shell_ws(ws: WebSocket) -> None:
        """Interactive shell over WebSocket.

        Query params: ``host``, ``user``, ``port`` (default 22), ``token``
        (bearer token for auth), ``command`` (default ``bash``).

        The ``command`` parameter is validated against an explicit whitelist of
        permitted shell executables.  Arbitrary shell strings, pipes, and
        redirections are rejected to prevent remote code execution via command
        injection (CVE / Issue #138).

        Frames: text frames sent from client are written to stdin.
        Text frames sent to client contain stdout/stderr chunks.
        Session ends when the command exits or the client disconnects.
        Max session duration: 1 hour.
        """
        import json as _json_mod

        if not await _websocket_auth_or_close(ws, Role.admin, auth_token, jwt_config):
            return

        pool = _ssh_pool()
        if pool is None:
            await ws.accept()
            await ws.send_text('{"error": "SSH not installed"}')
            await ws.close(code=1011)
            return

        host = ws.query_params.get("host") or ""
        user = ws.query_params.get("user") or ""

        # Validate port — must be a valid integer in 1-65535
        raw_port = ws.query_params.get("port") or "22"
        try:
            port = int(raw_port)
            if not (1 <= port <= 65535):
                raise ValueError("port out of range")
        except ValueError:
            await ws.accept()
            await ws.send_text('{"error": "invalid port"}')
            await ws.close(code=1008)
            return

        # Validate command against whitelist — reject anything that is not a
        # known-safe shell executable name.  This prevents injection of shell
        # metacharacters, pipes, subshells, or arbitrary binaries.
        raw_command = ws.query_params.get("command") or "bash"
        command = raw_command.strip()
        if command not in _ssh_allowed_commands:
            await ws.accept()
            await ws.send_text(
                _json_mod.dumps(
                    {
                        "error": (
                            f"command {command!r} is not permitted; "
                            f"allowed: {sorted(_ssh_allowed_commands)}"
                        )
                    }
                )
            )
            await ws.close(code=1008)
            return

        if not host or not user:
            await ws.accept()
            await ws.send_text('{"error": "host and user are required"}')
            await ws.close(code=1008)
            return

        await ws.accept()
        try:
            session = await pool.get(host, user=user, port=port)
            # Pass command as a single-element list so asyncssh treats it as an
            # exec request rather than a shell string — no shell interpolation.
            async for chunk in session.shell_stream(command):
                await ws.send_bytes(chunk)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await ws.send_text(_json_mod.dumps({"error": str(exc)}))
            await ws.close(code=1011)

    @app.websocket("/api/v1/events")
    async def events_ws(ws: WebSocket) -> None:
        """Stream daemon events as JSON frames to the client.

        Clients pass ``?token=...`` as a query param because browser WebSocket
        APIs can't set headers. Static tokens grant admin; JWT tokens must have
        viewer-or-higher privileges. Terminate at a proxy for TLS.
        """
        if not await _websocket_auth_or_close(ws, Role.viewer, auth_token, jwt_config):
            return
        await ws.accept()
        try:
            async for event in daemon.events.subscribe(queue_size=64):
                await ws.send_text(event.to_json())
        except WebSocketDisconnect:
            return
        except Exception:
            await ws.close(code=1011)

    return app
