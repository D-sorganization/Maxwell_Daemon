"""issue_adapter — materialise a Decomposition as GitHub issues.

The adapter delegates creation to an injected ``create_issue`` coroutine so
these tests never touch the network.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from maxwell_daemon.director.issue_adapter import materialise_plan
from maxwell_daemon.director.types import Decomposition, Epic, Story, Task


class _FakeGh:
    """Records every create_issue call in order and hands out issue numbers."""

    def __init__(self, start: int = 100) -> None:
        self._next = start
        self.calls: list[dict[str, object]] = []

    async def create(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: Sequence[str],
    ) -> int:
        number = self._next
        self._next += 1
        self.calls.append(
            {
                "repo": repo,
                "title": title,
                "body": body,
                "labels": list(labels),
                "number": number,
            },
        )
        return number


def _simple_plan() -> Decomposition:
    e = Epic(id="e1", title="Epic 1", description="epic desc")
    s = Story(
        id="s1",
        epic_id="e1",
        title="Story 1",
        description="story desc",
        acceptance_criteria=("does X", "does Y"),
    )
    t1 = Task(id="t1", story_id="s1", title="Task 1", description="t1 desc")
    t2 = Task(
        id="t2",
        story_id="s1",
        title="Task 2",
        description="t2 desc",
        depends_on=("t1",),
    )
    return Decomposition(vision="v", epics=(e,), stories=(s,), tasks=(t1, t2))


class TestMaterialisePlan:
    async def test_creates_one_issue_per_entity(self) -> None:
        gh = _FakeGh()
        plan = _simple_plan()
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        # 1 epic + 1 story + 2 tasks = 4
        assert len(gh.calls) == 4
        assert set(mapping) == {"e1", "s1", "t1", "t2"}

    async def test_labels_include_director_level_tag(self) -> None:
        gh = _FakeGh()
        plan = _simple_plan()
        await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        # order: epic, story, tasks (topologically)
        labels_by_index = [call["labels"] for call in gh.calls]
        assert "director:epic" in labels_by_index[0]  # type: ignore[operator]
        assert "director:story" in labels_by_index[1]  # type: ignore[operator]
        assert "director:task" in labels_by_index[2]  # type: ignore[operator]
        assert "director:task" in labels_by_index[3]  # type: ignore[operator]

    async def test_custom_label_prefix(self) -> None:
        gh = _FakeGh()
        plan = _simple_plan()
        await materialise_plan(
            plan,
            repo="o/r",
            create_issue=gh.create,
            label_prefix="plan",
        )
        assert "plan:epic" in gh.calls[0]["labels"]  # type: ignore[operator]
        assert "plan:story" in gh.calls[1]["labels"]  # type: ignore[operator]
        assert "plan:task" in gh.calls[2]["labels"]  # type: ignore[operator]

    async def test_story_body_contains_part_of_epic_reference(self) -> None:
        gh = _FakeGh(start=200)
        plan = _simple_plan()
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        epic_number = mapping["e1"]
        story_body = gh.calls[1]["body"]
        assert isinstance(story_body, str)
        assert f"Part of #{epic_number}" in story_body

    async def test_task_body_contains_part_of_story_and_depends_on(self) -> None:
        gh = _FakeGh(start=500)
        plan = _simple_plan()
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        story_number = mapping["s1"]
        t1_number = mapping["t1"]
        # Task 2 depends on Task 1.
        t2_body = gh.calls[3]["body"]
        assert isinstance(t2_body, str)
        assert f"Part of #{story_number}" in t2_body
        assert f"Depends on #{t1_number}" in t2_body

    async def test_task_with_multiple_deps_renders_comma_list(self) -> None:
        e = Epic(id="e1", title="E", description="d")
        s = Story(
            id="s1",
            epic_id="e1",
            title="S",
            description="d",
            acceptance_criteria=("ok",),
        )
        t1 = Task(id="t1", story_id="s1", title="T1", description="d")
        t2 = Task(id="t2", story_id="s1", title="T2", description="d")
        t3 = Task(
            id="t3",
            story_id="s1",
            title="T3",
            description="d",
            depends_on=("t1", "t2"),
        )
        plan = Decomposition(vision="v", epics=(e,), stories=(s,), tasks=(t1, t2, t3))
        gh = _FakeGh(start=10)
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        # Last call is t3.
        t3_body = gh.calls[-1]["body"]
        assert isinstance(t3_body, str)
        n1, n2 = mapping["t1"], mapping["t2"]
        assert f"Depends on #{n1}, #{n2}" in t3_body

    async def test_returns_slug_to_number_map(self) -> None:
        gh = _FakeGh(start=42)
        plan = _simple_plan()
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        assert mapping["e1"] == 42
        assert mapping["s1"] == 43
        assert mapping["t1"] == 44
        assert mapping["t2"] == 45

    async def test_empty_decomposition_creates_zero_issues(self) -> None:
        gh = _FakeGh()
        plan = Decomposition(vision="v", epics=(), stories=(), tasks=())
        mapping = await materialise_plan(plan, repo="o/r", create_issue=gh.create)
        assert mapping == {}
        assert gh.calls == []

    async def test_create_issue_errors_propagate(self) -> None:
        async def broken(
            repo: str,
            *,
            title: str,
            body: str,
            labels: Sequence[str],
        ) -> int:
            raise RuntimeError("gh exploded")

        plan = _simple_plan()
        with pytest.raises(RuntimeError, match="gh exploded"):
            await materialise_plan(plan, repo="o/r", create_issue=broken)
