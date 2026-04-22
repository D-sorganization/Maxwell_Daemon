"""Provider-agnostic delegate lifecycle models and lease mechanics."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
