"""Ephemeral per-task working memory.

The executor's retry loops already pass prior plans/diffs/errors back to the
LLM; ScratchPad formalises that so every refinement step sees the whole
accumulated context without ad-hoc parameter threading.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from maxwell_daemon.contracts import require

__all__ = ["ScratchEntry", "ScratchPad"]


@dataclass(slots=True, frozen=True)
class ScratchEntry:
    role: str
    content: str
    ts: datetime


class ScratchPad:
    """Bounded-queue per-task scratchpad; dropped on task completion."""

    def __init__(self, *, max_entries_per_task: int = 32) -> None:
        self._cap = max_entries_per_task
        self._entries: dict[str, deque[ScratchEntry]] = {}

    def append(self, task_id: str, *, role: str, content: str) -> None:
        require(bool(task_id), "ScratchPad.append: task_id must be non-empty")
        require(bool(role), "ScratchPad.append: role must be non-empty")
        entries = self._entries.setdefault(task_id, deque(maxlen=self._cap))
        entries.append(ScratchEntry(role=role, content=content, ts=datetime.now(timezone.utc)))

    def entries(self, task_id: str) -> list[ScratchEntry]:
        return list(self._entries.get(task_id, ()))

    def clear(self, task_id: str) -> None:
        self._entries.pop(task_id, None)

    def render(self, task_id: str, *, max_chars: int = 8000) -> str:
        items = self.entries(task_id)
        if not items:
            return ""
        # Newest first so the LLM sees the most-relevant state within the budget.
        per_item = max(80, max_chars // max(1, len(items)))
        parts: list[str] = []
        for entry in reversed(items):
            body = entry.content
            if len(body) > per_item:
                body = body[:per_item] + "\n... (truncated)"
            parts.append(f"[{entry.role}] {body}")
        rendered = "\n\n".join(parts)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n... (truncated)"
        return rendered
