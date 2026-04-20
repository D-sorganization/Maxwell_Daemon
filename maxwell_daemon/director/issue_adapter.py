"""Materialise a Decomposition as GitHub issues.

The adapter is deliberately decoupled from :class:`GitHubClient`: callers pass
in a ``create_issue`` coroutine that matches :class:`GitHubClient.create_issue`'s
kwargs. That keeps the module unit-testable with no subprocess or network.

Issues are created in topological order:

* Every Epic (so stories can reference parent issue numbers).
* Every Story (so tasks can reference their parent).
* Every Task in a dependency-respecting order (so ``Depends on #N`` refers to
  issues that already exist).

Bodies include the entity's own description plus cross-references to related
issue numbers (``Part of #N``, ``Depends on #M, #O``) so GitHub renders back-
links automatically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from maxwell_daemon.director.types import Decomposition, Epic, Story, Task

__all__ = ["CreateIssueFn", "materialise_plan"]


#: Signature compatible with :meth:`GitHubClient.create_issue` but returning
#: the issue number directly. The adapter only needs the number for building
#: cross-references; callers can wrap ``GitHubClient`` trivially.
CreateIssueFn = Callable[..., Awaitable[int]]


async def materialise_plan(
    decomposition: Decomposition,
    *,
    repo: str,
    create_issue: CreateIssueFn,
    label_prefix: str = "director",
) -> dict[str, int]:
    """Create one GitHub issue per Epic, Story, and Task in ``decomposition``.

    :returns: Mapping from each entity's slug id to its created issue number.
    """
    mapping: dict[str, int] = {}

    for epic in decomposition.epics:
        mapping[epic.id] = await _create(
            create_issue,
            repo,
            title=epic.title,
            body=_epic_body(epic),
            labels=[f"{label_prefix}:epic"],
        )

    for story in decomposition.stories:
        mapping[story.id] = await _create(
            create_issue,
            repo,
            title=story.title,
            body=_story_body(story, mapping),
            labels=[f"{label_prefix}:story"],
        )

    for task in _tasks_in_topo_order(decomposition.tasks):
        mapping[task.id] = await _create(
            create_issue,
            repo,
            title=task.title,
            body=_task_body(task, mapping),
            labels=[f"{label_prefix}:task"],
        )

    return mapping


async def _create(
    create_issue: CreateIssueFn,
    repo: str,
    *,
    title: str,
    body: str,
    labels: list[str],
) -> int:
    result: Any = await create_issue(repo, title=title, body=body, labels=labels)
    return int(result)


def _epic_body(epic: Epic) -> str:
    return epic.description


def _story_body(story: Story, mapping: dict[str, int]) -> str:
    lines = [story.description]
    if story.acceptance_criteria:
        lines.append("")
        lines.append("## Acceptance criteria")
        lines.extend(f"- {item}" for item in story.acceptance_criteria)
    epic_number = mapping.get(story.epic_id)
    if epic_number is not None:
        lines.append("")
        lines.append(f"Part of #{epic_number}")
    return "\n".join(lines)


def _task_body(task: Task, mapping: dict[str, int]) -> str:
    lines = [task.description]
    story_number = mapping.get(task.story_id)
    if story_number is not None:
        lines.append("")
        lines.append(f"Part of #{story_number}")
    if task.depends_on:
        dep_numbers = [mapping[dep] for dep in task.depends_on if dep in mapping]
        if dep_numbers:
            rendered = ", ".join(f"#{n}" for n in dep_numbers)
            lines.append(f"Depends on {rendered}")
    return "\n".join(lines)


def _tasks_in_topo_order(tasks: tuple[Task, ...]) -> list[Task]:
    """Return tasks ordered so every task appears after its dependencies.

    Uses Kahn's algorithm (iterative BFS over in-degrees) for stability and
    because validation already guarantees no cycles by the time this runs.
    Tasks without declared dependencies keep their original relative order,
    which matches how a decomposer usually emits them.
    """
    by_id: dict[str, Task] = {t.id: t for t in tasks}
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    dependents: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            if dep in by_id:
                in_degree[t.id] += 1
                dependents[dep].append(t.id)

    # Preserve input order among ready tasks.
    ready: list[str] = [t.id for t in tasks if in_degree[t.id] == 0]
    ordered: list[Task] = []
    while ready:
        tid = ready.pop(0)
        ordered.append(by_id[tid])
        for child in dependents[tid]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)

    # If a cycle sneaks through (shouldn't, because validate() ran), fall
    # back to appending unvisited tasks in input order so we never silently
    # drop work.
    if len(ordered) != len(tasks):
        seen = {t.id for t in ordered}
        ordered.extend(t for t in tasks if t.id not in seen)
    return ordered
