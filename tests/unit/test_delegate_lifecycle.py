from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.core.delegate_lifecycle import (
    AssignmentLease,
    Checkpoint,
    Delegate,
    DelegateLifecycleManager,
    DelegateLifecycleService,
    DelegateSession,
    DelegateSessionStatus,
    DelegateSessionStore,
    HandoffArtifact,
    LeaseRecoveryPolicy,
    validate_delegate_session_transition,
)


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _delegate() -> Delegate:
    return Delegate(
        id="delegate-1",
        role="implementer",
        capability_tags=("python", "tests"),
        allowed_tools=("pytest", "ruff"),
        max_budget_usd=25.0,
        max_wall_clock_seconds=3600,
    )


def _session(*, status: DelegateSessionStatus = DelegateSessionStatus.QUEUED) -> DelegateSession:
    created_at = datetime(2026, 4, 22, 11, 0, tzinfo=timezone.utc)
    return DelegateSession(
        id="session-1",
        delegate_id="delegate-1",
        work_item_id="issue-395",
        workspace_ref="worktree://issue-395",
        backend_ref="codex-cli",
        machine_ref="worker-a",
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


def test_delegate_session_status_transitions_reject_invalid_jumps() -> None:
    validate_delegate_session_transition(
        DelegateSessionStatus.QUEUED,
        DelegateSessionStatus.LEASED,
    )
    validate_delegate_session_transition(
        DelegateSessionStatus.RUNNING,
        DelegateSessionStatus.BLOCKED,
    )

    with pytest.raises(ValueError, match="invalid delegate session transition"):
        validate_delegate_session_transition(
            DelegateSessionStatus.QUEUED,
            DelegateSessionStatus.COMPLETED,
        )


def test_running_session_requires_active_lease() -> None:
    now = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    active_lease = AssignmentLease(
        session_id="session-1",
        owner_id="worker-a",
        heartbeat_at=now,
        expires_at=now + timedelta(minutes=10),
    )

    leased = _session().with_status(DelegateSessionStatus.LEASED, now=now)
    session = leased.with_status(
        DelegateSessionStatus.RUNNING,
        lease=active_lease,
        now=now,
    )

    assert session.status is DelegateSessionStatus.RUNNING

    with pytest.raises(ValueError, match="running session requires an active lease"):
        leased.with_status(DelegateSessionStatus.RUNNING, now=now)

    with pytest.raises(ValidationError, match="running session requires an active lease"):
        _session(status=DelegateSessionStatus.RUNNING)

    with pytest.raises(ValueError, match="running session requires an active lease"):
        leased.with_status(
            DelegateSessionStatus.RUNNING,
            lease=active_lease,
            now=now + timedelta(minutes=11),
        )


def test_lease_acquire_renew_expire_release_and_takeover_are_deterministic() -> None:
    clock = FrozenClock(datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc))
    manager = DelegateLifecycleManager(clock=clock)
    session = _session()

    lease = manager.acquire_lease(
        session=session,
        owner_id="worker-a",
        ttl=timedelta(minutes=5),
        recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
    )

    assert lease.owner_id == "worker-a"
    assert lease.renewal_count == 0
    assert lease.expires_at == clock() + timedelta(minutes=5)
    assert manager.current_lease("session-1") == lease

    with pytest.raises(ValueError, match="active lease"):
        manager.acquire_lease(session=session, owner_id="worker-b", ttl=timedelta(minutes=5))

    clock.advance(timedelta(minutes=2))
    renewed = manager.renew_lease("session-1", owner_id="worker-a", ttl=timedelta(minutes=10))
    assert renewed.renewal_count == 1
    assert renewed.heartbeat_at == clock()
    assert renewed.expires_at == clock() + timedelta(minutes=10)

    clock.advance(timedelta(minutes=11))
    assert manager.expire_lease("session-1").expired_at == clock()
    assert manager.current_lease("session-1") is None

    takeover = manager.takeover_lease(
        session=session, owner_id="worker-b", ttl=timedelta(minutes=7)
    )
    assert takeover.owner_id == "worker-b"
    assert takeover.supersedes_owner_id == "worker-a"

    manager.release_lease("session-1", owner_id="worker-b")
    assert manager.current_lease("session-1") is None


def test_checkpoints_preserve_evidence_and_require_session_timestamp() -> None:
    timestamp = datetime(2026, 4, 22, 12, 30, tzinfo=timezone.utc)
    checkpoint = Checkpoint(
        id="checkpoint-1",
        session_id="session-1",
        created_at=timestamp,
        current_plan="Add tests first, then implement lifecycle models.",
        changed_files=("maxwell_daemon/core/delegate_lifecycle.py",),
        test_commands=("pytest tests/unit/test_delegate_lifecycle.py",),
        failures_and_learnings=("Initial tests fail before implementation.",),
        artifact_refs=("artifact://diff-1",),
        resume_prompt="Continue from the lifecycle model tests.",
    )

    assert checkpoint.changed_files == ("maxwell_daemon/core/delegate_lifecycle.py",)
    assert checkpoint.test_commands == ("pytest tests/unit/test_delegate_lifecycle.py",)
    assert checkpoint.artifact_refs == ("artifact://diff-1",)

    with pytest.raises(ValidationError):
        Checkpoint(
            id="checkpoint-2",
            session_id="",
            created_at=timestamp,
            current_plan="invalid",
        )


def test_recovered_session_records_prior_session_id_and_latest_checkpoint() -> None:
    clock = FrozenClock(datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc))
    prior = _session(status=DelegateSessionStatus.ABANDONED)
    checkpoint = Checkpoint(
        id="checkpoint-1",
        session_id=prior.id,
        created_at=clock(),
        current_plan="Recover from last checkpoint.",
        changed_files=("tests/unit/test_delegate_lifecycle.py",),
    )

    recovered = DelegateSession.recover_from(
        prior,
        new_session_id="session-2",
        delegate_id="delegate-1",
        machine_ref="worker-b",
        backend_ref="codex-cli",
        latest_checkpoint=checkpoint,
        now=clock(),
    )

    assert recovered.prior_session_id == "session-1"
    assert recovered.latest_checkpoint_id == "checkpoint-1"
    assert recovered.status is DelegateSessionStatus.QUEUED

    with pytest.raises(ValueError, match="recovered session must record the prior session id"):
        DelegateSession(
            id="session-3",
            delegate_id="delegate-1",
            work_item_id="issue-395",
            workspace_ref="worktree://issue-395",
            backend_ref="codex-cli",
            machine_ref="worker-c",
            recovered_at=clock(),
        )


def test_handoff_artifact_is_typed_and_attached_to_session() -> None:
    handoff = HandoffArtifact(
        id="handoff-1",
        session_id="session-1",
        artifact_type="test_report",
        artifact_ref="artifact://test-report-1",
        summary="Unit test evidence for lifecycle behavior.",
        created_at=datetime(2026, 4, 22, 13, 0, tzinfo=timezone.utc),
    )

    assert handoff.artifact_type == "test_report"
    assert handoff.artifact_ref == "artifact://test-report-1"


def test_delegate_requires_positive_limits_and_capabilities() -> None:
    delegate = _delegate()

    assert delegate.capability_tags == ("python", "tests")
    assert delegate.allowed_tools == ("pytest", "ruff")

    with pytest.raises(ValidationError):
        Delegate(
            id="delegate-2",
            role="critic",
            capability_tags=(),
            allowed_tools=("pytest",),
            max_budget_usd=0,
            max_wall_clock_seconds=60,
        )


def test_delegate_session_store_round_trips_sessions_leases_checkpoints_and_handoffs(
    tmp_path: Path,
) -> None:
    store = DelegateSessionStore(tmp_path / "delegate_sessions.db")
    session = _session()
    lease = AssignmentLease(
        session_id=session.id,
        owner_id="worker-a",
        heartbeat_at=datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 4, 22, 12, 5, tzinfo=timezone.utc),
        recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
    )
    checkpoint = Checkpoint(
        id="checkpoint-1",
        session_id=session.id,
        created_at=datetime(2026, 4, 22, 12, 2, tzinfo=timezone.utc),
        current_plan="Keep the lease alive and save a checkpoint.",
        changed_files=("maxwell_daemon/core/delegate_lifecycle.py",),
        test_commands=("pytest tests/unit/test_delegate_lifecycle.py",),
        failures_and_learnings=("Store round-trip should preserve tuples.",),
        artifact_refs=("artifact://patch-1",),
        resume_prompt="Continue from the saved checkpoint.",
    )
    handoff = HandoffArtifact(
        id="handoff-1",
        session_id=session.id,
        artifact_type="test_report",
        artifact_ref="artifact://report-1",
        summary="Lifecycle store preserved the checkpoint evidence.",
        created_at=datetime(2026, 4, 22, 12, 3, tzinfo=timezone.utc),
    )

    store.save_session(session)
    store.save_lease(lease)
    store.save_checkpoint(checkpoint)
    store.save_handoff_artifact(handoff)

    loaded_session = store.get_session(session.id)
    loaded_lease = store.current_lease(session.id)
    loaded_checkpoint = store.latest_checkpoint(session.id)
    loaded_handoffs = store.list_handoff_artifacts(session.id)

    assert loaded_session is not None
    assert loaded_session.id == session.id
    assert loaded_lease is not None
    assert loaded_lease.owner_id == "worker-a"
    assert loaded_checkpoint is not None
    assert loaded_checkpoint.current_plan == checkpoint.current_plan
    assert loaded_checkpoint.changed_files == checkpoint.changed_files
    assert loaded_handoffs == [handoff]


def test_delegate_lifecycle_service_can_start_checkpoint_expire_recover_and_complete(
    tmp_path: Path,
) -> None:
    clock = FrozenClock(datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc))
    service = DelegateLifecycleService(
        DelegateSessionStore(tmp_path / "delegate_sessions.db"),
        clock=clock,
    )
    service.create_session(_session())

    lease = service.acquire_lease(
        "session-1",
        owner_id="worker-a",
        ttl=timedelta(minutes=5),
        recovery_policy=LeaseRecoveryPolicy.RECOVERABLE,
    )
    running = service.mark_running("session-1", owner_id="worker-a")
    checkpoint = service.record_checkpoint(
        "session-1",
        current_plan="Add recovery tests first.",
        changed_files=("tests/unit/test_delegate_lifecycle.py",),
        test_commands=("pytest tests/unit/test_delegate_lifecycle.py",),
        failures_and_learnings=("Service should preserve the latest checkpoint.",),
        artifact_refs=("artifact://patch-2",),
        resume_prompt="Resume from the last checkpoint.",
    )

    assert lease.owner_id == "worker-a"
    assert running.status is DelegateSessionStatus.RUNNING
    assert checkpoint.session_id == "session-1"

    snapshot = service.snapshot("session-1")
    assert snapshot.active_lease is not None
    assert snapshot.latest_checkpoint is not None
    assert snapshot.latest_checkpoint.id == checkpoint.id

    clock.advance(timedelta(minutes=6))
    expired = service.expire_session("session-1")
    assert expired.status is DelegateSessionStatus.PAUSED

    recovered, recovered_lease = service.recover_session(
        "session-1",
        new_session_id="session-2",
        owner_id="worker-b",
        machine_ref="worker-b",
        backend_ref="codex-cli",
        ttl=timedelta(minutes=10),
    )

    assert recovered.id == "session-2"
    assert recovered.status is DelegateSessionStatus.RUNNING
    assert recovered.prior_session_id == "session-1"
    assert recovered.latest_checkpoint_id == checkpoint.id
    assert recovered_lease.owner_id == "worker-b"
    assert service.snapshot("session-2").latest_checkpoint is not None

    completed = service.complete_session("session-2", owner_id="worker-b")
    assert completed.status is DelegateSessionStatus.COMPLETED
    assert service.snapshot("session-2").active_lease is None
