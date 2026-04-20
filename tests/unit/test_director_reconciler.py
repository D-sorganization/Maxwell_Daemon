"""Reconciler — set-diff between two Decompositions by entity id."""

from __future__ import annotations

import dataclasses

import pytest

from maxwell_daemon.director.reconciler import PlanDiff, diff_plans
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task


def _epic(eid: str, title: str = "E") -> Epic:
    return Epic(id=eid, title=title, description="d")


def _story(sid: str, epic_id: str = "e1", title: str = "S") -> Story:
    return Story(
        id=sid,
        epic_id=epic_id,
        title=title,
        description="d",
        acceptance_criteria=("ok",),
    )


def _task(tid: str, story_id: str = "s1", title: str = "T") -> Task:
    return Task(id=tid, story_id=story_id, title=title, description="d")


def _plan(
    epics: tuple[Epic, ...] = (),
    stories: tuple[Story, ...] = (),
    tasks: tuple[Task, ...] = (),
) -> Decomposition:
    return Decomposition(vision="v", epics=epics, stories=stories, tasks=tasks)


class TestDiffPlans:
    def test_empty_vs_empty_is_empty_diff(self) -> None:
        d = diff_plans(_plan(), _plan())
        assert d == PlanDiff(
            added_epics=(),
            removed_epics=(),
            added_stories=(),
            removed_stories=(),
            added_tasks=(),
            removed_tasks=(),
        )

    def test_adds_new_epics(self) -> None:
        old = _plan()
        new = _plan(epics=(_epic("e1"), _epic("e2")))
        d = diff_plans(old, new)
        assert {e.id for e in d.added_epics} == {"e1", "e2"}
        assert d.removed_epics == ()

    def test_removes_deleted_epics(self) -> None:
        old = _plan(epics=(_epic("e1"), _epic("e2")))
        new = _plan(epics=(_epic("e1"),))
        d = diff_plans(old, new)
        assert d.added_epics == ()
        assert d.removed_epics == ("e2",)

    def test_story_level_diff(self) -> None:
        old = _plan(
            epics=(_epic("e1"),),
            stories=(_story("s1"), _story("s2")),
        )
        new = _plan(
            epics=(_epic("e1"),),
            stories=(_story("s2"), _story("s3")),
        )
        d = diff_plans(old, new)
        assert {s.id for s in d.added_stories} == {"s3"}
        assert d.removed_stories == ("s1",)

    def test_task_level_diff(self) -> None:
        old = _plan(
            epics=(_epic("e1"),),
            stories=(_story("s1"),),
            tasks=(_task("t1"), _task("t2")),
        )
        new = _plan(
            epics=(_epic("e1"),),
            stories=(_story("s1"),),
            tasks=(_task("t2"), _task("t3")),
        )
        d = diff_plans(old, new)
        assert {t.id for t in d.added_tasks} == {"t3"}
        assert d.removed_tasks == ("t1",)

    def test_same_id_different_title_is_not_in_diff(self) -> None:
        """Changed titles are intentionally out-of-scope for now.

        The same id in both plans must NOT appear as added+removed.
        """
        old = _plan(epics=(_epic("e1", title="Old"),))
        new = _plan(epics=(_epic("e1", title="New"),))
        d = diff_plans(old, new)
        assert d.added_epics == ()
        assert d.removed_epics == ()

    def test_same_id_different_title_at_all_levels(self) -> None:
        old = _plan(
            epics=(_epic("e1", title="OldE"),),
            stories=(_story("s1", title="OldS"),),
            tasks=(_task("t1", title="OldT"),),
        )
        new = _plan(
            epics=(_epic("e1", title="NewE"),),
            stories=(_story("s1", title="NewS"),),
            tasks=(_task("t1", title="NewT"),),
        )
        d = diff_plans(old, new)
        assert d == PlanDiff(
            added_epics=(),
            removed_epics=(),
            added_stories=(),
            removed_stories=(),
            added_tasks=(),
            removed_tasks=(),
        )

    def test_plan_diff_is_frozen(self) -> None:
        d = diff_plans(_plan(), _plan())
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.added_epics = (_epic("x"),)  # type: ignore[misc]
