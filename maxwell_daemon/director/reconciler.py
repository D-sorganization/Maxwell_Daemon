"""Reconcile a new Decomposition against an existing one.

Pure set-diff by entity id per level. Edits to an entity's title or description
(same id, different fields) intentionally don't show up in the diff — keeping
"updated" separate is left for a future PR.
"""

from __future__ import annotations

from dataclasses import dataclass

from maxwell_daemon.director.types import Decomposition, Epic, Story, Task

__all__ = ["PlanDiff", "diff_plans"]


@dataclass(slots=True, frozen=True)
class PlanDiff:
    """Structured diff between two plans, split by entity level."""

    added_epics: tuple[Epic, ...]
    removed_epics: tuple[str, ...]
    added_stories: tuple[Story, ...]
    removed_stories: tuple[str, ...]
    added_tasks: tuple[Task, ...]
    removed_tasks: tuple[str, ...]


def diff_plans(old: Decomposition, new: Decomposition) -> PlanDiff:
    """Return the per-level set-diff between ``old`` and ``new``.

    An entity is "added" if its id exists in ``new`` but not in ``old``, and
    "removed" if its id exists in ``old`` but not in ``new``. Shared ids with
    mutated fields are treated as "unchanged" here and are not reported.
    """
    old_epic_ids = {e.id for e in old.epics}
    new_epic_ids = {e.id for e in new.epics}
    added_epics = tuple(e for e in new.epics if e.id not in old_epic_ids)
    removed_epics = tuple(e.id for e in old.epics if e.id not in new_epic_ids)

    old_story_ids = {s.id for s in old.stories}
    new_story_ids = {s.id for s in new.stories}
    added_stories = tuple(s for s in new.stories if s.id not in old_story_ids)
    removed_stories = tuple(s.id for s in old.stories if s.id not in new_story_ids)

    old_task_ids = {t.id for t in old.tasks}
    new_task_ids = {t.id for t in new.tasks}
    added_tasks = tuple(t for t in new.tasks if t.id not in old_task_ids)
    removed_tasks = tuple(t.id for t in old.tasks if t.id not in new_task_ids)

    return PlanDiff(
        added_epics=added_epics,
        removed_epics=removed_epics,
        added_stories=added_stories,
        removed_stories=removed_stories,
        added_tasks=added_tasks,
        removed_tasks=removed_tasks,
    )
