"""Gate runtime primitives for Maxwell's gauntlet execution model.

The runtime is intentionally small and explicit:

* ``GateDefinition`` captures the contract for one gate.
* ``GateAdapter`` is the execution protocol for a single gate type.
* ``InMemoryGateStore`` preserves every recorded outcome for inspection.
* ``GauntletRuntime`` executes ordered gates deterministically and applies
  blocker, continue-on-failure, and waiver rules in one place.

DbC:
  * gate identifiers, names, and adapter names must be non-empty
  * metadata keys and values must be strings
  * waivers must include both a reviewer and a reason
  * each gauntlet run must use unique gate ids

LOD:
  * adapters only see one gate definition at a time
  * the runtime owns ordering, blocking, and evidence aggregation
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

from maxwell_daemon.contracts import require

__all__ = [
    "GateAdapter",
    "GateAdapterResult",
    "GateDefinition",
    "GateOutcome",
    "GateStore",
    "GateWaiver",
    "GauntletDecision",
    "GauntletRuntime",
    "InMemoryGateStore",
]

GateStatus = Literal["passed", "failed", "waived", "skipped"]


@dataclass(slots=True, frozen=True)
class GateWaiver:
    """A decision to allow a failed gate to proceed."""

    waived_by: str
    reason: str
    issued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        require(bool(self.waived_by.strip()), "GateWaiver.waived_by must be non-empty")
        require(bool(self.reason.strip()), "GateWaiver.reason must be non-empty")


@dataclass(slots=True, frozen=True)
class GateDefinition:
    """One executable gate in a gauntlet."""

    gate_id: str
    name: str
    adapter: str
    required: bool = True
    metadata: Mapping[str, str] = field(default_factory=dict)
    waiver: GateWaiver | None = None

    def __post_init__(self) -> None:
        require(bool(self.gate_id.strip()), "GateDefinition.gate_id must be non-empty")
        require(bool(self.name.strip()), "GateDefinition.name must be non-empty")
        require(bool(self.adapter.strip()), "GateDefinition.adapter must be non-empty")
        for key, value in self.metadata.items():
            require(
                isinstance(key, str) and bool(key.strip()),
                "GateDefinition.metadata keys must be non-empty strings",
            )
            require(
                isinstance(value, str), "GateDefinition.metadata values must be strings"
            )


@dataclass(slots=True, frozen=True)
class GateAdapterResult:
    """The direct outcome returned by a gate adapter."""

    passed: bool
    evidence: tuple[str, ...] = ()
    message: str = ""


@dataclass(slots=True, frozen=True)
class GateOutcome:
    """A recorded runtime outcome for one gate."""

    gate: GateDefinition
    status: GateStatus
    passed: bool
    evidence: tuple[str, ...] = ()
    message: str = ""
    original_status: GateStatus | None = None

    @property
    def gate_id(self) -> str:
        return self.gate.gate_id


@dataclass(slots=True, frozen=True)
class GauntletDecision:
    """Summary of a full gauntlet run."""

    passed: bool
    stopped_early: bool
    executed_gate_ids: tuple[str, ...]
    skipped_gate_ids: tuple[str, ...]
    failed_gate_ids: tuple[str, ...]
    blocking_failed_gate_ids: tuple[str, ...]
    waived_gate_ids: tuple[str, ...]
    evidence: tuple[str, ...]
    outcomes: tuple[GateOutcome, ...]


class GateStore(Protocol):
    """Persistence abstraction for runtime outcomes."""

    def record(self, outcome: GateOutcome) -> None: ...

    def history(self) -> tuple[GateOutcome, ...]: ...


class InMemoryGateStore:
    """Simple store used by the runtime and tests."""

    def __init__(self) -> None:
        self._records: list[GateOutcome] = []

    def record(self, outcome: GateOutcome) -> None:
        self._records.append(outcome)

    def history(self) -> tuple[GateOutcome, ...]:
        return tuple(self._records)

    def clear(self) -> None:
        self._records.clear()


class GateAdapter(Protocol):
    """Execution protocol for a single gate family."""

    async def run(self, gate: GateDefinition) -> GateAdapterResult: ...


class GauntletRuntime:
    """Execute an ordered list of gates against registered adapters."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, GateAdapter],
        store: GateStore | None = None,
        continue_on_failure: bool = False,
    ) -> None:
        self._adapters = dict(adapters)
        self._store = store or InMemoryGateStore()
        self._continue_on_failure = continue_on_failure

    @property
    def store(self) -> GateStore:
        return self._store

    async def run(self, gates: Sequence[GateDefinition]) -> GauntletDecision:
        require(bool(gates), "GauntletRuntime.run: gates must not be empty")
        seen: set[str] = set()
        for gate in gates:
            require(gate.gate_id not in seen, f"duplicate gate_id {gate.gate_id!r}")
            seen.add(gate.gate_id)
            require(
                gate.adapter in self._adapters,
                f"no adapter registered for gate {gate.gate_id!r} ({gate.adapter!r})",
            )

        outcomes: list[GateOutcome] = []
        executed_gate_ids: list[str] = []
        skipped_gate_ids: list[str] = []
        failed_gate_ids: list[str] = []
        blocking_failed_gate_ids: list[str] = []
        waived_gate_ids: list[str] = []
        blocked = False

        for gate in gates:
            if blocked:
                skipped_gate_ids.append(gate.gate_id)
                continue

            adapter = self._adapters[gate.adapter]
            adapter_result = await adapter.run(gate)
            outcome = self._build_outcome(gate, adapter_result)
            self._store.record(outcome)
            outcomes.append(outcome)
            executed_gate_ids.append(gate.gate_id)
            if outcome.status == "failed":
                failed_gate_ids.append(gate.gate_id)
                if gate.required:
                    blocking_failed_gate_ids.append(gate.gate_id)
                if gate.required and not self._continue_on_failure:
                    blocked = True
            elif outcome.status == "waived":
                waived_gate_ids.append(gate.gate_id)

        evidence = tuple(item for outcome in outcomes for item in outcome.evidence)
        passed = not blocking_failed_gate_ids
        return GauntletDecision(
            passed=passed,
            stopped_early=bool(skipped_gate_ids),
            executed_gate_ids=tuple(executed_gate_ids),
            skipped_gate_ids=tuple(skipped_gate_ids),
            failed_gate_ids=tuple(failed_gate_ids),
            blocking_failed_gate_ids=tuple(blocking_failed_gate_ids),
            waived_gate_ids=tuple(waived_gate_ids),
            evidence=evidence,
            outcomes=tuple(outcomes),
        )

    @staticmethod
    def _build_outcome(gate: GateDefinition, result: GateAdapterResult) -> GateOutcome:
        evidence = tuple(result.evidence)
        if result.passed:
            return GateOutcome(
                gate=gate,
                status="passed",
                passed=True,
                evidence=evidence,
                message=result.message,
            )
        if gate.waiver is not None:
            return GateOutcome(
                gate=gate,
                status="waived",
                passed=True,
                evidence=evidence,
                message=result.message,
                original_status="failed",
            )
        return GateOutcome(
            gate=gate,
            status="failed",
            passed=False,
            evidence=evidence,
            message=result.message,
        )
