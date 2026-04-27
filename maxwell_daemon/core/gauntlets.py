"""Gauntlet run models and in-memory persistence.

This module is the storage/model slice for Maxwell's gate gauntlet runtime. It
keeps transition validation and fail-closed invariants close to the domain
objects while leaving API, CLI, and tool-specific gate execution for later
integration slices.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from maxwell_daemon.contracts import require

__all__ = [
    "GateDecision",
    "GateDecisionVerdict",
    "GateDefinition",
    "GateEvidence",
    "GateRun",
    "GateRunStatus",
    "GauntletRun",
    "GauntletStatus",
    "GauntletStore",
    "InMemoryGauntletStore",
    "WaiverRecord",
    "complete_gate",
    "finalize_gauntlet",
    "start_gate",
    "validate_gate_transition",
    "waive_gate_failure",
]


class GateRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAIVED = "waived"
    ERROR = "error"


class GateDecisionVerdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_HUMAN = "needs_human"
    WAIVED = "waived"


class GauntletStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    PASSED_WITH_WAIVERS = "passed_with_waivers"
    FAILED = "failed"


_COMPLETED_GATE_STATUSES = frozenset(
    {
        GateRunStatus.PASSED,
        GateRunStatus.FAILED,
        GateRunStatus.WAIVED,
        GateRunStatus.ERROR,
    }
)
_COMPLETED_GAUNTLET_STATUSES = frozenset(
    {
        GauntletStatus.PASSED,
        GauntletStatus.PASSED_WITH_WAIVERS,
        GauntletStatus.FAILED,
    }
)
_ALLOWED_GATE_TRANSITIONS: dict[GateRunStatus, frozenset[GateRunStatus]] = {
    GateRunStatus.PENDING: frozenset({GateRunStatus.RUNNING, GateRunStatus.BLOCKED}),
    GateRunStatus.RUNNING: frozenset(
        {
            GateRunStatus.PASSED,
            GateRunStatus.FAILED,
            GateRunStatus.BLOCKED,
            GateRunStatus.ERROR,
        }
    ),
    GateRunStatus.PASSED: frozenset(),
    GateRunStatus.FAILED: frozenset({GateRunStatus.WAIVED}),
    GateRunStatus.BLOCKED: frozenset(),
    GateRunStatus.WAIVED: frozenset(),
    GateRunStatus.ERROR: frozenset(),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_non_empty(value: str, label: str) -> None:
    require(bool(value.strip()), f"{label} must be non-empty")


@dataclass(slots=True, frozen=True)
class GateDefinition:
    """Declarative contract for one gate within a gauntlet."""

    id: str
    name: str
    required: bool = True
    timeout_seconds: int | None = None
    retry_limit: int = 0
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "GateDefinition.id")
        _require_non_empty(self.name, "GateDefinition.name")
        if self.timeout_seconds is not None:
            require(
                self.timeout_seconds > 0,
                "GateDefinition.timeout_seconds must be positive",
            )
        require(self.retry_limit >= 0, "GateDefinition.retry_limit must be non-negative")
        for key, value in self.metadata.items():
            require(
                isinstance(key, str) and bool(key.strip()),
                "GateDefinition.metadata keys must be non-empty strings",
            )
            require(isinstance(value, str), "GateDefinition.metadata values must be strings")


@dataclass(slots=True, frozen=True)
class GateEvidence:
    """Evidence pointer produced by a gate run."""

    id: str
    kind: str
    summary: str
    uri: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "GateEvidence.id")
        _require_non_empty(self.kind, "GateEvidence.kind")
        _require_non_empty(self.summary, "GateEvidence.summary")
        if self.uri is not None:
            _require_non_empty(self.uri, "GateEvidence.uri")


@dataclass(slots=True, frozen=True)
class GateDecision:
    """Decision emitted by a gate or the final gauntlet rollup."""

    verdict: GateDecisionVerdict
    summary: str
    reasons: tuple[str, ...] = ()
    blocking_findings: tuple[str, ...] = ()
    recommended_next_action: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.summary, "GateDecision.summary")
        for reason in self.reasons:
            _require_non_empty(reason, "GateDecision.reasons entries")
        for finding in self.blocking_findings:
            _require_non_empty(finding, "GateDecision.blocking_findings entries")
        if self.recommended_next_action is not None:
            _require_non_empty(
                self.recommended_next_action,
                "GateDecision.recommended_next_action",
            )


@dataclass(slots=True, frozen=True)
class WaiverRecord:
    """Explicit exception record for a failed gate."""

    id: str
    gate_run_id: str
    actor: str
    reason: str
    original_verdict: GateDecisionVerdict = GateDecisionVerdict.FAIL
    created_at: datetime = field(default_factory=_utc_now)

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "WaiverRecord.id")
        _require_non_empty(self.gate_run_id, "WaiverRecord.gate_run_id")
        _require_non_empty(self.actor, "WaiverRecord.actor")
        _require_non_empty(self.reason, "WaiverRecord.reason")
        require(
            self.original_verdict is GateDecisionVerdict.FAIL,
            "WaiverRecord.original_verdict must preserve a failed decision",
        )


@dataclass(slots=True, frozen=True)
class GateRun:
    """State for one gate execution within a gauntlet run."""

    id: str
    gauntlet_run_id: str
    gate: GateDefinition
    work_item_id: str
    status: GateRunStatus = GateRunStatus.PENDING
    decision: GateDecision | None = None
    evidence: tuple[GateEvidence, ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    attempt: int = 1
    task_id: str | None = None
    action_id: str | None = None
    pr_number: int | None = None
    waivers: tuple[WaiverRecord, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "GateRun.id")
        _require_non_empty(self.gauntlet_run_id, "GateRun.gauntlet_run_id")
        _require_non_empty(self.work_item_id, "GateRun.work_item_id")
        require(self.attempt > 0, "GateRun.attempt must be positive")
        if self.task_id is not None:
            _require_non_empty(self.task_id, "GateRun.task_id")
        if self.action_id is not None:
            _require_non_empty(self.action_id, "GateRun.action_id")
        if self.pr_number is not None:
            require(self.pr_number > 0, "GateRun.pr_number must be positive")
        if self.status in _COMPLETED_GATE_STATUSES:
            require(self.decision is not None, "completed gate requires a decision")
            require(self.completed_at is not None, "completed gate requires completed_at")
        if self.status is GateRunStatus.RUNNING:
            require(self.started_at is not None, "running gate requires started_at")
        if self.status is GateRunStatus.FAILED:
            require(bool(self.evidence), "failed gate requires evidence")
            require(
                self.decision is not None and bool(self.decision.reasons),
                "failed gate requires at least one reason",
            )
            require(
                self.decision is not None and self.decision.verdict is GateDecisionVerdict.FAIL,
                "failed gate requires a fail decision",
            )
        if self.status is GateRunStatus.WAIVED:
            require(bool(self.waivers), "waived gate requires a waiver record")
            require(
                self.decision is not None and self.decision.verdict is GateDecisionVerdict.FAIL,
                "waived gate must keep the original failed decision visible",
            )
        for waiver in self.waivers:
            require(
                waiver.gate_run_id == self.id,
                "waiver gate_run_id must match the waived gate run",
            )


@dataclass(slots=True, frozen=True)
class GauntletRun:
    """Ordered collection of gate runs and their final decision."""

    id: str
    work_item_id: str
    gate_runs: tuple[GateRun, ...] = ()
    status: GauntletStatus = GauntletStatus.PENDING
    final_decision: GateDecision | None = None
    created_at: datetime = field(default_factory=_utc_now)
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.id, "GauntletRun.id")
        _require_non_empty(self.work_item_id, "GauntletRun.work_item_id")
        seen_gate_run_ids: set[str] = set()
        seen_gate_ids: set[str] = set()
        for gate_run in self.gate_runs:
            require(
                gate_run.gauntlet_run_id == self.id,
                "gate run gauntlet_run_id must match the gauntlet",
            )
            require(
                gate_run.work_item_id == self.work_item_id,
                "gate run work_item_id must match the gauntlet",
            )
            require(gate_run.id not in seen_gate_run_ids, "duplicate gate run id")
            require(gate_run.gate.id not in seen_gate_ids, "duplicate gate id")
            seen_gate_run_ids.add(gate_run.id)
            seen_gate_ids.add(gate_run.gate.id)
        if self.status in {GauntletStatus.PASSED, GauntletStatus.PASSED_WITH_WAIVERS}:
            require(
                not self.unwaived_required_failed_gate_ids,
                "cannot mark gauntlet passed with unwaived required gate failures",
            )
        if self.status in _COMPLETED_GAUNTLET_STATUSES:
            require(
                self.final_decision is not None,
                "completed gauntlet requires a final decision",
            )
            require(
                self.completed_at is not None,
                "completed gauntlet requires completed_at",
            )

    @property
    def failed_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            gate_run.id
            for gate_run in self.gate_runs
            if gate_run.status in {GateRunStatus.FAILED, GateRunStatus.WAIVED}
        )

    @property
    def waived_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            gate_run.id for gate_run in self.gate_runs if gate_run.status is GateRunStatus.WAIVED
        )

    @property
    def unwaived_required_failed_gate_ids(self) -> tuple[str, ...]:
        return tuple(
            gate_run.id
            for gate_run in self.gate_runs
            if gate_run.gate.required and gate_run.status is GateRunStatus.FAILED
        )


class GauntletStore(Protocol):
    """Persistence abstraction for gauntlet state."""

    def create(self, run: GauntletRun) -> None: ...

    def get(self, gauntlet_run_id: str) -> GauntletRun | None: ...

    def list_for_work_item(self, work_item_id: str) -> tuple[GauntletRun, ...]: ...

    def append_gate_run(self, gauntlet_run_id: str, gate_run: GateRun) -> GauntletRun: ...

    def transition_gate_run(
        self,
        gate_run_id: str,
        status: GateRunStatus,
        *,
        decision: GateDecision | None = None,
        evidence: tuple[GateEvidence, ...] = (),
    ) -> GateRun: ...

    def record_waiver(self, gate_run_id: str, waiver: WaiverRecord) -> GateRun: ...

    def finalize(self, gauntlet_run_id: str) -> GauntletRun: ...


class InMemoryGauntletStore:
    """Deterministic in-memory gauntlet store for tests and early integrations."""

    def __init__(self) -> None:
        self._runs: dict[str, GauntletRun] = {}
        self._gate_index: dict[str, str] = {}

    def create(self, run: GauntletRun) -> None:
        require(run.id not in self._runs, f"gauntlet run {run.id!r} already exists")
        self._runs[run.id] = run
        for gate_run in run.gate_runs:
            self._index_gate_run(run.id, gate_run)

    def get(self, gauntlet_run_id: str) -> GauntletRun | None:
        return self._runs.get(gauntlet_run_id)

    def list_for_work_item(self, work_item_id: str) -> tuple[GauntletRun, ...]:
        _require_non_empty(work_item_id, "work_item_id")
        return tuple(run for run in self._runs.values() if run.work_item_id == work_item_id)

    def append_gate_run(self, gauntlet_run_id: str, gate_run: GateRun) -> GauntletRun:
        run = self._require_run(gauntlet_run_id)
        if gate_run.id in self._gate_index:
            raise ValueError("duplicate gate run id")
        updated = replace(run, gate_runs=(*run.gate_runs, gate_run))
        self._runs[gauntlet_run_id] = updated
        self._index_gate_run(gauntlet_run_id, gate_run)
        return updated

    def transition_gate_run(
        self,
        gate_run_id: str,
        status: GateRunStatus,
        *,
        decision: GateDecision | None = None,
        evidence: tuple[GateEvidence, ...] = (),
    ) -> GateRun:
        run, gate_run = self._require_gate_run(gate_run_id)
        if status is GateRunStatus.RUNNING:
            updated_gate = start_gate(gate_run)
        else:
            updated_gate = complete_gate(gate_run, status, decision=decision, evidence=evidence)
        self._replace_gate_run(run.id, updated_gate)
        return updated_gate

    def record_waiver(self, gate_run_id: str, waiver: WaiverRecord) -> GateRun:
        run, gate_run = self._require_gate_run(gate_run_id)
        updated_gate = waive_gate_failure(gate_run, waiver)
        self._replace_gate_run(run.id, updated_gate)
        return updated_gate

    def finalize(self, gauntlet_run_id: str) -> GauntletRun:
        run = self._require_run(gauntlet_run_id)
        updated = finalize_gauntlet(run)
        self._runs[gauntlet_run_id] = updated
        return updated

    def _require_run(self, gauntlet_run_id: str) -> GauntletRun:
        _require_non_empty(gauntlet_run_id, "gauntlet_run_id")
        try:
            return self._runs[gauntlet_run_id]
        except KeyError as exc:
            raise KeyError(gauntlet_run_id) from exc

    def _require_gate_run(self, gate_run_id: str) -> tuple[GauntletRun, GateRun]:
        _require_non_empty(gate_run_id, "gate_run_id")
        try:
            gauntlet_run_id = self._gate_index[gate_run_id]
        except KeyError as exc:
            raise KeyError(gate_run_id) from exc
        run = self._require_run(gauntlet_run_id)
        for gate_run in run.gate_runs:
            if gate_run.id == gate_run_id:
                return run, gate_run
        raise KeyError(gate_run_id)

    def _replace_gate_run(self, gauntlet_run_id: str, updated_gate: GateRun) -> None:
        run = self._require_run(gauntlet_run_id)
        gate_runs = tuple(
            updated_gate if gate_run.id == updated_gate.id else gate_run
            for gate_run in run.gate_runs
        )
        self._runs[gauntlet_run_id] = replace(run, gate_runs=gate_runs)

    def _index_gate_run(self, gauntlet_run_id: str, gate_run: GateRun) -> None:
        self._gate_index[gate_run.id] = gauntlet_run_id


def validate_gate_transition(current: GateRunStatus, target: GateRunStatus) -> None:
    if target not in _ALLOWED_GATE_TRANSITIONS[current]:
        raise ValueError(f"invalid gate transition: {current.value} -> {target.value}")


def start_gate(gate_run: GateRun, *, now: datetime | None = None) -> GateRun:
    validate_gate_transition(gate_run.status, GateRunStatus.RUNNING)
    timestamp = now or _utc_now()
    return replace(gate_run, status=GateRunStatus.RUNNING, started_at=timestamp)


def complete_gate(
    gate_run: GateRun,
    status: GateRunStatus,
    *,
    decision: GateDecision | None,
    evidence: tuple[GateEvidence, ...],
    now: datetime | None = None,
) -> GateRun:
    require(status in _COMPLETED_GATE_STATUSES, "complete_gate requires a completed status")
    require(status is not GateRunStatus.WAIVED, "use waive_gate_failure to record waivers")
    validate_gate_transition(gate_run.status, status)
    timestamp = now or _utc_now()
    return replace(
        gate_run,
        status=status,
        decision=decision,
        evidence=evidence,
        completed_at=timestamp,
    )


def waive_gate_failure(
    gate_run: GateRun,
    waiver: WaiverRecord,
    *,
    now: datetime | None = None,
) -> GateRun:
    validate_gate_transition(gate_run.status, GateRunStatus.WAIVED)
    require(waiver.gate_run_id == gate_run.id, "waiver gate_run_id must match gate run")
    require(
        gate_run.decision is not None and gate_run.decision.verdict is GateDecisionVerdict.FAIL,
        "waiver requires an original failed gate decision",
    )
    return replace(
        gate_run,
        status=GateRunStatus.WAIVED,
        completed_at=now or gate_run.completed_at or _utc_now(),
        waivers=(*gate_run.waivers, waiver),
    )


def finalize_gauntlet(gauntlet: GauntletRun, *, now: datetime | None = None) -> GauntletRun:
    timestamp = now or _utc_now()
    unwaived_required = gauntlet.unwaived_required_failed_gate_ids
    if unwaived_required:
        return replace(
            gauntlet,
            status=GauntletStatus.FAILED,
            final_decision=GateDecision(
                verdict=GateDecisionVerdict.FAIL,
                summary="One or more required gates failed without waiver.",
                reasons=tuple(f"required-gate-failed:{gate_id}" for gate_id in unwaived_required),
                recommended_next_action="fix",
            ),
            completed_at=timestamp,
        )
    if gauntlet.waived_gate_ids:
        return replace(
            gauntlet,
            status=GauntletStatus.PASSED_WITH_WAIVERS,
            final_decision=GateDecision(
                verdict=GateDecisionVerdict.WAIVED,
                summary="All required gate failures were explicitly waived.",
                reasons=tuple(f"gate-waived:{gate_id}" for gate_id in gauntlet.waived_gate_ids),
                recommended_next_action="escalate",
            ),
            completed_at=timestamp,
        )
    return replace(
        gauntlet,
        status=GauntletStatus.PASSED,
        final_decision=GateDecision(
            verdict=GateDecisionVerdict.PASS,
            summary="All required gates passed.",
            recommended_next_action="merge",
        ),
        completed_at=timestamp,
    )
