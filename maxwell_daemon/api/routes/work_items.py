"""Work-item and task-graph endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896 Phase 1.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.work_items import (
    REPO_PATTERN,
    AcceptanceCriterion,
    ScopeBoundary,
    WorkItem,
    WorkItemStatus,
)
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.director import (
    GraphStatus,
    NodeRun,
    TaskGraph,
    TaskGraphExecutorUnavailableError,
    TaskGraphRecord,
    TaskGraphTemplate,
)
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "NodeRunView",
    "TaskGraphCreate",
    "TaskGraphView",
    "WorkItemCreate",
    "WorkItemPatch",
    "WorkItemTransition",
    "WorkItemView",
    "register",
]


class WorkItemCreate(BaseModel):
    id: str | None = Field(default=None, min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    repo: str | None = None
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
    repo: str | None = None
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


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    require_viewer: Any,
    require_operator: Any,
    auth: Any,
) -> None:
    """Attach work-item and task-graph endpoints to ``app``."""

    @app.post(
        "/api/v1/work-items",
        dependencies=[Depends(auth), Depends(require_operator)],
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

    @app.get("/api/v1/work-items", dependencies=[Depends(require_viewer)])
    async def list_work_items(
        status_filter: Annotated[WorkItemStatus | None, Query(alias="status")] = None,
        repo: Annotated[str | None, Query(pattern=REPO_PATTERN)] = None,
        source: Annotated[str | None, Query(pattern=r"^(manual|github_issue|gaai|api)$")] = None,
        max_priority: Annotated[int | None, Query(ge=0, le=1000)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[WorkItemView]:
        items = daemon.list_work_items(
            limit=limit, status=status_filter, repo=repo, source=source, max_priority=max_priority
        )
        return [WorkItemView.from_item(item) for item in items]

    @app.get("/api/v1/work-items/{item_id}", dependencies=[Depends(require_viewer)])
    async def get_work_item(item_id: str) -> WorkItemView:
        item = daemon.get_work_item(item_id)
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "work item not found")
        return WorkItemView.from_item(item)

    @app.get("/api/v1/work-items/{item_id}/artifacts", dependencies=[Depends(require_viewer)])
    async def list_work_item_artifacts(
        item_id: str,
        kind: Annotated[ArtifactKind | None, Query()] = None,
    ) -> list[Any]:
        from maxwell_daemon.api.routes.actions import ArtifactView

        return [
            ArtifactView.from_artifact(a)
            for a in daemon.list_work_item_artifacts(item_id, kind=kind)
        ]

    @app.patch(
        "/api/v1/work-items/{item_id}",
        dependencies=[Depends(auth), Depends(require_operator)],
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
        dependencies=[Depends(auth), Depends(require_operator)],
    )
    async def transition_work_item_endpoint(
        item_id: str, payload: WorkItemTransition
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
        dependencies=[Depends(auth), Depends(require_operator)],
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

    @app.get("/api/v1/task-graphs", dependencies=[Depends(require_viewer)])
    async def list_task_graphs(
        work_item_id: Annotated[str | None, Query()] = None,
        status_filter: Annotated[GraphStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[TaskGraphView]:
        return [
            TaskGraphView.from_record(r)
            for r in daemon.list_task_graphs(
                work_item_id=work_item_id, status=status_filter, limit=limit
            )
        ]

    @app.get("/api/v1/task-graphs/{graph_id}", dependencies=[Depends(require_viewer)])
    async def get_task_graph(graph_id: str) -> TaskGraphView:
        record = daemon.get_task_graph(graph_id)
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task graph not found")
        return TaskGraphView.from_record(record)

    @app.post(
        "/api/v1/task-graphs/{graph_id}/start",
        dependencies=[Depends(auth), Depends(require_operator)],
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
