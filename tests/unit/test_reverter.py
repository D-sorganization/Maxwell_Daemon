from pathlib import Path

import pytest

from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.actions import Action, ActionKind, ActionStatus
from maxwell_daemon.core.reverter import ActionReverter


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def action_service() -> ActionService:
    # We can mock this simply because reverter only calls mark_reverted
    class MockService:
        def mark_reverted(self, action_id: str) -> None:
            self.reverted_id = action_id

    return MockService()  # type: ignore


def test_revert_file_write_creation(workspace: Path, action_service: ActionService) -> None:
    reverter = ActionReverter(workspace, action_service)

    test_file = workspace / "new_file.txt"
    test_file.write_text("hello", encoding="utf-8")

    action = Action(
        id="act-1",
        task_id="task-1",
        kind=ActionKind.FILE_WRITE,
        status=ActionStatus.APPLIED,
        summary="wrote a file",
        payload={"path": "new_file.txt"},
        inverse_payload={"existed": False},
    )

    reverter.revert(action)
    assert action_service.reverted_id == "act-1"
    assert not test_file.exists()


def test_revert_file_write_overwrite(workspace: Path, action_service: ActionService) -> None:
    reverter = ActionReverter(workspace, action_service)

    test_file = workspace / "existing_file.txt"
    test_file.write_text("new content", encoding="utf-8")

    action = Action(
        id="act-2",
        task_id="task-1",
        kind=ActionKind.FILE_WRITE,
        status=ActionStatus.APPLIED,
        summary="overwrote a file",
        payload={"path": "existing_file.txt"},
        inverse_payload={"existed": True, "old_content": "old content"},
    )

    reverter.revert(action)
    assert action_service.reverted_id == "act-2"
    assert test_file.read_text(encoding="utf-8") == "old content"


def test_revert_file_edit(workspace: Path, action_service: ActionService) -> None:
    reverter = ActionReverter(workspace, action_service)

    test_file = workspace / "edit_file.txt"
    test_file.write_text("this is the edited text", encoding="utf-8")

    action = Action(
        id="act-3",
        task_id="task-1",
        kind=ActionKind.FILE_EDIT,
        status=ActionStatus.APPLIED,
        summary="edited a file",
        payload={"path": "edit_file.txt"},
        inverse_payload={"existed": True, "old_content": "this is the original text"},
    )

    reverter.revert(action)
    assert action_service.reverted_id == "act-3"
    assert test_file.read_text(encoding="utf-8") == "this is the original text"


def test_revert_unsupported_kind(workspace: Path, action_service: ActionService) -> None:
    reverter = ActionReverter(workspace, action_service)
    action = Action(
        id="act-4",
        task_id="task-1",
        kind=ActionKind.COMMAND,
        status=ActionStatus.APPLIED,
        summary="ran a command",
    )
    with pytest.raises(NotImplementedError):
        reverter.revert(action)


def test_revert_wrong_status(workspace: Path, action_service: ActionService) -> None:
    reverter = ActionReverter(workspace, action_service)
    action = Action(
        id="act-5",
        task_id="task-1",
        kind=ActionKind.FILE_WRITE,
        status=ActionStatus.PROPOSED,
        summary="unapplied write",
    )
    with pytest.raises(ValueError, match="Cannot revert action in status"):
        reverter.revert(action)
