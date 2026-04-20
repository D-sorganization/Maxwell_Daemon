"""Tests for the event-sourced session log.

Every interaction is an append-only event; the log is the truth. Replay
reconstructs a readable transcript; forking at an event yields a new
session rooted at that event's prefix. No hidden state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from maxwell_daemon.session import (
    AgentFinish,
    CondensationEvent,
    ObservationEvent,
    SessionLog,
    ToolUseEvent,
    UserMessage,
    load_events,
    replay_transcript,
)

# ── Shape ────────────────────────────────────────────────────────────────────


class TestEventShapes:
    def test_user_message_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        e = UserMessage(session_id="s", seq=1, content="hi")
        with pytest.raises(FrozenInstanceError):
            e.content = "x"  # type: ignore[misc]

    def test_tool_use_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        e = ToolUseEvent(session_id="s", seq=2, tool="read_file", arguments={"path": "x"})
        with pytest.raises(FrozenInstanceError):
            e.tool = "write_file"  # type: ignore[misc]

    def test_every_event_has_kind_field(self) -> None:
        """Kind is the discriminator when we serialise to JSON."""
        assert UserMessage(session_id="s", seq=0, content="").kind == "user_message"
        assert ToolUseEvent(session_id="s", seq=0, tool="t", arguments={}).kind == "tool_use"
        assert (
            ObservationEvent(session_id="s", seq=0, tool="t", content="", is_error=False).kind
            == "observation"
        )
        assert (
            CondensationEvent(session_id="s", seq=0, summarised_range=(1, 5), summary="").kind
            == "condensation"
        )
        assert (
            AgentFinish(session_id="s", seq=0, reason="end_turn", final_text="").kind
            == "agent_finish"
        )


# ── Append + read ────────────────────────────────────────────────────────────


class TestSessionLog:
    def test_append_writes_jsonl(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s1", directory=tmp_path)
        e1 = UserMessage(session_id="s1", seq=0, content="hello")
        log.append(e1)
        lines = (tmp_path / "s1.jsonl").read_text().splitlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["kind"] == "user_message"
        assert parsed["content"] == "hello"
        assert parsed["seq"] == 0

    def test_append_is_append_only(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(UserMessage(session_id="s", seq=0, content="a"))
        log.append(UserMessage(session_id="s", seq=1, content="b"))
        lines = (tmp_path / "s.jsonl").read_text().splitlines()
        assert len(lines) == 2

    def test_session_id_mismatch_rejected(self, tmp_path: Path) -> None:
        """A log labelled 's1' must refuse events stamped for a different session."""
        log = SessionLog(session_id="s1", directory=tmp_path)
        with pytest.raises(ValueError, match="session_id"):
            log.append(UserMessage(session_id="s2", seq=0, content="foreign"))

    def test_seq_monotonic_enforced(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(UserMessage(session_id="s", seq=0, content="a"))
        with pytest.raises(ValueError, match="seq"):
            log.append(UserMessage(session_id="s", seq=0, content="dup"))
        with pytest.raises(ValueError, match="seq"):
            log.append(UserMessage(session_id="s", seq=5, content="skip"))

    def test_auto_seq_assignment(self, tmp_path: Path) -> None:
        """``append_event`` variants that don't care about seq get it assigned."""
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append_auto(UserMessage(session_id="s", seq=-1, content="a"))
        log.append_auto(UserMessage(session_id="s", seq=-1, content="b"))
        events = tuple(load_events(tmp_path / "s.jsonl"))
        assert [e.seq for e in events] == [0, 1]


# ── Load ────────────────────────────────────────────────────────────────────


class TestLoadEvents:
    def test_load_round_trip(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(UserMessage(session_id="s", seq=0, content="hello"))
        log.append(ToolUseEvent(session_id="s", seq=1, tool="read_file", arguments={"path": "x"}))
        log.append(
            ObservationEvent(
                session_id="s", seq=2, tool="read_file", content="body", is_error=False
            )
        )
        log.append(AgentFinish(session_id="s", seq=3, reason="end_turn", final_text="done"))

        events = tuple(load_events(tmp_path / "s.jsonl"))
        assert [type(e).__name__ for e in events] == [
            "UserMessage",
            "ToolUseEvent",
            "ObservationEvent",
            "AgentFinish",
        ]
        assert events[1].tool == "read_file"  # type: ignore[attr-defined]
        assert events[2].content == "body"  # type: ignore[attr-defined]

    def test_load_nonexistent_file_returns_empty(self, tmp_path: Path) -> None:
        assert tuple(load_events(tmp_path / "missing.jsonl")) == ()

    def test_load_ignores_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            '{"kind": "user_message", "session_id": "s", "seq": 0, "content": "ok"}\n'
            "not json at all\n"
            '{"kind": "user_message", "session_id": "s", "seq": 1, "content": "also ok"}\n'
        )
        events = tuple(load_events(path))
        assert len(events) == 2
        assert events[0].content == "ok"  # type: ignore[attr-defined]
        assert events[1].content == "also ok"  # type: ignore[attr-defined]

    def test_load_ignores_unknown_kind(self, tmp_path: Path) -> None:
        path = tmp_path / "s.jsonl"
        path.write_text(
            '{"kind": "something_future", "session_id": "s", "seq": 0}\n'
            '{"kind": "user_message", "session_id": "s", "seq": 1, "content": "ok"}\n'
        )
        events = tuple(load_events(path))
        assert [type(e).__name__ for e in events] == ["UserMessage"]


# ── Replay / transcript ─────────────────────────────────────────────────────


class TestReplay:
    def test_replay_transcript_roundtrip(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(UserMessage(session_id="s", seq=0, content="fix this bug"))
        log.append(
            ToolUseEvent(session_id="s", seq=1, tool="read_file", arguments={"path": "bug.py"})
        )
        log.append(
            ObservationEvent(
                session_id="s", seq=2, tool="read_file", content="def buggy(): ...", is_error=False
            )
        )
        log.append(AgentFinish(session_id="s", seq=3, reason="end_turn", final_text="fixed"))

        transcript = replay_transcript(tmp_path / "s.jsonl")
        assert "fix this bug" in transcript
        assert "read_file" in transcript
        assert "def buggy" in transcript
        assert "end_turn" in transcript
        # Events appear in sequence order.
        assert transcript.index("fix this bug") < transcript.index("read_file")
        assert transcript.index("read_file") < transcript.index("end_turn")

    def test_replay_marks_tool_errors(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(ToolUseEvent(session_id="s", seq=0, tool="read_file", arguments={"path": "x"}))
        log.append(
            ObservationEvent(
                session_id="s", seq=1, tool="read_file", content="not found", is_error=True
            )
        )
        transcript = replay_transcript(tmp_path / "s.jsonl")
        assert "ERROR" in transcript.upper() or "error" in transcript

    def test_replay_shows_condensation(self, tmp_path: Path) -> None:
        log = SessionLog(session_id="s", directory=tmp_path)
        log.append(UserMessage(session_id="s", seq=0, content="task"))
        log.append(
            CondensationEvent(
                session_id="s", seq=1, summarised_range=(2, 15), summary="did some stuff"
            )
        )
        log.append(AgentFinish(session_id="s", seq=2, reason="end_turn", final_text="ok"))
        transcript = replay_transcript(tmp_path / "s.jsonl")
        assert "condensation" in transcript.lower() or "summarised" in transcript.lower()
        assert "did some stuff" in transcript


# ── Session discovery ──────────────────────────────────────────────────────


class TestDiscovery:
    def test_list_sessions_in_directory(self, tmp_path: Path) -> None:
        from maxwell_daemon.session import list_sessions

        log_a = SessionLog(session_id="alpha", directory=tmp_path)
        log_b = SessionLog(session_id="beta", directory=tmp_path)
        log_a.append(UserMessage(session_id="alpha", seq=0, content="a"))
        log_b.append(UserMessage(session_id="beta", seq=0, content="b"))
        (tmp_path / "not-a-log.txt").write_text("noise")

        assert sorted(list_sessions(tmp_path)) == ["alpha", "beta"]

    def test_list_sessions_empty_dir(self, tmp_path: Path) -> None:
        from maxwell_daemon.session import list_sessions

        assert list_sessions(tmp_path) == ()
