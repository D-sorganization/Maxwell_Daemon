"""Tests for the Maxwell Gate Runtime and gauntlet execution model."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.core.gates import (
    GateAdapterResult,
    GateDefinition,
    GateOutcome,
    GateWaiver,
    GauntletRuntime,
    InMemoryGateStore,
)


@dataclass
class _ScriptedAdapter:
    results: dict[str, GateAdapterResult]
    calls: list[str] = field(default_factory=list)

    async def run(self, gate: GateDefinition) -> GateAdapterResult:
        self.calls.append(gate.gate_id)
        return self.results[gate.gate_id]


def _gate(
    gate_id: str,
    *,
    required: bool = True,
    waiver: GateWaiver | None = None,
) -> GateDefinition:
    return GateDefinition(
        gate_id=gate_id,
        name=gate_id.replace("-", " ").title(),
        adapter="fake",
        required=required,
        waiver=waiver,
    )


class TestGateDefinitionValidation:
    def test_rejects_blank_identifiers(self) -> None:
        with pytest.raises(PreconditionError, match="gate_id"):
            GateDefinition(gate_id=" ", name="Name", adapter="fake")

    def test_rejects_blank_adapter(self) -> None:
        with pytest.raises(PreconditionError, match="adapter"):
            GateDefinition(gate_id="gate-1", name="Name", adapter=" ")

    def test_rejects_non_string_metadata_values(self) -> None:
        with pytest.raises(PreconditionError, match="metadata values"):
            GateDefinition(
                gate_id="gate-1",
                name="Name",
                adapter="fake",
                metadata=cast(Mapping[str, str], {"attempt": 1}),
            )

    def test_rejects_blank_waiver_fields(self) -> None:
        with pytest.raises(PreconditionError, match="waived_by"):
            GateWaiver(waived_by=" ", reason="approved")


class TestGauntletOrdering:
    async def test_runs_gates_in_defined_order(self) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(True, ("evidence-1",), "one"),
                "gate-2": GateAdapterResult(True, ("evidence-2",), "two"),
                "gate-3": GateAdapterResult(True, ("evidence-3",), "three"),
            }
        )
        store = InMemoryGateStore()
        runtime = GauntletRuntime(adapters={"fake": adapter}, store=store)

        decision = await runtime.run(
            [
                _gate("gate-1"),
                _gate("gate-2"),
                _gate("gate-3"),
            ]
        )

        assert adapter.calls == ["gate-1", "gate-2", "gate-3"]
        assert decision.executed_gate_ids == ("gate-1", "gate-2", "gate-3")
        assert [outcome.gate_id for outcome in store.history()] == [
            "gate-1",
            "gate-2",
            "gate-3",
        ]


class TestBlockerSemantics:
    async def test_required_failure_stops_later_gates_by_default(self) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(False, ("blocker-evidence",), "blocked"),
                "gate-2": GateAdapterResult(True, ("late-evidence",), "late"),
                "gate-3": GateAdapterResult(True, ("later-evidence",), "later"),
            }
        )
        runtime = GauntletRuntime(adapters={"fake": adapter}, store=InMemoryGateStore())

        decision = await runtime.run(
            [
                _gate("gate-1"),
                _gate("gate-2"),
                _gate("gate-3"),
            ]
        )

        assert decision.passed is False
        assert decision.executed_gate_ids == ("gate-1",)
        assert decision.skipped_gate_ids == ("gate-2", "gate-3")
        assert decision.blocking_failed_gate_ids == ("gate-1",)
        assert adapter.calls == ["gate-1"]

    async def test_optional_failure_is_recorded_without_blocking_completion(
        self,
    ) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(
                    False, ("optional-evidence",), "optional failed"
                ),
                "gate-2": GateAdapterResult(
                    True, ("required-evidence",), "required passed"
                ),
            }
        )
        store = InMemoryGateStore()
        runtime = GauntletRuntime(adapters={"fake": adapter}, store=store)

        decision = await runtime.run(
            [
                _gate("gate-1", required=False),
                _gate("gate-2"),
            ]
        )

        assert decision.passed is True
        assert decision.failed_gate_ids == ("gate-1",)
        assert decision.blocking_failed_gate_ids == ()
        assert decision.evidence == ("optional-evidence", "required-evidence")
        assert [outcome.status for outcome in store.history()] == ["failed", "passed"]

    async def test_continue_on_failure_keeps_running_but_final_decision_fails(
        self,
    ) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(False, ("blocker-evidence",), "blocked"),
                "gate-2": GateAdapterResult(True, ("late-evidence",), "late"),
            }
        )
        runtime = GauntletRuntime(
            adapters={"fake": adapter},
            store=InMemoryGateStore(),
            continue_on_failure=True,
        )

        decision = await runtime.run(
            [
                _gate("gate-1"),
                _gate("gate-2"),
            ]
        )

        assert decision.passed is False
        assert decision.executed_gate_ids == ("gate-1", "gate-2")
        assert decision.skipped_gate_ids == ()
        assert decision.blocking_failed_gate_ids == ("gate-1",)


class TestEvidenceAndWaivers:
    async def test_preserves_evidence_for_failed_gate(self) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(
                    False, ("stdout: a", "stderr: b"), "failed"
                ),
            }
        )
        store = InMemoryGateStore()
        runtime = GauntletRuntime(adapters={"fake": adapter}, store=store)

        decision = await runtime.run([_gate("gate-1")])

        assert decision.evidence == ("stdout: a", "stderr: b")
        history = store.history()
        assert history[0].evidence == ("stdout: a", "stderr: b")
        assert history[0].message == "failed"

    async def test_waiver_preserves_original_failed_gate(self) -> None:
        adapter = _ScriptedAdapter(
            results={
                "gate-1": GateAdapterResult(
                    False, ("waived-evidence",), "still failed"
                ),
                "gate-2": GateAdapterResult(True, ("post-waiver",), "passed"),
            }
        )
        store = InMemoryGateStore()
        runtime = GauntletRuntime(adapters={"fake": adapter}, store=store)

        decision = await runtime.run(
            [
                _gate(
                    "gate-1",
                    waiver=GateWaiver(waived_by="reviewer", reason="accepted risk"),
                ),
                _gate("gate-2"),
            ]
        )

        assert decision.passed is True
        assert decision.waived_gate_ids == ("gate-1",)
        assert decision.failed_gate_ids == ()
        outcome = store.history()[0]
        assert outcome.status == "waived"
        assert outcome.original_status == "failed"
        assert outcome.evidence == ("waived-evidence",)
        assert decision.executed_gate_ids == ("gate-1", "gate-2")


class TestStoreContracts:
    def test_in_memory_store_replays_history_in_order(self) -> None:
        store = InMemoryGateStore()
        gate = _gate("gate-1")
        outcome = GateOutcome(
            gate=gate,
            status="passed",
            passed=True,
            evidence=("evidence",),
        )
        store.record(outcome)

        assert store.history() == (outcome,)
