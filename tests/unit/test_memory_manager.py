"""MemoryManager — composite that assembles context for the IssueExecutor."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.memory import (
    Episode,
    EpisodicStore,
    MemoryManager,
    RepoProfile,
    ScratchPad,
)


@pytest.fixture
def manager(tmp_path: Path) -> MemoryManager:
    return MemoryManager(
        scratchpad=ScratchPad(),
        profile=RepoProfile(tmp_path / "m.db"),
        episodes=EpisodicStore(tmp_path / "m.db"),
    )


class TestAssembleContext:
    def test_empty_memory_returns_empty_string(self, manager: MemoryManager) -> None:
        assembled = manager.assemble_context(
            repo="o/r", issue_title="t", issue_body="b", task_id="task-1"
        )
        assert assembled == ""

    def test_includes_profile_facts(self, manager: MemoryManager) -> None:
        manager.profile.learn("o/r", "language", "python")
        manager.profile.learn("o/r", "test_runner", "pytest")
        assembled = manager.assemble_context(
            repo="o/r", issue_title="fix it", issue_body="x", task_id="task-1"
        )
        assert "language: python" in assembled
        assert "test_runner: pytest" in assembled

    def test_includes_related_episodes(self, manager: MemoryManager) -> None:
        manager.episodes.record(
            Episode(
                id="e1",
                repo="o/r",
                issue_number=5,
                issue_title="fix parser on empty input",
                issue_body="segfault",
                plan="guard the empty case",
                applied_diff=True,
                pr_url="u",
                outcome="merged",
            )
        )
        assembled = manager.assemble_context(
            repo="o/r",
            issue_title="another parser crash",
            issue_body="dies on null input",
            task_id="task-1",
        )
        assert "parser" in assembled
        assert "guard the empty case" in assembled

    def test_includes_scratchpad(self, manager: MemoryManager) -> None:
        manager.scratchpad.append("task-1", role="plan", content="initial plan draft")
        assembled = manager.assemble_context(
            repo="o/r", issue_title="t", issue_body="b", task_id="task-1"
        )
        assert "initial plan draft" in assembled

    def test_budget_bound_respected(self, manager: MemoryManager) -> None:
        for i in range(20):
            manager.profile.learn("o/r", f"k{i}", "x" * 500)
        assembled = manager.assemble_context(
            repo="o/r", issue_title="t", issue_body="b", task_id="t", max_chars=2000
        )
        assert len(assembled) <= 2500  # some slack for section headers


class TestRecordOutcome:
    def test_records_episode(self, manager: MemoryManager) -> None:
        manager.record_outcome(
            task_id="task-1",
            repo="o/r",
            issue_number=5,
            issue_title="parser fix",
            issue_body="body",
            plan="the plan",
            applied_diff=True,
            pr_url="https://github.com/o/r/pull/5",
            outcome="merged",
        )
        assert manager.episodes.search("parser", limit=5)

    def test_clears_scratchpad_for_task(self, manager: MemoryManager) -> None:
        manager.scratchpad.append("task-x", role="plan", content="p")
        manager.record_outcome(
            task_id="task-x",
            repo="o/r",
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan="p",
            applied_diff=False,
            pr_url="u",
            outcome="merged",
        )
        assert manager.scratchpad.entries("task-x") == []
