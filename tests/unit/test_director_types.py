"""Director data types — Epic / Story / Task / Decomposition invariants.

Validates:
* Dataclasses are frozen (immutable) so a plan can be safely shared.
* Derived accessors filter correctly by parent id.
* ``Decomposition.validate()`` enforces structural consistency: unique ids per
  level, no orphan stories/tasks, task dependencies reference real tasks, and
  the task dependency graph is acyclic.
"""

from __future__ import annotations

import dataclasses

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task


def _epic(eid: str = "e1", title: str = "E1") -> Epic:
    return Epic(id=eid, title=title, description="desc")


def _story(sid: str = "s1", epic_id: str = "e1") -> Story:
    return Story(
        id=sid,
        epic_id=epic_id,
        title=f"Story {sid}",
        description="desc",
        acceptance_criteria=("done",),
    )


def _task(
    tid: str = "t1",
    story_id: str = "s1",
    depends_on: tuple[str, ...] = (),
) -> Task:
    return Task(
        id=tid,
        story_id=story_id,
        title=f"Task {tid}",
        description="desc",
        depends_on=depends_on,
    )


class TestFrozen:
    def test_epic_is_frozen(self) -> None:
        e = _epic()
        with pytest.raises(dataclasses.FrozenInstanceError):
            e.title = "changed"  # type: ignore[misc]

    def test_story_is_frozen(self) -> None:
        s = _story()
        with pytest.raises(dataclasses.FrozenInstanceError):
            s.title = "changed"  # type: ignore[misc]

    def test_task_is_frozen(self) -> None:
        t = _task()
        with pytest.raises(dataclasses.FrozenInstanceError):
            t.title = "changed"  # type: ignore[misc]

    def test_decomposition_is_frozen(self) -> None:
        d = Decomposition(vision="v", epics=(), stories=(), tasks=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.vision = "changed"  # type: ignore[misc]


class TestDerivedAccessors:
    def test_stories_for_epic_filters_by_epic_id(self) -> None:
        e1, e2 = _epic("e1"), _epic("e2")
        s1 = _story("s1", "e1")
        s2 = _story("s2", "e2")
        s3 = _story("s3", "e1")
        d = Decomposition(vision="v", epics=(e1, e2), stories=(s1, s2, s3), tasks=())
        assert d.stories_for_epic("e1") == (s1, s3)
        assert d.stories_for_epic("e2") == (s2,)
        assert d.stories_for_epic("missing") == ()

    def test_tasks_for_story_filters_by_story_id(self) -> None:
        e = _epic()
        s1, s2 = _story("s1"), _story("s2")
        t1 = _task("t1", "s1")
        t2 = _task("t2", "s2")
        t3 = _task("t3", "s1")
        d = Decomposition(vision="v", epics=(e,), stories=(s1, s2), tasks=(t1, t2, t3))
        assert d.tasks_for_story("s1") == (t1, t3)
        assert d.tasks_for_story("s2") == (t2,)
        assert d.tasks_for_story("missing") == ()


class TestValidate:
    def test_happy_path_no_errors(self) -> None:
        e = _epic()
        s = _story()
        t1 = _task("t1", "s1")
        t2 = _task("t2", "s1", depends_on=("t1",))
        d = Decomposition(vision="v", epics=(e,), stories=(s,), tasks=(t1, t2))
        d.validate()  # no raise

    def test_duplicate_epic_id_raises(self) -> None:
        d = Decomposition(
            vision="v",
            epics=(_epic("e1"), _epic("e1")),
            stories=(),
            tasks=(),
        )
        with pytest.raises(PreconditionError, match="duplicate"):
            d.validate()

    def test_duplicate_story_id_raises(self) -> None:
        e = _epic()
        d = Decomposition(
            vision="v",
            epics=(e,),
            stories=(_story("s1"), _story("s1")),
            tasks=(),
        )
        with pytest.raises(PreconditionError, match="duplicate"):
            d.validate()

    def test_duplicate_task_id_raises(self) -> None:
        e = _epic()
        s = _story()
        d = Decomposition(
            vision="v",
            epics=(e,),
            stories=(s,),
            tasks=(_task("t1"), _task("t1")),
        )
        with pytest.raises(PreconditionError, match="duplicate"):
            d.validate()

    def test_orphan_story_raises(self) -> None:
        d = Decomposition(
            vision="v",
            epics=(_epic("e1"),),
            stories=(_story("s1", epic_id="nope"),),
            tasks=(),
        )
        with pytest.raises(PreconditionError, match="orphan"):
            d.validate()

    def test_orphan_task_raises(self) -> None:
        d = Decomposition(
            vision="v",
            epics=(_epic(),),
            stories=(_story(),),
            tasks=(_task("t1", story_id="nope"),),
        )
        with pytest.raises(PreconditionError, match="orphan"):
            d.validate()

    def test_task_depends_on_unknown_id_raises(self) -> None:
        e = _epic()
        s = _story()
        d = Decomposition(
            vision="v",
            epics=(e,),
            stories=(s,),
            tasks=(_task("t1", depends_on=("ghost",)),),
        )
        with pytest.raises(PreconditionError, match="unknown"):
            d.validate()

    def test_task_dependency_cycle_raises(self) -> None:
        e = _epic()
        s = _story()
        t1 = _task("t1", depends_on=("t2",))
        t2 = _task("t2", depends_on=("t1",))
        d = Decomposition(vision="v", epics=(e,), stories=(s,), tasks=(t1, t2))
        with pytest.raises(PreconditionError, match="cycle"):
            d.validate()

    def test_task_self_cycle_raises(self) -> None:
        e = _epic()
        s = _story()
        t = _task("t1", depends_on=("t1",))
        d = Decomposition(vision="v", epics=(e,), stories=(s,), tasks=(t,))
        with pytest.raises(PreconditionError, match="cycle"):
            d.validate()
