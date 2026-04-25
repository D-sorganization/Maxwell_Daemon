"""Action ledger model, policy, store, and service behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.core.action_policy import ActionPolicy, ApprovalMode
from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.core.actions import Action, ActionKind, ActionStatus


def _action(**overrides: object) -> Action:
    data = {
        "id": "act-1",
        "task_id": "task-1",
        "kind": ActionKind.FILE_WRITE,
        "summary": "write file",
        "payload": {"path": "src/app.py"},
    }
    data.update(overrides)  # type: ignore[arg-type]
    return Action.model_validate(data)


class TestActionTransitions:
    def test_cannot_approve_non_proposed_action(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action(status=ActionStatus.REJECTED)
        store.save(action)

        with pytest.raises(ValueError, match="invalid action transition"):
            store.transition(action.id, ActionStatus.APPROVED, actor="operator")

    def test_cannot_apply_rejected_action(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action(status=ActionStatus.REJECTED)
        store.save(action)

        with pytest.raises(ValueError, match="invalid action transition"):
            store.transition(action.id, ActionStatus.APPLIED)

    def test_approved_action_records_actor_and_timestamp(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action()
        store.save(action)

        approved = store.transition(action.id, ActionStatus.APPROVED, actor="operator")

        assert approved.status is ActionStatus.APPROVED
        assert approved.approved_by == "operator"
        assert approved.approved_at is not None

    def test_failed_action_records_error(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action()
        store.save(action)
        store.transition(action.id, ActionStatus.APPROVED, actor="operator")
        store.transition(action.id, ActionStatus.RUNNING)

        failed = store.transition(action.id, ActionStatus.FAILED, error="boom")

        assert failed.status is ActionStatus.FAILED
        assert failed.error == "boom"


class TestActionPolicy:
    def test_suggest_requires_approval_for_all_side_effects(self, tmp_path: Path) -> None:
        policy = ActionPolicy(mode=ApprovalMode.SUGGEST, workspace_root=tmp_path)

        decision = policy.evaluate(_action(payload={"path": "ok.py"}))

        assert decision.allowed is True
        assert decision.requires_approval is True

    def test_auto_edit_allows_scoped_file_edits_but_not_commands(self, tmp_path: Path) -> None:
        policy = ActionPolicy(mode=ApprovalMode.AUTO_EDIT, workspace_root=tmp_path)

        file_decision = policy.evaluate(
            _action(kind=ActionKind.FILE_EDIT, payload={"path": "ok.py"})
        )
        command_decision = policy.evaluate(
            _action(kind=ActionKind.COMMAND, payload={"command": "pytest"})
        )

        assert file_decision.requires_approval is False
        assert command_decision.requires_approval is True

    def test_full_auto_still_blocks_out_of_scope_file(self, tmp_path: Path) -> None:
        policy = ActionPolicy(mode=ApprovalMode.FULL_AUTO, workspace_root=tmp_path)

        decision = policy.evaluate(_action(payload={"path": "../outside.py"}))

        assert decision.allowed is False
        assert decision.requires_approval is True

    def test_full_auto_still_blocks_denied_command(self, tmp_path: Path) -> None:
        policy = ActionPolicy(mode=ApprovalMode.FULL_AUTO, workspace_root=tmp_path)

        decision = policy.evaluate(
            _action(kind=ActionKind.COMMAND, payload={"command": "rm -rf x"})
        )

        assert decision.allowed is False
        assert decision.requires_approval is True


class TestActionStore:
    def test_create_list_by_task_and_metadata_round_trip(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action(payload={"path": "ok.py", "diff": "+x"})

        store.save(action)

        assert store.get(action.id) == action
        assert store.list_for_task("task-1") == [action]

    def test_transition_prevents_double_approval(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        action = _action()
        store.save(action)

        store.transition(action.id, ActionStatus.APPROVED, actor="operator")

        with pytest.raises(ValueError, match="invalid action transition"):
            store.transition(action.id, ActionStatus.APPROVED, actor="other")


class TestActionService:
    async def test_auto_allowed_action_runs_to_applied(self, tmp_path: Path) -> None:
        service = ActionService(
            ActionStore(tmp_path / "actions.db"),
            policy=ActionPolicy(mode=ApprovalMode.FULL_AUTO, workspace_root=tmp_path),
        )

        action = await service.propose_and_maybe_run(
            task_id="task-1",
            kind=ActionKind.FILE_WRITE,
            summary="write file",
            payload={"path": "ok.py"},
            runner=lambda: {"ok": True},
        )

        assert action.status is ActionStatus.APPLIED
        assert action.result == {"ok": True}

    def test_approval_and_rejection_are_audited(self, tmp_path: Path) -> None:
        store = ActionStore(tmp_path / "actions.db")
        service = ActionService(store)
        audit = AuditLogger(tmp_path / "audit.jsonl")
        approved, _ = service.propose(
            task_id="task-1",
            kind=ActionKind.FILE_WRITE,
            summary="write file",
            payload={"path": "ok.py"},
        )
        rejected, _ = service.propose(
            task_id="task-1",
            kind=ActionKind.COMMAND,
            summary="run command",
            payload={"command": "pytest"},
        )

        service.approve(approved.id, actor="operator", audit=audit)
        service.reject(rejected.id, actor="operator", reason="too risky", audit=audit)

        operations = [entry["details"]["operation"] for entry in audit.entries()]
        assert operations == ["action_approved", "action_rejected"]
