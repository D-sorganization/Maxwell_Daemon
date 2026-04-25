from pathlib import Path

from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.actions import Action, ActionKind, ActionStatus


class ActionReverter:
    """Executes the inverse of previously applied actions to restore state."""

    def __init__(self, workspace_root: Path, action_service: ActionService):
        self._workspace_root = workspace_root.resolve()
        self._action_service = action_service

    def revert(self, action: Action) -> None:
        """
        Revert an action using its stored inverse_payload.
        Transitions the action to REVERTED on success.
        """
        if action.status is not ActionStatus.APPLIED:
            raise ValueError(f"Cannot revert action in status {action.status.value}")

        if action.kind in (ActionKind.FILE_WRITE, ActionKind.FILE_EDIT):
            self._revert_file_operation(action)
        else:
            raise NotImplementedError(f"Reverting {action.kind.value} is not supported")

        self._action_service.mark_reverted(action.id)

    def _revert_file_operation(self, action: Action) -> None:
        if not action.inverse_payload:
            raise ValueError(
                f"Action {action.id} lacks an inverse_payload for reversion"
            )

        # Re-resolve the original path
        path_str = action.payload.get("path")
        if not path_str:
            raise ValueError(f"Action {action.id} payload is missing 'path'")

        candidate = (self._workspace_root / path_str).resolve()
        try:
            candidate.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ValueError(
                f"Path {path_str!r} resolves outside workspace root"
            ) from exc

        existed = action.inverse_payload.get("existed", False)
        old_content = action.inverse_payload.get("old_content")

        if existed:
            if old_content is None:
                raise ValueError(f"Action {action.id} existed but lacks old_content")
            # Restore the old content
            candidate.write_text(old_content, encoding="utf-8")
        else:
            # Did not exist previously, so we remove it
            if candidate.exists():
                candidate.unlink()
