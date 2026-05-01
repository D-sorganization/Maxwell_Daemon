"""Control plane endpoints for task lifecycle management.

Extracted from ``maxwell_daemon/api/server.py`` as part of issue #793
decomposition. These endpoints provide the control-plane gauntlet interface
for retrying, cancelling, and waiving task gates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskStatus
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


class GateTimelineEntry(BaseModel):
    """Timeline entry for a gate in the control plane."""

    id: str
    name: str
    status: Literal["passed", "failed", "blocked", "waived", "running", "pending"]
    evidence_links: tuple[str, ...] = ()
    next_action: str | None = None
    retry_allowed: bool = False
    waiver_allowed: bool = False


class CriticFindingView(BaseModel):
    """Finding from the critic review process."""

    severity: Literal["blocker", "warning", "note"]
    critic: str
    message: str
    file: str | None = None
    line: int | None = None
    evidence: str | None = None


class DelegateSessionView(BaseModel):
    """View of a delegate session."""

    id: str
    role: str
    status: str
    machine: str | None = None
    backend: str | None = None
    latest_checkpoint: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float | None = None


class ResourceRoutingView(BaseModel):
    """Resource routing decision information."""

    selected_backend: str | None
    selected_model: str | None = None
    selection_reason: str | None = None
    alternatives_considered: tuple[str, ...] = ()
    warning: str | None = None


class ControlPlaneActionView(BaseModel):
    """Action available in the control plane."""

    kind: Literal["retry", "waive", "cancel"]
    label: str
    path: str
    target_id: str
    expected_status: Literal["failed", "queued"]
    requires_reason: bool = False
    requires_actor: bool = False


class ControlPlaneWorkItemView(BaseModel):
    """View model for control plane work items."""

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


class GateRetryRequest(BaseModel):
    """Request to retry a failed gate."""

    target_id: str = Field(..., min_length=1)
    expected_status: Literal["failed"] = "failed"


class GateCancelRequest(BaseModel):
    """Request to cancel a queued gate."""

    target_id: str = Field(..., min_length=1)
    expected_status: Literal["queued"] = "queued"


class GateWaiverRequest(BaseModel):
    """Request to waive a failed gate."""

    target_id: str = Field(..., min_length=1)
    expected_status: Literal["failed"] = "failed"
    actor: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1, max_length=1000)


def _task_title(task: Task) -> str:
    """Get a human-readable title for a task."""
    if task.issue_repo and task.issue_number is not None:
        return f"{task.issue_repo}#{task.issue_number}"
    return task.prompt[:80]


def _duration_seconds(task: Task) -> float | None:
    """Calculate duration in seconds for a task."""
    if task.started_at is None:
        return None
    end = task.finished_at or datetime.now(timezone.utc)
    return max(0.0, (end - task.started_at).total_seconds())


def _task_is_waived(task: Task) -> bool:
    """Check if a task has been waived."""
    return bool(task.waived_by and task.waiver_reason)


def _control_plane_actions_for_task(task: Task) -> tuple[ControlPlaneActionView, ...]:
    """Get available control plane actions for a task."""
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
    """Get gate timeline entries for a task."""
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
    """Get critic findings for a task."""
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


def _delegate_snapshots_for_task(daemon: Daemon, task: Task, *, limit: int = 20) -> tuple[Any, ...]:
    """Get delegate session snapshots for a task."""
    snapshots = daemon.delegate_lifecycle.list_sessions(limit=limit, task_id=task.id)
    ordered = sorted(
        snapshots,
        key=lambda snapshot: (
            snapshot.session.updated_at,
            (
                snapshot.latest_checkpoint.created_at
                if snapshot.latest_checkpoint
                else snapshot.session.created_at
            ),
        ),
        reverse=True,
    )
    return tuple(ordered)


def _delegate_views_for_task(
    _daemon: Daemon, task: Task, snapshots: tuple[Any, ...]
) -> tuple[DelegateSessionView, ...]:
    """Convert delegate snapshots to view models."""
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
    daemon: Daemon, snapshots: tuple[Any, ...]
) -> tuple[str | None, str | None]:
    """Get work item context for a task."""
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
    """Build a control plane view from a task."""
    gates = _gate_statuses_for_task(task)
    snapshots = _delegate_snapshots_for_task(daemon, task)
    work_item_id, work_item_status = _work_item_context_for_task(daemon, snapshots)
    current_gate = next(
        (gate.name for gate in gates if gate.status in {"failed", "blocked", "running", "pending"}),
        None,
    )
    decision_by_status: dict[
        str,
        Literal["pass", "fail", "blocked", "running", "pending", "cancelled", "waived"],
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


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    auth_dep: Any,
    viewer_dep: Any,
    operator_dep: Any,
) -> None:
    """Attach control plane endpoints to ``app``."""

    @app.get(
        "/api/v1/control-plane/gauntlet",
        response_model=tuple[ControlPlaneWorkItemView, ...],
        dependencies=[Depends(viewer_dep)],
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
        dependencies=[Depends(auth_dep), Depends(operator_dep)],
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
        dependencies=[Depends(auth_dep), Depends(operator_dep)],
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
        dependencies=[Depends(auth_dep), Depends(operator_dep)],
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
