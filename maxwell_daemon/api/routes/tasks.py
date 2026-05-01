"""Task endpoints for task submission, listing, and inspection.

Extracted from ``maxwell_daemon/api/server.py`` as part of issue #793
decomposition. These endpoints manage the task lifecycle: submission,
listing, retrieval, and cancellation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from maxwell_daemon.api.contract import TaskDetail, TaskListResponse, TaskSummary
from maxwell_daemon.api.validation import PromptField, RepoField
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import DuplicateTaskIdError, Task, TaskStatus
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


class TaskSubmit(BaseModel):
    """Request model for task submission."""

    prompt: PromptField
    task_id: str | None = None
    kind: str = "prompt"
    repo: RepoField | None = None
    backend: str | None = None
    model: str | None = None
    issue_repo: RepoField | None = None
    issue_number: int | None = None
    issue_mode: Literal["plan", "implement"] | None = None
    priority: int = Field(default=100, ge=0, le=1000)
    dry_run: bool = False
    depends_on: list[str] = Field(
        default_factory=list,
        description="Task IDs that must reach COMPLETED before this task starts.",
    )


class TaskView(BaseModel):
    """View model for task representation."""

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
    thread_id: str | None = None
    turn_count: int = 0
    max_turns: int = 20
    session_id: str
    continuation: bool = False
    depends_on: list[str] = Field(default_factory=list)
    priority: int = 100
    pr_url: str | None = None
    dispatched_to: str | None = None
    side_effects_started: bool = False
    status: str
    result: str | None
    error: str | None
    waived_by: str | None = None
    waiver_reason: str | None = None
    waived_at: datetime | None = None
    dry_run: bool = False
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
            thread_id=t.thread_id,
            turn_count=t.turn_count,
            max_turns=t.max_turns,
            session_id=t.turn_session_id,
            continuation=t.is_continuation_turn,
            depends_on=list(getattr(t, "depends_on", [])),
            priority=getattr(t, "priority", 100),
            pr_url=t.pr_url,
            dispatched_to=t.dispatched_to,
            side_effects_started=t.side_effects_started,
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
            dry_run=getattr(t, "dry_run", False),
        )


def _coerce_datetime_to_utc(value: datetime) -> datetime:
    """Normalize datetime to UTC timezone."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    auth_dep: Any,
    viewer_dep: Any,
    operator_dep: Any,
) -> None:
    """Attach task-related endpoints to ``app``."""

    @app.post(
        "/api/v1/tasks",
        dependencies=[Depends(auth_dep), Depends(operator_dep)],
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
                        dry_run=payload.dry_run,
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
                    depends_on=payload.depends_on or [],
                    dry_run=payload.dry_run,
                )
        except DuplicateTaskIdError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return TaskView.from_task(task)

    @app.get("/api/v1/tasks", dependencies=[Depends(viewer_dep)])
    async def list_tasks(
        status: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        repo: Annotated[str | None, Query()] = None,
        cursor: Annotated[datetime | None, Query()] = None,
        completed_before: Annotated[datetime | None, Query()] = None,
        completed_before_camel: Annotated[datetime | None, Query(alias="completedBefore")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[TaskView]:
        completed_before_filter = completed_before or completed_before_camel
        if completed_before_filter is not None:
            completed_before_filter = _coerce_datetime_to_utc(completed_before_filter)
        if cursor is not None:
            cursor = _coerce_datetime_to_utc(cursor)

        task_status: TaskStatus | None = None
        if status is not None:
            try:
                task_status = TaskStatus(status)
            except ValueError as exc:
                raise HTTPException(422, f"invalid task status: {status}") from exc

        tasks = await daemon._task_store.alist_tasks(
            limit=limit,
            status=task_status,
            repo=repo,
            kind=kind,
            cursor=cursor,
            completed_before=completed_before_filter,
        )
        return [TaskView.from_task(t) for t in tasks]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(viewer_dep)])
    async def get_task(task_id: str) -> TaskView:
        t = daemon.get_task(task_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return TaskView.from_task(t)

    @app.post(
        "/api/v1/tasks/{task_id}/cancel",
        dependencies=[Depends(auth_dep), Depends(operator_dep)],
    )
    async def cancel_task(task_id: str) -> TaskView:
        try:
            cancelled = daemon.cancel_task(task_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from None
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
        return TaskView.from_task(cancelled)

    @app.get("/api/tasks", dependencies=[Depends(viewer_dep)])
    async def api_list_tasks(
        limit: int = Query(ge=1, le=1000),
        cursor: str | None = Query(default=None),
    ) -> TaskListResponse:
        tasks = await daemon._task_store.alist_tasks(limit=limit)
        summaries = [
            TaskSummary(
                id=t.id,
                status=t.status.value,
                created_at=t.created_at.isoformat(),
                repo=t.repo,
                prompt_preview=t.prompt[:120] if t.prompt else "",
            )
            for t in tasks
        ]
        return TaskListResponse(
            tasks=summaries,
            next_cursor=None,
            total=len(summaries),
        )

    @app.get("/api/tasks/{task_id}", dependencies=[Depends(viewer_dep)])
    async def api_get_task(task_id: str) -> TaskDetail:
        t = daemon.get_task(task_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return TaskDetail(
            id=t.id,
            status=t.status.value,
            created_at=t.created_at.isoformat(),
            repo=t.repo,
            transcript=[],
            artifacts=[],
        )
