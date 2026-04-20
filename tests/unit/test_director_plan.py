"""Director — orchestrates an injected decomposer and validates its output."""

from __future__ import annotations

import dataclasses

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.director.decomposer import Director
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task


def _valid_plan(vision: str = "ship auth") -> Decomposition:
    return Decomposition(
        vision=vision,
        epics=(Epic(id="e1", title="E", description="d"),),
        stories=(
            Story(
                id="s1",
                epic_id="e1",
                title="S",
                description="d",
                acceptance_criteria=("ok",),
            ),
        ),
        tasks=(Task(id="t1", story_id="s1", title="T", description="d"),),
    )


class TestDirectorPlan:
    async def test_empty_vision_raises_precondition(self) -> None:
        async def fake(vision: str) -> Decomposition:
            return _valid_plan()

        d = Director(decomposer=fake)
        with pytest.raises(PreconditionError):
            await d.plan("")

    async def test_whitespace_only_vision_raises(self) -> None:
        async def fake(vision: str) -> Decomposition:
            return _valid_plan()

        d = Director(decomposer=fake)
        with pytest.raises(PreconditionError):
            await d.plan("   \n\t  ")

    async def test_forwards_vision_to_decomposer(self) -> None:
        seen: list[str] = []

        async def fake(vision: str) -> Decomposition:
            seen.append(vision)
            return _valid_plan(vision=vision)

        d = Director(decomposer=fake)
        await d.plan("harden CI")
        assert seen == ["harden CI"]

    async def test_returns_decomposer_result(self) -> None:
        plan = _valid_plan()

        async def fake(vision: str) -> Decomposition:
            return plan

        d = Director(decomposer=fake)
        got = await d.plan("a vision")
        assert got is plan

    async def test_propagates_validation_errors(self) -> None:
        bad = Decomposition(
            vision="v",
            epics=(Epic(id="e1", title="", description=""),),
            stories=(
                Story(
                    id="s1",
                    epic_id="ghost",  # orphan: no such epic
                    title="",
                    description="",
                    acceptance_criteria=(),
                ),
            ),
            tasks=(),
        )

        async def fake(vision: str) -> Decomposition:
            return bad

        d = Director(decomposer=fake)
        with pytest.raises(PreconditionError):
            await d.plan("anything")

    async def test_result_is_frozen(self) -> None:
        async def fake(vision: str) -> Decomposition:
            return _valid_plan()

        d = Director(decomposer=fake)
        result = await d.plan("v")
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.vision = "changed"  # type: ignore[misc]
