"""Append-only JSONL event log for one agent session.

Every meaningful moment in an agent run is a :class:`SessionEvent`:

  * :class:`UserMessage`        — the operator-authored task or follow-up
  * :class:`ToolUseEvent`       — the agent asked for a tool
  * :class:`ObservationEvent`   — the tool came back with a result
  * :class:`CondensationEvent`  — middle turns compressed to a summary
  * :class:`AgentFinish`        — the loop terminated (with reason)

Events are strictly append-only; the log file is the source of truth.
Any session can be replayed into a readable transcript or forked at a
specific ``seq`` to explore alternate futures.

Why separate from :mod:`maxwell_daemon.events` (the in-process bus)?
The bus is ephemeral fan-out for telemetry; the session log is the
audit trail. Different durability, different consumers, different
invariants.

DbC: ``SessionLog`` rejects events with a mismatched ``session_id`` or a
non-monotonic ``seq``. Malformed events on load are silently skipped so
one bad line doesn't strand an otherwise-valid transcript.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "AgentFinish",
    "CondensationEvent",
    "ObservationEvent",
    "SessionEvent",
    "SessionLog",
    "ToolUseEvent",
    "UserMessage",
    "list_sessions",
    "load_events",
    "replay_transcript",
]


# ── Event hierarchy ────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class UserMessage:
    """Operator-authored text — the task, or mid-run clarification."""

    session_id: str
    seq: int
    content: str
    kind: str = "user_message"


@dataclass(slots=True, frozen=True)
class ToolUseEvent:
    """The agent asked for a tool to be invoked."""

    session_id: str
    seq: int
    tool: str
    arguments: dict[str, Any]
    kind: str = "tool_use"


@dataclass(slots=True, frozen=True)
class ObservationEvent:
    """A tool returned; ``is_error`` distinguishes failures from successes."""

    session_id: str
    seq: int
    tool: str
    content: str
    is_error: bool
    kind: str = "observation"


@dataclass(slots=True, frozen=True)
class CondensationEvent:
    """Middle turns compressed to a summary. ``summarised_range`` is inclusive."""

    session_id: str
    seq: int
    summarised_range: tuple[int, int]
    summary: str
    kind: str = "condensation"


@dataclass(slots=True, frozen=True)
class AgentFinish:
    """The loop terminated — whether by end_turn, timeout, budget, or crash."""

    session_id: str
    seq: int
    reason: str
    final_text: str
    kind: str = "agent_finish"


#: Union of every concrete event type. Handy for type-annotating iterators.
SessionEvent = UserMessage | ToolUseEvent | ObservationEvent | CondensationEvent | AgentFinish


_KIND_MAP: dict[str, type] = {
    "user_message": UserMessage,
    "tool_use": ToolUseEvent,
    "observation": ObservationEvent,
    "condensation": CondensationEvent,
    "agent_finish": AgentFinish,
}


# ── Log ────────────────────────────────────────────────────────────────────


class SessionLog:
    """Owns one append-only ``.jsonl`` file.

    Each :meth:`append` serialises the event to a single line, asserts
    monotonic ``seq``, and checks the session_id matches the log's.
    :meth:`append_auto` fills in ``seq`` automatically so callers that
    don't track it don't have to.
    """

    def __init__(self, *, session_id: str, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._session_id = session_id
        self._path = directory / f"{session_id}.jsonl"
        self._next_seq = 0
        if self._path.is_file():
            # Resume: pick up where the previous run left off.
            for event in load_events(self._path):
                self._next_seq = max(self._next_seq, event.seq + 1)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_id(self) -> str:
        return self._session_id

    def append(self, event: SessionEvent) -> None:
        """Write ``event`` to disk.

        Raises ``ValueError`` if the event is stamped for a different
        session, or its ``seq`` is not equal to the next expected
        ``seq`` for this log.
        """
        if event.session_id != self._session_id:
            raise ValueError(
                f"session_id mismatch: log is for {self._session_id!r}, "
                f"event has {event.session_id!r}"
            )
        if event.seq != self._next_seq:
            raise ValueError(
                f"seq mismatch: expected {self._next_seq}, got {event.seq} for {event.kind}"
            )
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), default=str) + "\n")
        self._next_seq += 1

    def append_auto(self, event: SessionEvent) -> None:
        """Like :meth:`append` but ignores the event's ``seq`` and assigns the next one."""
        stamped = _replace_seq(event, self._next_seq)
        self.append(stamped)


def _replace_seq(event: SessionEvent, seq: int) -> SessionEvent:
    """Return a copy of ``event`` with ``seq`` overridden."""
    data = asdict(event)
    data["seq"] = seq
    kind = data.pop("kind")
    cls = _KIND_MAP[kind]
    return cls(**data)  # type: ignore[no-any-return]


# ── Load + render ──────────────────────────────────────────────────────────


def load_events(path: Path) -> Iterator[SessionEvent]:
    """Stream events from a JSONL log. Malformed lines are silently skipped."""
    if not path.is_file():
        return
    with path.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = parsed.get("kind")
            cls = _KIND_MAP.get(kind)
            if cls is None:
                continue
            parsed.pop("kind", None)
            try:
                yield cls(**parsed)
            except TypeError:
                continue


def list_sessions(directory: Path) -> tuple[str, ...]:
    """Return session IDs for every ``*.jsonl`` in ``directory``."""
    if not directory.is_dir():
        return ()
    return tuple(sorted(p.stem for p in directory.iterdir() if p.suffix == ".jsonl"))


def replay_transcript(path: Path) -> str:
    """Render a log as a human-readable transcript."""
    blocks: list[str] = []
    for event in load_events(path):
        blocks.append(_render_event(event))
    return "\n\n".join(blocks)


def _render_event(event: SessionEvent) -> str:
    if isinstance(event, UserMessage):
        return f"[{event.seq}] user:\n{event.content}"
    if isinstance(event, ToolUseEvent):
        args = json.dumps(event.arguments, ensure_ascii=False)
        return f"[{event.seq}] tool_use {event.tool}({args})"
    if isinstance(event, ObservationEvent):
        marker = "ERROR" if event.is_error else "ok"
        return f"[{event.seq}] observation [{marker}] from {event.tool}:\n{event.content}"
    if isinstance(event, CondensationEvent):
        a, b = event.summarised_range
        return f"[{event.seq}] condensation (summarised seqs {a}-{b}):\n{event.summary}"
    if isinstance(event, AgentFinish):
        return f"[{event.seq}] agent_finish reason={event.reason}\n{event.final_text}"
    return f"[{event.seq}] <unknown event kind>"  # pragma: no cover
