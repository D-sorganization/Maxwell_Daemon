"""Pure routing policy: tasks -> machines.

The dispatcher is deliberately side-effect-free. Given an immutable snapshot
of the fleet (:class:`MachineState`) plus a list of :class:`TaskRequirement`,
it returns a :class:`DispatchPlan` describing which tasks go where and which
couldn't be placed. No asyncio, no I/O, no global state.

Scoring is intentionally simple: ``available_slots * 10 + #preferred_tags_matched``.
The factor of 10 keeps "more capacity" dominant over "nice-to-have tags" while
still letting preferred tags break ties between machines with equal slots.
Greedy placement picks the highest-scoring (machine, task) pair at each step
— after picking, the machine's remaining capacity drops by one and scores are
recomputed so the next pick reflects current load.

See GitHub issue #104 for context.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from maxwell_daemon.contracts import require

__all__ = [
    "Assignment",
    "DispatchPlan",
    "FleetDispatcher",
    "MachineState",
    "TaskRequirement",
    "score_machine",
]


# Weight applied to each free slot when scoring. Large enough that capacity
# dominates preferred-tag matches, so two equally-preferred machines are
# separated by load rather than tag count.
_SLOT_WEIGHT = 10


@dataclass(slots=True, frozen=True)
class MachineState:
    """Immutable snapshot of a machine's current load and capabilities."""

    name: str
    host: str
    port: int
    capacity: int
    tags: tuple[str, ...]
    active_tasks: int = 0
    healthy: bool = True

    @property
    def available_slots(self) -> int:
        """Free slots right now. Unhealthy machines report zero regardless of load."""
        if not self.healthy:
            return 0
        return max(0, self.capacity - self.active_tasks)


@dataclass(slots=True, frozen=True)
class TaskRequirement:
    """What a task needs (required tags) and prefers (preferred tags).

    Required tags act as a hard filter — a machine that lacks any is excluded.
    Preferred tags only influence scoring.
    """

    task_id: str
    required_tags: frozenset[str] = frozenset()
    preferred_tags: frozenset[str] = frozenset()


@dataclass(slots=True, frozen=True)
class Assignment:
    """One task -> one machine binding."""

    task_id: str
    machine_name: str


@dataclass(slots=True, frozen=True)
class DispatchPlan:
    """Result of :meth:`FleetDispatcher.plan`.

    ``assignments`` is ordered by placement: the first entry was the highest
    scoring (machine, task) pair at the moment it was picked.
    """

    assignments: tuple[Assignment, ...]
    unassigned: tuple[str, ...]


def score_machine(machine: MachineState, task: TaskRequirement) -> int | None:
    """Score a (machine, task) pair.

    Returns:
        ``None`` when the machine cannot run the task (unhealthy, full, or
        missing any required tag). Otherwise an integer where higher is better.
    """
    if not machine.healthy:
        return None
    if machine.available_slots <= 0:
        return None
    machine_tags = set(machine.tags)
    if not task.required_tags.issubset(machine_tags):
        return None
    preferred_hits = len(task.preferred_tags & machine_tags)
    return machine.available_slots * _SLOT_WEIGHT + preferred_hits


class FleetDispatcher:
    """Pure logic — produce a :class:`DispatchPlan` from a fleet snapshot.

    Algorithm: repeatedly find the highest-scoring (machine, task) pair across
    all currently-schedulable pairs. Assign it, decrement that machine's
    available capacity, remove the task from the pool, repeat until nothing is
    schedulable. Any task we never placed goes into ``unassigned``.

    Tie-breaking (when two candidate pairs have identical scores): we pick the
    lowest task id first, then the lowest machine name. Stable and deterministic
    — important for reproducible planning in tests.
    """

    def plan(
        self,
        machines: tuple[MachineState, ...],
        tasks: tuple[TaskRequirement, ...],
    ) -> DispatchPlan:
        # Machines mutate (their active_tasks grows) as we assign. Work on a
        # mutable dict keyed by name so we can swap in updated snapshots.
        live: dict[str, MachineState] = {m.name: m for m in machines}
        remaining: list[TaskRequirement] = list(tasks)
        assignments: list[Assignment] = []

        while remaining:
            best: tuple[int, str, str, TaskRequirement, MachineState] | None = None
            # Iterate in a deterministic order so ties resolve the same way
            # every run. We sort by (-score, task_id, machine_name) at the end.
            for task in remaining:
                for machine in live.values():
                    score = score_machine(machine, task)
                    if score is None:
                        continue
                    candidate = (score, task.task_id, machine.name, task, machine)
                    if best is None:
                        best = candidate
                        continue
                    # Higher score wins; on equal score prefer lower task id,
                    # then lower machine name (lexicographic, stable).
                    if (score > best[0]) or (
                        score == best[0] and (task.task_id, machine.name) < (best[1], best[2])
                    ):
                        best = candidate

            if best is None:
                break  # nothing schedulable — the rest go unassigned

            _score, task_id, machine_name, task, machine = best
            require(
                machine.available_slots > 0,
                "scored machine must have at least one available slot",
            )
            assignments.append(Assignment(task_id=task_id, machine_name=machine_name))
            # "Consume" a slot by incrementing active_tasks on the live copy.
            live[machine_name] = replace(machine, active_tasks=machine.active_tasks + 1)
            remaining.remove(task)

        return DispatchPlan(
            assignments=tuple(assignments),
            unassigned=tuple(t.task_id for t in remaining),
        )
