"""Director data types — Epic, Story, Task, and the Decomposition that binds them.

Every type is a frozen dataclass so a plan produced by the Director can be
safely shared between the reconciler, the issue adapter, and any downstream
cache without defensive copying. ``Decomposition.validate()`` enforces the
structural invariants that the rest of the pipeline assumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from maxwell_daemon.contracts import require

__all__ = ["Decomposition", "Epic", "Story", "Task"]


@dataclass(slots=True, frozen=True)
class Epic:
    """A top-level objective. Multiple stories serve an epic."""

    id: str
    title: str
    description: str


@dataclass(slots=True, frozen=True)
class Story:
    """A user-visible outcome that lives under an Epic."""

    id: str
    epic_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class Task:
    """A concrete unit of implementation work under a Story."""

    id: str
    story_id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = field(default=())


@dataclass(slots=True, frozen=True)
class Decomposition:
    """The output of a single Director run."""

    vision: str
    epics: tuple[Epic, ...]
    stories: tuple[Story, ...]
    tasks: tuple[Task, ...]

    def stories_for_epic(self, epic_id: str) -> tuple[Story, ...]:
        """Return every story whose ``epic_id`` matches, preserving order."""
        return tuple(s for s in self.stories if s.epic_id == epic_id)

    def tasks_for_story(self, story_id: str) -> tuple[Task, ...]:
        """Return every task whose ``story_id`` matches, preserving order."""
        return tuple(t for t in self.tasks if t.story_id == story_id)

    def validate(self) -> None:
        """Verify structural invariants; raise :class:`PreconditionError` on failure.

        Checks, in order:

        1. Unique ids within each level (Epic, Story, Task).
        2. Every story's ``epic_id`` refers to a known epic.
        3. Every task's ``story_id`` refers to a known story.
        4. Every task ``depends_on`` entry references a known task.
        5. The task dependency graph is acyclic (iterative DFS with a
           three-colour marking, so we detect self-loops and longer cycles
           without recursion limits).
        """
        _check_unique_ids("epic", tuple(e.id for e in self.epics))
        _check_unique_ids("story", tuple(s.id for s in self.stories))
        _check_unique_ids("task", tuple(t.id for t in self.tasks))
        all_ids = (
            tuple(e.id for e in self.epics)
            + tuple(s.id for s in self.stories)
            + tuple(t.id for t in self.tasks)
        )
        _check_unique_ids("global", all_ids)

        epic_ids = {e.id for e in self.epics}
        for s in self.stories:
            require(
                s.epic_id in epic_ids,
                f"orphan story {s.id!r}: unknown epic_id {s.epic_id!r}",
            )

        story_ids = {s.id for s in self.stories}
        for t in self.tasks:
            require(
                t.story_id in story_ids,
                f"orphan task {t.id!r}: unknown story_id {t.story_id!r}",
            )

        task_ids = {t.id for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                require(
                    dep in task_ids,
                    f"task {t.id!r} depends on unknown task id {dep!r}",
                )

        _check_no_cycles(self.tasks)


def _check_unique_ids(level: str, ids: tuple[str, ...]) -> None:
    seen: set[str] = set()
    for id_ in ids:
        require(id_ not in seen, f"duplicate {level} id {id_!r}")
        seen.add(id_)


# Colouring constants for iterative DFS cycle detection.
_WHITE = 0  # unvisited
_GREY = 1  # on the current DFS stack
_BLACK = 2  # fully explored


def _check_no_cycles(tasks: tuple[Task, ...]) -> None:
    """Detect cycles in the task ``depends_on`` graph via iterative DFS.

    Standard three-colour algorithm:

    * ``WHITE`` — not yet visited.
    * ``GREY`` — in the current traversal stack; encountering a grey node
      means we've found a back-edge → cycle.
    * ``BLACK`` — fully explored, safe to skip.

    Iterative rather than recursive so pathological plans can't blow the
    Python stack. A self-loop (task depending on itself) is caught on the
    first edge visit because the node is already ``GREY``.
    """
    graph: dict[str, tuple[str, ...]] = {t.id: t.depends_on for t in tasks}
    colour: dict[str, int] = dict.fromkeys(graph, _WHITE)

    for start in graph:
        if colour[start] != _WHITE:
            continue
        # Each frame: (node, iterator over its outgoing edges).
        stack: list[tuple[str, list[str]]] = [(start, list(graph[start]))]
        colour[start] = _GREY
        while stack:
            node, pending = stack[-1]
            if not pending:
                colour[node] = _BLACK
                stack.pop()
                continue
            nxt = pending.pop()
            # Missing-target edges are caught earlier in validate(); treat
            # a miss here defensively as "no cycle through unknown node".
            if nxt not in colour:
                continue
            state = colour[nxt]
            if state == _GREY:
                require(False, f"task dependency cycle detected at {nxt!r}")
            if state == _WHITE:
                colour[nxt] = _GREY
                stack.append((nxt, list(graph[nxt])))
