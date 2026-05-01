"""Task data models for the Maxwell-Daemon runner (phase 1 of #798).

Extracted from ``maxwell_daemon/daemon/runner.py`` to give the core task
data structures their own focused module, decoupled from the Daemon
orchestration logic.

These types are imported by ``runner.py`` and exposed through
``maxwell_daemon.daemon`` for backwards-compatibility.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

__all__ = [
    "DaemonState",
    "DuplicateTaskIdError",
    "QueueSaturationError",
    "Task",
    "TaskKind",
    "TaskStatus",
]


class QueueSaturationError(Exception):
    """Raised when the priority queue is full and cannot accept more tasks."""

    def __init__(self, message: str, backoff_seconds: int = 60) -> None:
        super().__init__(message)
        self.backoff_seconds = backoff_seconds


class TaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"  # assigned to a remote worker, awaiting execution
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskKind(str, Enum):
    PROMPT = "prompt"
    ISSUE = "issue"


class DuplicateTaskIdError(ValueError):
    """Raised when a caller supplies a task id that already exists."""


@functools.total_ordering
@dataclass
class Task:
    id: str
    prompt: str
    kind: TaskKind = TaskKind.PROMPT
    repo: str | None = None
    backend: str | None = None
    model: str | None = None
    route_reason: str | None = None
    # Issue-specific fields (set when kind == ISSUE).
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None  # "plan" | "implement"
    # A/B grouping: sibling tasks share an ab_group so the UI pairs them.
    ab_group: str | None = None
    # Continuation-turn identity for multi-turn task iteration.
    # ``thread_id`` groups turns; ``turn_count`` is the active zero-based turn id.
    thread_id: str | None = None
    turn_count: int = 0
    max_turns: int = 20
    # DAG dependencies: list of task IDs that must reach COMPLETED before this
    # task is allowed to start.  An empty list (the default) means "no deps".
    depends_on: list[str] = field(default_factory=list)
    # Priority: lower number = higher priority. 0=emergency, 50=high, 100=normal, 200=batch.
    priority: int = 100
    dry_run: bool = False
    status: TaskStatus = TaskStatus.QUEUED
    result: str | None = None
    error: str | None = None
    waived_by: str | None = None
    waiver_reason: str | None = None
    waived_at: datetime | None = None
    cost_usd: float = 0.0
    pr_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Fleet dispatch tracking: set when a coordinator sends this task to a remote worker.
    dispatched_to: str | None = None  # machine name of the worker that received this task
    side_effects_started: bool = False

    @property
    def continuation_thread_id(self) -> str:
        """Stable logical thread id used for continuation session naming."""
        return self.thread_id or self.id

    @property
    def turn_session_id(self) -> str:
        """Return the Symphony-style ``<thread_id>-<turn_id>`` session id."""
        return f"{self.continuation_thread_id}-{self.turn_count}"

    @property
    def is_continuation_turn(self) -> bool:
        """Whether the active turn should send continuation guidance only."""
        return self.turn_count > 0

    @property
    def has_turn_budget(self) -> bool:
        """Whether another turn may start without exceeding ``max_turns``."""
        return self.turn_count < self.max_turns

    def __lt__(self, other: object) -> bool:
        """Support PriorityQueue ordering — compare by (priority, created_at)."""
        if not isinstance(other, Task):
            return NotImplemented
        return (self.priority, self.created_at) < (other.priority, other.created_at)


@dataclass
class DaemonState:
    version: str
    config_path: Path | None
    tasks: dict[str, Task]
    started_at: datetime
    backends_available: list[str]
    worker_count: int = 0
    queue_depth: int = 0
    needs_restart: bool = False
    restart_required_reasons: list[str] = field(default_factory=list)
