"""ScratchPad — ephemeral per-task working memory.

Keeps prior plans/diffs/test-output across retries of the same task so the
LLM refinement loop has full context without re-paying for tokens.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.memory import ScratchPad


class TestScratchPad:
    def test_empty_pad_has_no_entries(self) -> None:
        pad = ScratchPad()
        assert pad.entries("task-1") == []

    def test_append_and_retrieve(self) -> None:
        pad = ScratchPad()
        pad.append("task-1", role="plan", content="do the thing")
        pad.append("task-1", role="diff", content="--- a\n+++ b\n")
        entries = pad.entries("task-1")
        assert [e.role for e in entries] == ["plan", "diff"]
        assert entries[0].content == "do the thing"

    def test_isolated_per_task(self) -> None:
        pad = ScratchPad()
        pad.append("task-1", role="plan", content="A")
        pad.append("task-2", role="plan", content="B")
        assert pad.entries("task-1")[0].content == "A"
        assert pad.entries("task-2")[0].content == "B"

    def test_clear_removes_task(self) -> None:
        pad = ScratchPad()
        pad.append("task-1", role="plan", content="x")
        pad.clear("task-1")
        assert pad.entries("task-1") == []

    def test_render_for_prompt(self) -> None:
        pad = ScratchPad()
        pad.append("task-1", role="plan", content="the plan")
        pad.append("task-1", role="test_output", content="FAILED test_x")
        rendered = pad.render("task-1")
        assert "the plan" in rendered
        assert "FAILED" in rendered

    def test_render_respects_max_chars(self) -> None:
        pad = ScratchPad()
        pad.append("task-1", role="big", content="x" * 10_000)
        rendered = pad.render("task-1", max_chars=500)
        assert len(rendered) <= 600  # slack for role headers + truncation marker

    def test_rejects_empty_task_id(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        pad = ScratchPad()
        with pytest.raises(PreconditionError):
            pad.append("", role="plan", content="x")

    def test_cap_per_task_drops_oldest(self) -> None:
        pad = ScratchPad(max_entries_per_task=3)
        for i in range(5):
            pad.append("t", role="note", content=f"e{i}")
        entries = pad.entries("t")
        assert len(entries) == 3
        assert entries[0].content == "e2"
        assert entries[-1].content == "e4"
