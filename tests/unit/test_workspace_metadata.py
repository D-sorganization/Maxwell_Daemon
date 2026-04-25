from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.core.workspace_service import WorkspaceService
from maxwell_daemon.core.workspace_store import WorkspaceStore
from maxwell_daemon.core.workspaces import (
    TaskWorkspace,
    WorkspaceCheckpoint,
    WorkspaceStatus,
    validate_workspace_transition,
)
from maxwell_daemon.gh.workspace import Workspace, WorkspaceError


def _ids(*values: str):  # type: ignore[no-untyped-def]
    pending = iter(values)
    return lambda: next(pending)


def test_workspace_model_rejects_non_monotonic_timestamps(tmp_path: Path) -> None:
    created = datetime.now(timezone.utc)

    with pytest.raises(ValidationError, match="updated_at cannot be before created_at"):
        TaskWorkspace(
            id="ws-1",
            task_id="task-1",
            repo="owner/repo",
            path=str(tmp_path),
            base_branch="main",
            work_branch="feature",
            created_at=created,
            updated_at=created - timedelta(seconds=1),
            last_used_at=created,
        )


def test_workspace_transition_validation_fails_closed() -> None:
    validate_workspace_transition(WorkspaceStatus.CREATING, WorkspaceStatus.READY)

    with pytest.raises(ValueError, match="invalid workspace transition"):
        validate_workspace_transition(WorkspaceStatus.DELETED, WorkspaceStatus.READY)


def test_service_reuses_workspace_path_validation(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspaces.db")
    workspace = Workspace(root=tmp_path / "root")
    service = WorkspaceService(
        path_resolver=workspace,
        store=store,
        id_factory=_ids("ws-1"),
    )

    session = service.create_session(repo="owner/repo", task_id="task-123")

    assert session.id == "ws-1"
    assert session.task_id == "task-123"
    assert session.work_branch == "maxwell-daemon/task-123"
    assert Path(session.path).is_relative_to((tmp_path / "root").resolve())
    assert store.get_workspace_for_task("task-123") == session


def test_service_rejects_invalid_repo_before_persisting(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspaces.db")
    service = WorkspaceService(
        path_resolver=Workspace(root=tmp_path / "root"),
        store=store,
        id_factory=_ids("ws-1"),
    )

    with pytest.raises(WorkspaceError, match="Invalid repo"):
        service.create_session(repo="bad repo", task_id="task-123")

    assert store.get_workspace_for_task("task-123") is None


def test_store_transitions_and_updates_workspace_metadata(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspaces.db")
    workspace = TaskWorkspace(
        id="ws-1",
        task_id="task-1",
        repo="owner/repo",
        path=str(tmp_path / "repo" / "task-1"),
        base_branch="main",
        work_branch="feature",
    )
    store.create_workspace(workspace)

    ready = store.transition("ws-1", WorkspaceStatus.READY)
    assert ready.status is WorkspaceStatus.READY

    updated = store.update_heads("ws-1", current_head="abc123", base_head="def456")
    assert updated.current_head == "abc123"
    assert updated.base_head == "def456"
    assert updated.last_used_at >= ready.last_used_at

    with pytest.raises(ValueError, match="invalid workspace transition"):
        store.transition("ws-1", WorkspaceStatus.DELETED)


def test_store_creates_and_lists_checkpoints(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspaces.db")
    store.create_workspace(
        TaskWorkspace(
            id="ws-1",
            task_id="task-1",
            repo="owner/repo",
            path=str(tmp_path / "repo" / "task-1"),
            base_branch="main",
            work_branch="feature",
        )
    )

    checkpoint = store.create_checkpoint(
        WorkspaceCheckpoint(
            id="cp-1",
            workspace_id="ws-1",
            label="before-initial-diff",
            git_ref="refs/maxwell/checkpoints/task-1/cp-1",
            metadata={"phase": "before_diff"},
        )
    )

    assert store.list_checkpoints("ws-1") == [checkpoint]
    assert store.get_workspace("ws-1").checkpoint_count == 1  # type: ignore[union-attr]


def test_service_creates_checkpoints_for_existing_workspace(tmp_path: Path) -> None:
    store = WorkspaceStore(tmp_path / "workspaces.db")
    service = WorkspaceService(
        path_resolver=Workspace(root=tmp_path / "root"),
        store=store,
        id_factory=_ids("ws-1", "cp-1"),
    )
    session = service.create_session(repo="owner/repo", task_id="task-123")

    checkpoint = service.create_checkpoint(
        workspace_id=session.id,
        label="validated",
        git_ref="refs/maxwell/checkpoints/task-123/cp-1",
        metadata={"checks": ["pytest"]},
    )

    assert checkpoint.id == "cp-1"
    assert service.list_checkpoints(session.id) == [checkpoint]
