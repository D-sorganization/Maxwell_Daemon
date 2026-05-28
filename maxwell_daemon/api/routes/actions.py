"""Actions and artifacts endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896 Phase 1.1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from maxwell_daemon.core.actions import Action, ActionStatus
from maxwell_daemon.core.artifacts import Artifact, ArtifactIntegrityError, ArtifactKind
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = ["ActionRejectRequest", "ActionView", "ArtifactView", "register"]


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


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    audit: Any,
    require_viewer: Any,
    require_operator: Any,
    auth: Any,
) -> None:
    """Attach action/artifact endpoints to ``app``."""

    @app.get("/api/v1/tasks/{task_id}/artifacts", dependencies=[Depends(require_viewer)])
    async def list_task_artifacts(
        task_id: str,
        kind: Annotated[ArtifactKind | None, Query()] = None,
    ) -> list[ArtifactView]:
        return [
            ArtifactView.from_artifact(a) for a in daemon.list_task_artifacts(task_id, kind=kind)
        ]

    @app.get("/api/v1/tasks/{task_id}/actions", dependencies=[Depends(require_viewer)])
    async def list_task_actions(task_id: str) -> list[ActionView]:
        return [ActionView.from_action(a) for a in daemon.list_task_actions(task_id)]

    @app.get("/api/v1/actions", dependencies=[Depends(require_viewer)])
    async def list_actions(
        status_filter: Annotated[ActionStatus | None, Query(alias="status")] = None,
        task_id: Annotated[str | None, Query()] = None,
        work_item_id: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[ActionView]:
        return [
            ActionView.from_action(a)
            for a in daemon.list_actions(
                status=status_filter,
                task_id=task_id,
                work_item_id=work_item_id,
                limit=limit,
            )
        ]

    @app.get("/api/v1/actions/{action_id}", dependencies=[Depends(require_viewer)])
    async def get_action(action_id: str) -> ActionView:
        action = daemon.get_action(action_id)
        if action is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found")
        return ActionView.from_action(action)

    @app.post(
        "/api/v1/actions/{action_id}/approve",
        dependencies=[Depends(auth), Depends(require_operator)],
    )
    async def approve_action(action_id: str) -> ActionView:
        try:
            action = daemon.approve_action(action_id, actor="api", audit=audit)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return ActionView.from_action(action)

    @app.post(
        "/api/v1/actions/{action_id}/reject",
        dependencies=[Depends(auth), Depends(require_operator)],
    )
    async def reject_action(action_id: str, payload: ActionRejectRequest) -> ActionView:
        try:
            action = daemon.reject_action(
                action_id, actor="api", reason=payload.reason, audit=audit
            )
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "action not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return ActionView.from_action(action)

    @app.get("/api/v1/artifacts/{artifact_id}", dependencies=[Depends(require_viewer)])
    async def get_artifact(artifact_id: str) -> ArtifactView:
        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        return ArtifactView.from_artifact(artifact)

    @app.get(
        "/api/v1/artifacts/{artifact_id}/content",
        dependencies=[Depends(require_viewer)],
    )
    async def get_artifact_content(artifact_id: str) -> Response:
        artifact = daemon.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not found")
        try:
            content = daemon.read_artifact_bytes(artifact_id)
        except ArtifactIntegrityError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return Response(content=content, media_type=artifact.media_type)
