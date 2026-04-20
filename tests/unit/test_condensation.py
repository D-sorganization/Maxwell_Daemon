"""Tests for Condenser — shrinks long agent message histories via LLM summary.

For long-running stories (100+ turns), the message list grows until it hits
the context window. Condenser splits the history into [anchor][mid][tail]
segments, replaces the middle with a one-line "[prior turns summarized]"
synthetic assistant message, and preserves the original task plus the most
recent turns.

Tests exercise the orchestration (what's kept, what's dropped, where the
summary lands); the summarizer itself is injected so tests never hit an LLM.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.backends.condensation import Condenser


async def _stub_summarizer(messages: list[dict[str, object]]) -> str:
    return f"<summary of {len(messages)} turns>"


# ── Threshold policy ─────────────────────────────────────────────────────────


class TestShouldCondense:
    def test_below_threshold_returns_false(self) -> None:
        c = Condenser(threshold_tokens=1000, keep_recent=2, summarizer=_stub_summarizer)
        assert c.should_condense(999) is False

    def test_at_threshold_returns_true(self) -> None:
        c = Condenser(threshold_tokens=1000, keep_recent=2, summarizer=_stub_summarizer)
        assert c.should_condense(1000) is True

    def test_above_threshold_returns_true(self) -> None:
        c = Condenser(threshold_tokens=1000, keep_recent=2, summarizer=_stub_summarizer)
        assert c.should_condense(5_000) is True


# ── Structural invariants ────────────────────────────────────────────────────


class TestCondense:
    async def test_no_op_when_too_short_to_compress(self) -> None:
        c = Condenser(threshold_tokens=1, keep_recent=10, summarizer=_stub_summarizer)
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "ok"},
        ]
        out = await c.condense(messages)
        # 2 messages < anchor(1) + keep_recent(10) + summary(1) → leave untouched.
        assert out == messages

    async def test_preserves_first_user_message(self) -> None:
        c = Condenser(threshold_tokens=1, keep_recent=2, summarizer=_stub_summarizer)
        messages = [
            {"role": "user", "content": "the original task"},
            {"role": "assistant", "content": "m1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "m3"},
            {"role": "user", "content": "m4"},
            {"role": "assistant", "content": "m5"},
        ]
        out = await c.condense(messages)
        # First message always retained so the task statement stays visible.
        assert out[0] == {"role": "user", "content": "the original task"}

    async def test_preserves_last_keep_recent_messages(self) -> None:
        c = Condenser(threshold_tokens=1, keep_recent=2, summarizer=_stub_summarizer)
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "m1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "m3"},
            {"role": "user", "content": "latest-1"},
            {"role": "assistant", "content": "latest"},
        ]
        out = await c.condense(messages)
        assert out[-2:] == [
            {"role": "user", "content": "latest-1"},
            {"role": "assistant", "content": "latest"},
        ]

    async def test_injects_summary_between_anchor_and_tail(self) -> None:
        c = Condenser(threshold_tokens=1, keep_recent=2, summarizer=_stub_summarizer)
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "m1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "m3"},
            {"role": "user", "content": "latest-1"},
            {"role": "assistant", "content": "latest"},
        ]
        out = await c.condense(messages)
        # Shape: [task, summary, latest-1, latest]
        assert len(out) == 4
        summary_msg = out[1]
        assert summary_msg["role"] == "user"
        body = summary_msg["content"]
        assert isinstance(body, str)
        assert "summary" in body.lower()

    async def test_summarizer_receives_middle_slice(self) -> None:
        seen: list[list[dict[str, object]]] = []

        async def recording(messages: list[dict[str, object]]) -> str:
            seen.append(messages)
            return "summary"

        c = Condenser(threshold_tokens=1, keep_recent=2, summarizer=recording)
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "middle-1"},
            {"role": "user", "content": "middle-2"},
            {"role": "assistant", "content": "middle-3"},
            {"role": "user", "content": "last-user"},
            {"role": "assistant", "content": "last-assist"},
        ]
        await c.condense(messages)
        assert seen
        mid = seen[0]
        # Summarizer sees the middle 3 — not the anchor, not the tail.
        assert [m["content"] for m in mid] == ["middle-1", "middle-2", "middle-3"]

    async def test_summarizer_failure_returns_original(self) -> None:
        async def exploding(_: list[dict[str, object]]) -> str:
            raise RuntimeError("summarizer crashed")

        c = Condenser(threshold_tokens=1, keep_recent=2, summarizer=exploding)
        messages = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "m1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "m3"},
            {"role": "user", "content": "latest-1"},
            {"role": "assistant", "content": "latest"},
        ]
        out = await c.condense(messages)
        # On summarizer failure we keep the original rather than drop context.
        assert out == messages


# ── DbC ──────────────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_threshold_must_be_positive(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="threshold"):
            Condenser(threshold_tokens=0, keep_recent=2, summarizer=_stub_summarizer)

    def test_keep_recent_must_be_positive(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="keep_recent"):
            Condenser(threshold_tokens=1000, keep_recent=0, summarizer=_stub_summarizer)
