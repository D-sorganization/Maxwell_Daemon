"""Provider-agnostic delegate lifecycle models and lease mechanics."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from maxwell_daemon.contracts import require

__all__ = [
    "AssignmentLease",
    "Checkpoint",
    "Delegate",
    "DelegateLifecycleManager",
    "DelegateLifecycleService",
    "DelegateSession",
    "DelegateSessionSnapshot",
    "DelegateSessionStatus",
    "DelegateSessionStore",
    "HandoffArtifact",
    "LeaseRecoveryPolicy",
    "validate_delegate_session_transition",
]

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_iso_required(value: str) -> datetime:
    parsed = _parse_iso(value)
    if parsed is None:
        raise ValueError("expected non-empty timestamp")
    return parsed


class DelegateSessionStatus(str, Enum):
    """Lifecycle status for one concrete delegate execution session."""

    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    SUPERSEDED = "superseded"


class LeaseRecoveryPolicy(str, Enum):
    """Recovery behavior once a session lease expires."""

    ABANDON = "abandon"
    RECOVERABLE = "recoverable"
    TAKEOVER_ALLOWED = "takeover_allowed"


VALID_DELEGATE_SESSION_TRANSITIONS: dict[
    DelegateSessionStatus, frozenset[DelegateSessionStatus]
] = {
    DelegateSessionStatus.QUEUED: frozenset(
        {DelegateSessionStatus.LEASED, DelegateSessionStatus.ABANDONED}
    ),
    DelegateSessionStatus.LEASED: frozenset(
        {
            DelegateSessionStatus.RUNNING,
            DelegateSessionStatus.PAUSED,
            DelegateSessionStatus.ABANDONED,
            DelegateSessionStatus.SUPERSEDED,
        }
    ),
    DelegateSessionStatus.RUNNING: frozenset(
        {
            DelegateSessionStatus.PAUSED,
            DelegateSessionStatus.BLOCKED,
            DelegateSessionStatus.COMPLETED,
            DelegateSessionStatus.FAILED,
            DelegateSessionStatus.ABANDONED,
            DelegateSessionStatus.SUPERSEDED,
        }
    ),
    DelegateSessionStatus.PAUSED: frozenset(
        {
            DelegateSessionStatus.LEASED,
            DelegateSessionStatus.RUNNING,
            DelegateSessionStatus.ABANDONED,
            DelegateSessionStatus.SUPERSEDED,
        }
    ),
    DelegateSessionStatus.BLOCKED: frozenset(
        {
            DelegateSessionStatus.RUNNING,
            DelegateSessionStatus.PAUSED,
            DelegateSessionStatus.FAILED,
            DelegateSessionStatus.ABANDONED,
            DelegateSessionStatus.SUPERSEDED,
        }
    ),
    DelegateSessionStatus.COMPLETED: frozenset(),
    DelegateSessionStatus.FAILED: frozenset({DelegateSessionStatus.ABANDONED}),
    DelegateSessionStatus.ABANDONED: frozenset(),
    DelegateSessionStatus.SUPERSEDED: frozenset(),
}


def validate_delegate_session_transition(
    current: DelegateSessionStatus,
    new: DelegateSessionStatus,
) -> None:
    """Raise ValueError when a delegate session lifecycle transition is not allowed."""

    if current == new:
        return
    if new not in VALID_DELEGATE_SESSION_TRANSITIONS[current]:
        raise ValueError(
            f"invalid delegate session transition from {current.value!r} to {new.value!r}"
        )


class Delegate(BaseModel):
    """Logical delegate role and execution limits."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    capability_tags: tuple[str, ...] = Field(..., min_length=1)
    allowed_tools: tuple[str, ...] = Field(..., min_length=1)
    max_budget_usd: PositiveFloat
    max_wall_clock_seconds: PositiveInt
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssignmentLease(BaseModel):
    """Exclusive write lease for one delegate session."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default="", min_length=0)
    session_id: str = Field(..., min_length=1)
    owner_id: str = Field(..., min_length=1)
    heartbeat_at: datetime
    expires_at: datetime
    renewal_count: int = Field(default=0, ge=0)
    recovery_policy: LeaseRecoveryPolicy = LeaseRecoveryPolicy.ABANDON
    released_at: datetime | None = None
    expired_at: datetime | None = None
    supersedes_owner_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _lease_times_are_valid(self) -> AssignmentLease:
        self.heartbeat_at = _normalize_datetime(self.heartbeat_at)
        self.expires_at = _normalize_datetime(self.expires_at)
        if self.released_at is not None:
            self.released_at = _normalize_datetime(self.released_at)
        if self.expired_at is not None:
            self.expired_at = _normalize_datetime(self.expired_at)
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("lease expiration must be after heartbeat timestamp")
        return self

    def is_active(self, now: datetime) -> bool:
        checked_at = _normalize_datetime(now)
        return self.released_at is None and self.expired_at is None and self.expires_at > checked_at


class Checkpoint(BaseModel):
    """Durable delegate recovery evidence captured during execution."""

    id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    created_at: datetime
    current_plan: str = Field(..., min_length=1)
    changed_files: tuple[str, ...] = Field(default_factory=tuple)
    test_commands: tuple[str, ...] = Field(default_factory=tuple)
    failures_and_learnings: tuple[str, ...] = Field(default_factory=tuple)
    artifact_refs: tuple[str, ...] = Field(default_factory=tuple)
    resume_prompt: str | None = Field(default=None, min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checkpoint_timestamp_is_timezone_aware(self) -> Checkpoint:
        self.created_at = _normalize_datetime(self.created_at)
        return self


class HandoffArtifact(BaseModel):
    """Typed evidence or work product handed from one delegate to another."""

    id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    artifact_type: str = Field(..., min_length=1)
    artifact_ref: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _handoff_timestamp_is_timezone_aware(self) -> HandoffArtifact:
        self.created_at = _normalize_datetime(self.created_at)
        return self


class DelegateSession(BaseModel):
    """Concrete execution session for a logical delegate."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(..., min_length=1)
    delegate_id: str = Field(..., min_length=1)
    work_item_id: str | None = Field(default=None, min_length=1)
    task_id: str | None = Field(default=None, min_length=1)
    workspace_ref: str = Field(..., min_length=1)
    backend_ref: str = Field(..., min_length=1)
    machine_ref: str = Field(..., min_length=1)
    status: DelegateSessionStatus = DelegateSessionStatus.QUEUED
    active_lease_id: str | None = Field(default=None, min_length=1)
    prior_session_id: str | None = Field(default=None, min_length=1)
    latest_checkpoint_id: str | None = Field(default=None, min_length=1)
    recovered_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _session_invariants_hold(self) -> DelegateSession:
        self.created_at = _normalize_datetime(self.created_at)
        self.updated_at = _normalize_datetime(self.updated_at)
        if self.recovered_at is not None:
            self.recovered_at = _normalize_datetime(self.recovered_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at")
        if self.work_item_id is None and self.task_id is None:
            raise ValueError("delegate session must reference a work item or task")
        if self.status is DelegateSessionStatus.RUNNING and self.active_lease_id is None:
            raise ValueError("running session requires an active lease")
        if self.recovered_at is not None and self.prior_session_id is None:
            raise ValueError("recovered session must record the prior session id")
        return self

    def with_status(
        self,
        new_status: DelegateSessionStatus,
        *,
        lease: AssignmentLease | None = None,
        now: datetime | None = None,
    ) -> DelegateSession:
        checked_at = _normalize_datetime(now or _utc_now())
        validate_delegate_session_transition(self.status, new_status)
        active_lease_id = self.active_lease_id
        if new_status is DelegateSessionStatus.RUNNING:
            if lease is None or not lease.is_active(checked_at):
                raise ValueError("running session requires an active lease")
            active_lease_id = lease.id or _lease_id(
                lease.session_id, lease.owner_id, lease.heartbeat_at
            )
        return DelegateSession(
            **{
                **self.model_dump(),
                "status": new_status,
                "active_lease_id": active_lease_id,
                "updated_at": checked_at,
            }
        )

    @classmethod
    def recover_from(
        cls,
        prior: DelegateSession,
        *,
        new_session_id: str,
        delegate_id: str,
        machine_ref: str,
        backend_ref: str,
        latest_checkpoint: Checkpoint,
        now: datetime | None = None,
    ) -> DelegateSession:
        recovered_at = _normalize_datetime(now or _utc_now())
        if latest_checkpoint.session_id != prior.id:
            raise ValueError("latest checkpoint must belong to the prior session")
        return cls(
            id=new_session_id,
            delegate_id=delegate_id,
            work_item_id=prior.work_item_id,
            task_id=prior.task_id,
            workspace_ref=prior.workspace_ref,
            backend_ref=backend_ref,
            machine_ref=machine_ref,
            status=DelegateSessionStatus.QUEUED,
            prior_session_id=prior.id,
            latest_checkpoint_id=latest_checkpoint.id,
            recovered_at=recovered_at,
            created_at=recovered_at,
            updated_at=recovered_at,
        )


class DelegateLifecycleManager:
    """Deterministic in-memory lease manager for delegate lifecycle tests."""

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or _utc_now
        self._active_leases: dict[str, AssignmentLease] = {}
        self._last_leases: dict[str, AssignmentLease] = {}

    def current_lease(self, session_id: str) -> AssignmentLease | None:
        now = self._now()
        lease = self._active_leases.get(session_id)
        if lease is None:
            return None
        if not lease.is_active(now):
            expired = lease.model_copy(update={"expired_at": now})
            self._active_leases.pop(session_id, None)
            self._last_leases[session_id] = expired
            return None
        return lease

    def acquire_lease(
        self,
        *,
        session: DelegateSession,
        owner_id: str,
        ttl: timedelta,
        recovery_policy: LeaseRecoveryPolicy = LeaseRecoveryPolicy.ABANDON,
    ) -> AssignmentLease:
        now = self._now()
        current = self.current_lease(session.id)
        if current is not None:
            raise ValueError(f"session {session.id!r} already has an active lease")
        lease = self._new_lease(
            session_id=session.id,
            owner_id=owner_id,
            ttl=ttl,
            recovery_policy=recovery_policy,
            now=now,
        )
        self._active_leases[session.id] = lease
        self._last_leases[session.id] = lease
        return lease

    def renew_lease(self, session_id: str, *, owner_id: str, ttl: timedelta) -> AssignmentLease:
        now = self._now()
        lease = self.current_lease(session_id)
        if lease is None:
            raise ValueError(f"session {session_id!r} does not have an active lease")
        if lease.owner_id != owner_id:
            raise ValueError("only the current lease owner can renew the lease")
        renewed = lease.model_copy(
            update={
                "heartbeat_at": now,
                "expires_at": _expires_at(now, ttl),
                "renewal_count": lease.renewal_count + 1,
            }
        )
        self._active_leases[session_id] = renewed
        self._last_leases[session_id] = renewed
        return renewed

    def expire_lease(self, session_id: str) -> AssignmentLease:
        now = self._now()
        lease = self._active_leases.pop(session_id, None)
        if lease is None:
            lease = self._last_leases.get(session_id)
        if lease is None:
            raise ValueError(f"session {session_id!r} does not have a lease")
        expired = lease.model_copy(update={"expired_at": now})
        self._last_leases[session_id] = expired
        return expired

    def release_lease(self, session_id: str, *, owner_id: str) -> AssignmentLease:
        now = self._now()
        lease = self.current_lease(session_id)
        if lease is None:
            raise ValueError(f"session {session_id!r} does not have an active lease")
        if lease.owner_id != owner_id:
            raise ValueError("only the current lease owner can release the lease")
        released = lease.model_copy(update={"released_at": now})
        self._active_leases.pop(session_id, None)
        self._last_leases[session_id] = released
        return released

    def takeover_lease(
        self,
        *,
        session: DelegateSession,
        owner_id: str,
        ttl: timedelta,
    ) -> AssignmentLease:
        now = self._now()
        current = self.current_lease(session.id)
        if current is not None:
            raise ValueError(f"session {session.id!r} already has an active lease")
        prior = self._last_leases.get(session.id)
        if prior is None:
            raise ValueError(f"session {session.id!r} has no prior lease to take over")
        if prior.recovery_policy is LeaseRecoveryPolicy.ABANDON:
            raise ValueError("lease recovery policy does not allow takeover")
        takeover = self._new_lease(
            session_id=session.id,
            owner_id=owner_id,
            ttl=ttl,
            recovery_policy=prior.recovery_policy,
            now=now,
            supersedes_owner_id=prior.owner_id,
        )
        self._active_leases[session.id] = takeover
        self._last_leases[session.id] = takeover
        return takeover

    def _new_lease(
        self,
        *,
        session_id: str,
        owner_id: str,
        ttl: timedelta,
        recovery_policy: LeaseRecoveryPolicy,
        now: datetime,
        supersedes_owner_id: str | None = None,
    ) -> AssignmentLease:
        if ttl <= timedelta(0):
            raise ValueError("lease ttl must be positive")
        return AssignmentLease(
            id=_lease_id(session_id, owner_id, now),
            session_id=session_id,
            owner_id=owner_id,
            heartbeat_at=now,
            expires_at=_expires_at(now, ttl),
            recovery_policy=recovery_policy,
            supersedes_owner_id=supersedes_owner_id,
        )

    def _now(self) -> datetime:
        return _normalize_datetime(self._clock())


def _expires_at(now: datetime, ttl: timedelta) -> datetime:
    if ttl <= timedelta(0):
        raise ValueError("lease ttl must be positive")
    return _normalize_datetime(now) + ttl


def _lease_id(session_id: str, owner_id: str, heartbeat_at: datetime) -> str:
    timestamp = _normalize_datetime(heartbeat_at).isoformat()
    return f"{session_id}:{owner_id}:{timestamp}"


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _json_tuple(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    loaded = json.loads(value)
    require(isinstance(loaded, list), "delegate lifecycle payload must decode to a list")
    return tuple(str(item) for item in loaded)


class DelegateSessionSnapshot(BaseModel):
    """Durable readback for one delegate session and its evidence."""

    session: DelegateSession
    active_lease: AssignmentLease | None = None
    latest_checkpoint: Checkpoint | None = None
    handoff_artifacts: tuple[HandoffArtifact, ...] = ()

    @property
    def status(self) -> DelegateSessionStatus:
        return self.session.status


class DelegateSessionStore:
    """SQLite persistence for delegate sessions, leases, checkpoints, and handoffs."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS delegate_sessions (
        id TEXT PRIMARY KEY,
        delegate_id TEXT NOT NULL,
        work_item_id TEXT,
        task_id TEXT,
        workspace_ref TEXT NOT NULL,
        backend_ref TEXT NOT NULL,
        machine_ref TEXT NOT NULL,
        status TEXT NOT NULL,
        active_lease_id TEXT,
        prior_session_id TEXT,
        latest_checkpoint_id TEXT,
        recovered_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        metadata TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS delegate_leases (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        renewal_count INTEGER NOT NULL,
        recovery_policy TEXT NOT NULL,
        released_at TEXT,
        expired_at TEXT,
        supersedes_owner_id TEXT
    );
    CREATE TABLE IF NOT EXISTS delegate_checkpoints (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        current_plan TEXT NOT NULL,
        changed_files TEXT NOT NULL,
        test_commands TEXT NOT NULL,
        failures_and_learnings TEXT NOT NULL,
        artifact_refs TEXT NOT NULL,
        resume_prompt TEXT,
        metadata TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS delegate_handoffs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        artifact_type TEXT NOT NULL,
        artifact_ref TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_delegate_sessions_delegate ON delegate_sessions(delegate_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_sessions_work_item ON delegate_sessions(work_item_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_sessions_task ON delegate_sessions(task_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_sessions_status ON delegate_sessions(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_leases_session ON delegate_leases(session_id, heartbeat_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_checkpoints_session ON delegate_checkpoints(session_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_delegate_handoffs_session ON delegate_handoffs(session_id, created_at DESC);
    """

    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path).expanduser()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(self._SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def save_session(self, session: DelegateSession) -> DelegateSession:
        require(bool(session.id), "delegate session id must be non-empty")
        row = (
            session.id,
            session.delegate_id,
            session.work_item_id,
            session.task_id,
            session.workspace_ref,
            session.backend_ref,
            session.machine_ref,
            session.status.value,
            session.active_lease_id,
            session.prior_session_id,
            session.latest_checkpoint_id,
            _iso(session.recovered_at),
            _iso(session.created_at),
            _iso(session.updated_at),
            _json(session.metadata),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delegate_sessions (
                    id, delegate_id, work_item_id, task_id, workspace_ref,
                    backend_ref, machine_ref, status, active_lease_id,
                    prior_session_id, latest_checkpoint_id, recovered_at,
                    created_at, updated_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    delegate_id=excluded.delegate_id,
                    work_item_id=excluded.work_item_id,
                    task_id=excluded.task_id,
                    workspace_ref=excluded.workspace_ref,
                    backend_ref=excluded.backend_ref,
                    machine_ref=excluded.machine_ref,
                    status=excluded.status,
                    active_lease_id=excluded.active_lease_id,
                    prior_session_id=excluded.prior_session_id,
                    latest_checkpoint_id=excluded.latest_checkpoint_id,
                    recovered_at=excluded.recovered_at,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    metadata=excluded.metadata
                """,
                row,
            )
        return session

    def get_session(self, session_id: str) -> DelegateSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegate_sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def list_sessions(
        self,
        *,
        limit: int = 100,
        delegate_id: str | None = None,
        work_item_id: str | None = None,
        task_id: str | None = None,
        status: DelegateSessionStatus | None = None,
    ) -> list[DelegateSession]:
        require(limit >= 1, "limit must be at least 1")
        query = "SELECT * FROM delegate_sessions"
        clauses: list[str] = []
        args: list[object] = []
        if delegate_id is not None:
            clauses.append("delegate_id = ?")
            args.append(delegate_id)
        if work_item_id is not None:
            clauses.append("work_item_id = ?")
            args.append(work_item_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            args.append(task_id)
        if status is not None:
            clauses.append("status = ?")
            args.append(status.value)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        return [_row_to_session(row) for row in rows]

    def save_lease(self, lease: AssignmentLease) -> AssignmentLease:
        if not lease.id:
            lease = lease.model_copy(
                update={"id": _lease_id(lease.session_id, lease.owner_id, lease.heartbeat_at)}
            )
        row = (
            lease.id,
            lease.session_id,
            lease.owner_id,
            _iso(lease.heartbeat_at),
            _iso(lease.expires_at),
            lease.renewal_count,
            lease.recovery_policy.value,
            _iso(lease.released_at),
            _iso(lease.expired_at),
            lease.supersedes_owner_id,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delegate_leases (
                    id, session_id, owner_id, heartbeat_at, expires_at,
                    renewal_count, recovery_policy, released_at, expired_at,
                    supersedes_owner_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id=excluded.session_id,
                    owner_id=excluded.owner_id,
                    heartbeat_at=excluded.heartbeat_at,
                    expires_at=excluded.expires_at,
                    renewal_count=excluded.renewal_count,
                    recovery_policy=excluded.recovery_policy,
                    released_at=excluded.released_at,
                    expired_at=excluded.expired_at,
                    supersedes_owner_id=excluded.supersedes_owner_id
                """,
                row,
            )
        return lease

    def get_lease(self, lease_id: str) -> AssignmentLease | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegate_leases WHERE id = ?",
                (lease_id,),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def current_lease(self, session_id: str) -> AssignmentLease | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM delegate_leases
                WHERE session_id = ? AND released_at IS NULL AND expired_at IS NULL
                ORDER BY heartbeat_at DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def latest_lease(self, session_id: str) -> AssignmentLease | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM delegate_leases
                WHERE session_id = ?
                ORDER BY heartbeat_at DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def list_leases(self, session_id: str) -> list[AssignmentLease]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delegate_leases
                WHERE session_id = ?
                ORDER BY heartbeat_at DESC, id DESC
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_lease(row) for row in rows]

    def save_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        row = (
            checkpoint.id,
            checkpoint.session_id,
            _iso(checkpoint.created_at),
            checkpoint.current_plan,
            _json(list(checkpoint.changed_files)),
            _json(list(checkpoint.test_commands)),
            _json(list(checkpoint.failures_and_learnings)),
            _json(list(checkpoint.artifact_refs)),
            checkpoint.resume_prompt,
            _json(checkpoint.metadata),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delegate_checkpoints (
                    id, session_id, created_at, current_plan, changed_files,
                    test_commands, failures_and_learnings, artifact_refs,
                    resume_prompt, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegate_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
        return _row_to_checkpoint(row) if row is not None else None

    def latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM delegate_checkpoints
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return _row_to_checkpoint(row) if row is not None else None

    def list_checkpoints(self, session_id: str) -> list[Checkpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delegate_checkpoints
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def save_handoff_artifact(self, artifact: HandoffArtifact) -> HandoffArtifact:
        row = (
            artifact.id,
            artifact.session_id,
            artifact.artifact_type,
            artifact.artifact_ref,
            artifact.summary,
            _iso(artifact.created_at),
            _json(artifact.metadata),
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO delegate_handoffs (
                    id, session_id, artifact_type, artifact_ref, summary,
                    created_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
        return artifact

    def list_handoff_artifacts(self, session_id: str) -> list[HandoffArtifact]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM delegate_handoffs
                WHERE session_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (session_id,),
            ).fetchall()
        return [_row_to_handoff_artifact(row) for row in rows]


class DelegateLifecycleService:
    """High-level lifecycle operations backed by a durable session store."""

    def __init__(self, store: DelegateSessionStore, *, clock: Clock | None = None) -> None:
        self._store = store
        self._clock = clock or _utc_now

    def create_session(self, session: DelegateSession) -> DelegateSession:
        require(
            self._store.get_session(session.id) is None,
            f"session {session.id!r} already exists",
        )
        self._store.save_session(session)
        return session

    def get_session(self, session_id: str) -> DelegateSession | None:
        return self._store.get_session(session_id)

    def list_sessions(
        self,
        *,
        limit: int = 100,
        delegate_id: str | None = None,
        work_item_id: str | None = None,
        task_id: str | None = None,
        status: DelegateSessionStatus | None = None,
    ) -> list[DelegateSessionSnapshot]:
        return [
            self.snapshot(session.id)
            for session in self._store.list_sessions(
                limit=limit,
                delegate_id=delegate_id,
                work_item_id=work_item_id,
                task_id=task_id,
                status=status,
            )
        ]

    def snapshot(self, session_id: str) -> DelegateSessionSnapshot:
        session = self._require_session(session_id)
        latest_checkpoint = (
            self._store.get_checkpoint(session.latest_checkpoint_id)
            if session.latest_checkpoint_id is not None
            else self._store.latest_checkpoint(session_id)
        )
        return DelegateSessionSnapshot(
            session=session,
            active_lease=self._store.current_lease(session_id),
            latest_checkpoint=latest_checkpoint,
            handoff_artifacts=tuple(self._store.list_handoff_artifacts(session_id)),
        )

    def acquire_lease(
        self,
        session_id: str,
        *,
        owner_id: str,
        ttl: timedelta,
        recovery_policy: LeaseRecoveryPolicy = LeaseRecoveryPolicy.ABANDON,
    ) -> AssignmentLease:
        session = self._require_session(session_id)
        require(
            self._store.current_lease(session_id) is None,
            f"session {session_id!r} already has an active lease",
        )
        now = self._now()
        lease = AssignmentLease(
            id=_lease_id(session_id, owner_id, now),
            session_id=session_id,
            owner_id=owner_id,
            heartbeat_at=now,
            expires_at=_expires_at(now, ttl),
            recovery_policy=recovery_policy,
        )
        self._store.save_lease(lease)
        leased = session.with_status(DelegateSessionStatus.LEASED, lease=lease, now=now)
        self._store.save_session(leased)
        return lease

    def mark_running(self, session_id: str, *, owner_id: str) -> DelegateSession:
        session = self._require_session(session_id)
        lease = self._require_active_lease(session_id, owner_id=owner_id)
        running = session.with_status(DelegateSessionStatus.RUNNING, lease=lease, now=self._now())
        self._store.save_session(running)
        return running

    def heartbeat(
        self,
        session_id: str,
        *,
        owner_id: str,
        ttl: timedelta,
    ) -> AssignmentLease:
        lease = self._require_active_lease(session_id, owner_id=owner_id)
        now = self._now()
        renewed = lease.model_copy(
            update={
                "heartbeat_at": now,
                "expires_at": _expires_at(now, ttl),
                "renewal_count": lease.renewal_count + 1,
            }
        )
        self._store.save_lease(renewed)
        session = self._require_session(session_id)
        if session.status is DelegateSessionStatus.LEASED:
            session = session.with_status(DelegateSessionStatus.RUNNING, lease=renewed, now=now)
        else:
            session = session.model_copy(update={"active_lease_id": renewed.id, "updated_at": now})
        self._store.save_session(session)
        return renewed

    def record_checkpoint(
        self,
        session_id: str,
        *,
        current_plan: str,
        changed_files: tuple[str, ...] = (),
        test_commands: tuple[str, ...] = (),
        failures_and_learnings: tuple[str, ...] = (),
        artifact_refs: tuple[str, ...] = (),
        resume_prompt: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        session = self._require_session(session_id)
        require(
            session.status
            in {
                DelegateSessionStatus.LEASED,
                DelegateSessionStatus.RUNNING,
                DelegateSessionStatus.PAUSED,
                DelegateSessionStatus.BLOCKED,
            },
            "checkpoint can only be recorded for an active or paused session",
        )
        now = self._now()
        checkpoint = Checkpoint(
            id=f"{session_id}:checkpoint:{len(self._store.list_checkpoints(session_id)) + 1}",
            session_id=session_id,
            created_at=now,
            current_plan=current_plan,
            changed_files=changed_files,
            test_commands=test_commands,
            failures_and_learnings=failures_and_learnings,
            artifact_refs=artifact_refs,
            resume_prompt=resume_prompt,
            metadata=metadata or {},
        )
        self._store.save_checkpoint(checkpoint)
        updated = session.model_copy(
            update={
                "latest_checkpoint_id": checkpoint.id,
                "updated_at": now,
            }
        )
        self._store.save_session(updated)
        return checkpoint

    def record_handoff_artifact(
        self,
        session_id: str,
        *,
        artifact_type: str,
        artifact_ref: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> HandoffArtifact:
        self._require_session(session_id)
        artifact = HandoffArtifact(
            id=f"{session_id}:handoff:{len(self._store.list_handoff_artifacts(session_id)) + 1}",
            session_id=session_id,
            artifact_type=artifact_type,
            artifact_ref=artifact_ref,
            summary=summary,
            metadata=metadata or {},
        )
        self._store.save_handoff_artifact(artifact)
        return artifact

    def expire_session(self, session_id: str) -> DelegateSession:
        session = self._require_session(session_id)
        lease = self._store.current_lease(session_id)
        require(lease is not None, f"session {session_id!r} does not have an active lease")
        lease = cast(AssignmentLease, lease)
        now = self._now()
        next_status = (
            DelegateSessionStatus.ABANDONED
            if lease.recovery_policy is LeaseRecoveryPolicy.ABANDON
            else DelegateSessionStatus.PAUSED
        )
        validate_delegate_session_transition(session.status, next_status)
        expired = lease.model_copy(update={"expired_at": now})
        self._store.save_lease(expired)
        updated = session.model_copy(
            update={
                "status": next_status,
                "active_lease_id": None,
                "updated_at": now,
            }
        )
        self._store.save_session(updated)
        return updated

    def recover_session(
        self,
        prior_session_id: str,
        *,
        new_session_id: str,
        delegate_id: str | None = None,
        owner_id: str,
        machine_ref: str,
        backend_ref: str,
        ttl: timedelta,
    ) -> tuple[DelegateSession, AssignmentLease]:
        prior = self._require_session(prior_session_id)
        require(
            prior.status is DelegateSessionStatus.PAUSED,
            "only a paused delegate session can be recovered",
        )
        latest_checkpoint = self._store.latest_checkpoint(prior_session_id)
        require(
            latest_checkpoint is not None,
            "recovery requires at least one checkpoint",
        )
        latest_checkpoint = cast(Checkpoint, latest_checkpoint)
        now = self._now()
        recovered = DelegateSession.recover_from(
            prior,
            new_session_id=new_session_id,
            delegate_id=delegate_id or prior.delegate_id,
            machine_ref=machine_ref,
            backend_ref=backend_ref,
            latest_checkpoint=latest_checkpoint,
            now=now,
        )
        validate_delegate_session_transition(prior.status, DelegateSessionStatus.SUPERSEDED)
        self._store.save_session(recovered)
        superseded = prior.model_copy(
            update={
                "status": DelegateSessionStatus.SUPERSEDED,
                "updated_at": now,
            }
        )
        self._store.save_session(superseded)
        lease = self.acquire_lease(
            recovered.id,
            owner_id=owner_id,
            ttl=ttl,
            recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
        )
        running = self.mark_running(recovered.id, owner_id=owner_id)
        return running, lease

    def complete_session(self, session_id: str, *, owner_id: str) -> DelegateSession:
        session = self._require_session(session_id)
        lease = self._require_active_lease(session_id, owner_id=owner_id)
        now = self._now()
        validate_delegate_session_transition(session.status, DelegateSessionStatus.COMPLETED)
        released = lease.model_copy(update={"released_at": now})
        self._store.save_lease(released)
        completed = session.model_copy(
            update={
                "status": DelegateSessionStatus.COMPLETED,
                "active_lease_id": None,
                "updated_at": now,
            }
        )
        self._store.save_session(completed)
        return completed

    def _require_session(self, session_id: str) -> DelegateSession:
        session = self._store.get_session(session_id)
        require(session is not None, f"delegate session {session_id!r} not found")
        session = cast(DelegateSession, session)
        return session

    def _require_active_lease(self, session_id: str, *, owner_id: str) -> AssignmentLease:
        lease = self._store.current_lease(session_id)
        require(lease is not None, f"session {session_id!r} does not have an active lease")
        lease = cast(AssignmentLease, lease)
        require(
            lease.owner_id == owner_id,
            "only the current lease owner may update the session",
        )
        return lease

    def _now(self) -> datetime:
        return _normalize_datetime(self._clock())


def _row_to_session(row: sqlite3.Row) -> DelegateSession:
    return DelegateSession(
        id=row["id"],
        delegate_id=row["delegate_id"],
        work_item_id=row["work_item_id"],
        task_id=row["task_id"],
        workspace_ref=row["workspace_ref"],
        backend_ref=row["backend_ref"],
        machine_ref=row["machine_ref"],
        status=DelegateSessionStatus(row["status"]),
        active_lease_id=row["active_lease_id"],
        prior_session_id=row["prior_session_id"],
        latest_checkpoint_id=row["latest_checkpoint_id"],
        recovered_at=_parse_iso(row["recovered_at"]),
        created_at=_parse_iso_required(row["created_at"]),
        updated_at=_parse_iso_required(row["updated_at"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _row_to_lease(row: sqlite3.Row) -> AssignmentLease:
    return AssignmentLease(
        id=row["id"],
        session_id=row["session_id"],
        owner_id=row["owner_id"],
        heartbeat_at=_parse_iso_required(row["heartbeat_at"]),
        expires_at=_parse_iso_required(row["expires_at"]),
        renewal_count=row["renewal_count"],
        recovery_policy=LeaseRecoveryPolicy(row["recovery_policy"]),
        released_at=_parse_iso(row["released_at"]),
        expired_at=_parse_iso(row["expired_at"]),
        supersedes_owner_id=row["supersedes_owner_id"],
    )


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        id=row["id"],
        session_id=row["session_id"],
        created_at=_parse_iso_required(row["created_at"]),
        current_plan=row["current_plan"],
        changed_files=_json_tuple(row["changed_files"]),
        test_commands=_json_tuple(row["test_commands"]),
        failures_and_learnings=_json_tuple(row["failures_and_learnings"]),
        artifact_refs=_json_tuple(row["artifact_refs"]),
        resume_prompt=row["resume_prompt"],
        metadata=json.loads(row["metadata"] or "{}"),
    )


def _row_to_handoff_artifact(row: sqlite3.Row) -> HandoffArtifact:
    return HandoffArtifact(
        id=row["id"],
        session_id=row["session_id"],
        artifact_type=row["artifact_type"],
        artifact_ref=row["artifact_ref"],
        summary=row["summary"],
        created_at=_parse_iso_required(row["created_at"]),
        metadata=json.loads(row["metadata"] or "{}"),
    )
