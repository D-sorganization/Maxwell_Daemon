"""Service boundary for task workspace lifecycle operations."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from maxwell_daemon.core.workspace_store import WorkspaceStore
from maxwell_daemon.core.workspaces import TaskWorkspace, WorkspaceCheckpoint

__all__ = ["WorkspacePathResolver", "WorkspaceService"]


class WorkspacePathResolver(Protocol):
    def path_for(self, repo: str, *, task_id: str) -> Path:
        """Return the validated checkout path for a repo/task pair."""


class WorkspaceService:
    """Coordinates path validation and durable workspace metadata."""

    def __init__(
        self,
        *,
        path_resolver: WorkspacePathResolver,
        store: WorkspaceStore,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._path_resolver = path_resolver
        self._store = store
        self._id_factory = id_factory or (lambda: uuid4().hex)

    def create_session(
        self,
        *,
        repo: str,
        task_id: str,
        base_branch: str = "main",
        work_branch: str | None = None,
        work_item_id: str | None = None,
        current_head: str | None = None,
        base_head: str | None = None,
    ) -> TaskWorkspace:
        path = self._path_resolver.path_for(repo, task_id=task_id)
        session = TaskWorkspace(
            id=self._id_factory(),
            task_id=task_id,
            work_item_id=work_item_id,
            repo=repo,
            path=str(path),
            base_branch=base_branch,
            work_branch=work_branch or f"maxwell-daemon/{task_id}",
            current_head=current_head,
            base_head=base_head,
        )
        self._store.create_workspace(session)
        return session

    def create_checkpoint(
        self,
        *,
        workspace_id: str,
        label: str,
        git_ref: str,
        diff_artifact_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> WorkspaceCheckpoint:
        checkpoint = WorkspaceCheckpoint(
            id=self._id_factory(),
            workspace_id=workspace_id,
            label=label,
            git_ref=git_ref,
            diff_artifact_id=diff_artifact_id,
            metadata=metadata or {},
        )
        return self._store.create_checkpoint(checkpoint)

    def list_checkpoints(self, workspace_id: str) -> list[WorkspaceCheckpoint]:
        return self._store.list_checkpoints(workspace_id)
