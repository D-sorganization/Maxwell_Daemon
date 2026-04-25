"""Approval policy for agent side effects."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from maxwell_daemon.core.actions import Action, ActionKind


class ApprovalMode(str, Enum):
    SUGGEST = "suggest"
    AUTO_EDIT = "auto-edit"
    FULL_AUTO = "full-auto"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Small default-deny policy layer for side-effecting actions."""

    mode: ApprovalMode = ApprovalMode.SUGGEST
    workspace_root: Path | None = None
    denied_command_names: frozenset[str] = field(
        default_factory=lambda: frozenset({"rm", "shutdown", "reboot", "mkfs"})
    )

    def evaluate(self, action: Action, *, dry_run: bool = False) -> PolicyDecision:
        if not self._known_kind(action.kind):
            return PolicyDecision(False, True, f"unknown action kind: {action.kind.value}")
        if not self._within_allowed_scope(action):
            return PolicyDecision(False, True, "action target is outside the allowed workspace")
        if action.kind is ActionKind.COMMAND and self._uses_denied_command(action):
            return PolicyDecision(False, True, "command is denied by policy")
        if dry_run:
            return PolicyDecision(True, True, "dry-run forces approval required")
        if self.mode is ApprovalMode.SUGGEST:
            return PolicyDecision(True, True, "suggest mode requires approval")
        if self.mode is ApprovalMode.AUTO_EDIT:
            if action.kind in {ActionKind.FILE_WRITE, ActionKind.FILE_EDIT, ActionKind.DIFF_APPLY}:
                return PolicyDecision(True, False, "auto-edit permits scoped file changes")
            return PolicyDecision(True, True, "auto-edit requires approval for this action kind")
        if self.mode is ApprovalMode.FULL_AUTO:
            return PolicyDecision(True, False, "full-auto permits this policy-approved action")
        return PolicyDecision(False, True, f"unsupported approval mode: {self.mode.value}")

    @staticmethod
    def _known_kind(kind: ActionKind) -> bool:
        return kind in set(ActionKind)

    def _within_allowed_scope(self, action: Action) -> bool:
        if self.workspace_root is None:
            return True
        if action.kind not in {ActionKind.FILE_WRITE, ActionKind.FILE_EDIT, ActionKind.DIFF_APPLY}:
            return True
        raw_path = action.payload.get("path") or action.payload.get("target_path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        root = self.workspace_root.resolve()
        target = (root / raw_path).resolve()
        return target == root or root in target.parents

    def _uses_denied_command(self, action: Action) -> bool:
        raw_command = action.payload.get("command")
        if not isinstance(raw_command, str) or not raw_command.strip():
            return False
        try:
            parts = shlex.split(raw_command, posix=True)
        except ValueError:
            return True
        if not parts:
            return False
        command_name = Path(parts[0]).name
        return command_name in self.denied_command_names
