"""Unit tests for maxwell_daemon.fleet.dispatcher — pure task-to-machine routing.

The dispatcher is deliberately side-effect-free: given a snapshot of machines
and a list of task requirements, it returns a DispatchPlan. No I/O, no asyncio,
no network. That makes scoring decisions trivial to verify and lets the
higher-level workflow mock any policy change we throw at it.
"""

from __future__ import annotations

import dataclasses

import pytest

from maxwell_daemon.fleet.dispatcher import (
    Assignment,
    DispatchPlan,
    FleetDispatcher,
    MachineState,
    TaskRequirement,
    score_machine,
)


def _machine(
    name: str = "m1",
    *,
    host: str = "host.example",
    port: int = 50051,
    capacity: int = 2,
    tags: tuple[str, ...] = (),
    active_tasks: int = 0,
    healthy: bool = True,
) -> MachineState:
    return MachineState(
        name=name,
        host=host,
        port=port,
        capacity=capacity,
        tags=tags,
        active_tasks=active_tasks,
        healthy=healthy,
    )


def _task(
    task_id: str = "t1",
    *,
    required: tuple[str, ...] = (),
    preferred: tuple[str, ...] = (),
) -> TaskRequirement:
    return TaskRequirement(
        task_id=task_id,
        required_tags=frozenset(required),
        preferred_tags=frozenset(preferred),
    )


class TestMachineStateAvailableSlots:
    def test_available_slots_zero_when_unhealthy(self) -> None:
        m = _machine(capacity=4, active_tasks=0, healthy=False)
        assert m.available_slots == 0

    def test_available_slots_zero_when_full(self) -> None:
        m = _machine(capacity=2, active_tasks=2)
        assert m.available_slots == 0

    def test_available_slots_never_negative(self) -> None:
        """If active_tasks somehow exceeds capacity, clamp to 0 rather than lie."""
        m = _machine(capacity=2, active_tasks=5)
        assert m.available_slots == 0

    def test_available_slots_correct_when_partial(self) -> None:
        m = _machine(capacity=4, active_tasks=1)
        assert m.available_slots == 3

    def test_available_slots_equals_capacity_when_idle(self) -> None:
        m = _machine(capacity=3, active_tasks=0)
        assert m.available_slots == 3


class TestScoreMachine:
    def test_none_when_unhealthy(self) -> None:
        m = _machine(healthy=False)
        assert score_machine(m, _task()) is None

    def test_none_when_full(self) -> None:
        m = _machine(capacity=2, active_tasks=2)
        assert score_machine(m, _task()) is None

    def test_none_when_required_tag_missing(self) -> None:
        m = _machine(tags=("linux",))
        assert score_machine(m, _task(required=("gpu",))) is None

    def test_none_when_some_required_tag_missing(self) -> None:
        m = _machine(tags=("linux",))
        assert score_machine(m, _task(required=("linux", "gpu"))) is None

    def test_int_returned_when_all_required_tags_present(self) -> None:
        m = _machine(capacity=2, tags=("linux", "gpu"))
        result = score_machine(m, _task(required=("linux",)))
        assert isinstance(result, int)

    def test_higher_score_for_more_available_slots(self) -> None:
        low = _machine("a", capacity=1)
        high = _machine("b", capacity=8)
        task = _task()
        low_score = score_machine(low, task)
        high_score = score_machine(high, task)
        assert low_score is not None and high_score is not None
        assert high_score > low_score

    def test_higher_score_for_more_preferred_tags(self) -> None:
        plain = _machine("plain", capacity=2, tags=())
        tagged = _machine("tagged", capacity=2, tags=("fast", "ssd"))
        task = _task(preferred=("fast", "ssd"))
        plain_score = score_machine(plain, task)
        tagged_score = score_machine(tagged, task)
        assert plain_score is not None and tagged_score is not None
        assert tagged_score > plain_score

    def test_preferred_tag_missing_does_not_exclude(self) -> None:
        m = _machine(tags=())
        assert score_machine(m, _task(preferred=("fast",))) is not None


class TestDispatcherBasics:
    def test_empty_machines_all_unassigned(self) -> None:
        plan = FleetDispatcher().plan((), (_task("t1"), _task("t2")))
        assert plan.assignments == ()
        assert set(plan.unassigned) == {"t1", "t2"}

    def test_empty_tasks_empty_plan(self) -> None:
        plan = FleetDispatcher().plan((_machine(),), ())
        assert plan.assignments == ()
        assert plan.unassigned == ()

    def test_single_task_single_assignment(self) -> None:
        m = _machine("m1", capacity=2)
        plan = FleetDispatcher().plan((m,), (_task("t1"),))
        assert plan.assignments == (Assignment(task_id="t1", machine_name="m1"),)
        assert plan.unassigned == ()

    def test_single_task_best_machine_wins(self) -> None:
        small = _machine("small", capacity=1)
        big = _machine("big", capacity=8)
        plan = FleetDispatcher().plan((small, big), (_task("t1"),))
        assert plan.assignments == (Assignment(task_id="t1", machine_name="big"),)


class TestDispatcherCapacity:
    def test_tasks_exceeding_capacity_overflow(self) -> None:
        m = _machine("m1", capacity=2)
        tasks = tuple(_task(f"t{i}") for i in range(5))
        plan = FleetDispatcher().plan((m,), tasks)
        assert len(plan.assignments) == 2
        assert len(plan.unassigned) == 3
        assigned_ids = {a.task_id for a in plan.assignments}
        unassigned_ids = set(plan.unassigned)
        assert assigned_ids.isdisjoint(unassigned_ids)
        assert assigned_ids | unassigned_ids == {f"t{i}" for i in range(5)}

    def test_capacity_respected_across_machines(self) -> None:
        a = _machine("a", capacity=2)
        b = _machine("b", capacity=3)
        tasks = tuple(_task(f"t{i}") for i in range(10))
        plan = FleetDispatcher().plan((a, b), tasks)

        counts: dict[str, int] = {}
        for assignment in plan.assignments:
            counts[assignment.machine_name] = counts.get(assignment.machine_name, 0) + 1
        assert counts.get("a", 0) <= 2
        assert counts.get("b", 0) <= 3
        assert len(plan.assignments) == 5
        assert len(plan.unassigned) == 5

    def test_active_tasks_reduce_available_capacity(self) -> None:
        m = _machine("m1", capacity=4, active_tasks=3)
        tasks = (_task("t1"), _task("t2"), _task("t3"))
        plan = FleetDispatcher().plan((m,), tasks)
        assert len(plan.assignments) == 1
        assert len(plan.unassigned) == 2


class TestDispatcherFiltering:
    def test_required_tags_filter_excludes_machines(self) -> None:
        cpu_only = _machine("cpu", capacity=4, tags=("cpu",))
        gpu = _machine("gpu", capacity=1, tags=("cpu", "gpu"))
        plan = FleetDispatcher().plan(
            (cpu_only, gpu),
            (_task("t1", required=("gpu",)),),
        )
        assert plan.assignments == (Assignment(task_id="t1", machine_name="gpu"),)

    def test_no_machine_satisfies_required_tags(self) -> None:
        m = _machine("m1", capacity=4, tags=("linux",))
        plan = FleetDispatcher().plan((m,), (_task("t1", required=("darwin",)),))
        assert plan.assignments == ()
        assert plan.unassigned == ("t1",)

    def test_unhealthy_machine_skipped(self) -> None:
        dead = _machine("dead", capacity=8, healthy=False)
        alive = _machine("alive", capacity=1)
        plan = FleetDispatcher().plan((dead, alive), (_task("t1"),))
        assert plan.assignments == (Assignment(task_id="t1", machine_name="alive"),)


class TestDispatcherTieBreaking:
    def test_preferred_tags_break_ties(self) -> None:
        plain = _machine("plain", capacity=2, tags=())
        fast = _machine("fast", capacity=2, tags=("fast",))
        plan = FleetDispatcher().plan(
            (plain, fast),
            (_task("t1", preferred=("fast",)),),
        )
        assert plan.assignments == (Assignment(task_id="t1", machine_name="fast"),)

    def test_greedy_placement_highest_score_first(self) -> None:
        """When two tasks compete, the higher-scoring pair is picked first.

        Task t_gpu prefers ``gpu``; task t_any has no preference. The gpu machine
        scores higher for t_gpu (slots + preferred match) than the cpu machine
        does for either task. Greedy should place (t_gpu, gpu) first, then
        t_any lands on cpu.
        """
        cpu = _machine("cpu", capacity=1, tags=("cpu",))
        gpu = _machine("gpu", capacity=1, tags=("cpu", "gpu"))
        t_gpu = _task("t_gpu", preferred=("gpu",))
        t_any = _task("t_any")
        plan = FleetDispatcher().plan((cpu, gpu), (t_gpu, t_any))
        by_task = {a.task_id: a.machine_name for a in plan.assignments}
        assert by_task == {"t_gpu": "gpu", "t_any": "cpu"}


class TestFrozenDataclasses:
    def test_machine_state_frozen(self) -> None:
        m = _machine()
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.active_tasks = 99  # type: ignore[misc]

    def test_task_requirement_frozen(self) -> None:
        t = _task()
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.task_id = "other"  # type: ignore[misc]

    def test_assignment_frozen(self) -> None:
        a = Assignment(task_id="t1", machine_name="m1")
        with pytest.raises(dataclasses.FrozenInstanceError):
            a.task_id = "t2"  # type: ignore[misc]

    def test_dispatch_plan_frozen(self) -> None:
        plan = DispatchPlan(assignments=(), unassigned=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.unassigned = ("x",)  # type: ignore[misc]
