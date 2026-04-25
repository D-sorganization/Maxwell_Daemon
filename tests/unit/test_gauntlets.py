from datetime import datetime, timezone

import pytest

from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.core.gauntlets import (
    GateDecision,
    GateDecisionVerdict,
    GateDefinition,
    GateEvidence,
    GateRun,
    GateRunStatus,
    GauntletRun,
    GauntletStatus,
    InMemoryGauntletStore,
    WaiverRecord,
    complete_gate,
    finalize_gauntlet,
    start_gate,
    waive_gate_failure,
)


def test_gate_definition_rejects_empty_id_and_non_positive_timeout() -> None:
    with pytest.raises(PreconditionError, match="id must be non-empty"):
        GateDefinition(id="", name="Tests")

    with pytest.raises(PreconditionError, match="timeout_seconds must be positive"):
        GateDefinition(id="tests", name="Tests", timeout_seconds=0)


def test_completed_gate_requires_decision() -> None:
    gate = GateDefinition(id="tests", name="Tests")

    with pytest.raises(PreconditionError, match="completed gate requires a decision"):
        GateRun(
            id="gate-run-1",
            gauntlet_run_id="gauntlet-1",
            gate=gate,
            work_item_id="work-1",
            status=GateRunStatus.PASSED,
        )


def test_failed_gate_requires_reason_and_evidence() -> None:
    gate = GateDefinition(id="tests", name="Tests")
    gate_run = start_gate(
        GateRun(
            id="gate-run-1",
            gauntlet_run_id="gauntlet-1",
            gate=gate,
            work_item_id="work-1",
        )
    )
    decision = GateDecision(
        verdict=GateDecisionVerdict.FAIL,
        summary="Tests failed",
        reasons=("unit-test-failure",),
    )

    with pytest.raises(PreconditionError, match="failed gate requires evidence"):
        complete_gate(gate_run, GateRunStatus.FAILED, decision=decision, evidence=())

    evidence = (GateEvidence(id="log-1", kind="log", summary="pytest failure"),)
    with pytest.raises(
        PreconditionError, match="failed gate requires at least one reason"
    ):
        complete_gate(
            gate_run,
            GateRunStatus.FAILED,
            decision=GateDecision(
                verdict=GateDecisionVerdict.FAIL, summary="Tests failed"
            ),
            evidence=evidence,
        )


def test_transition_validation_rejects_invalid_order() -> None:
    gate_run = GateRun(
        id="gate-run-1",
        gauntlet_run_id="gauntlet-1",
        gate=GateDefinition(id="tests", name="Tests"),
        work_item_id="work-1",
    )
    decision = GateDecision(verdict=GateDecisionVerdict.PASS, summary="ok")
    evidence = (GateEvidence(id="log-1", kind="log", summary="pytest passed"),)

    with pytest.raises(ValueError, match="invalid gate transition"):
        complete_gate(
            gate_run, GateRunStatus.PASSED, decision=decision, evidence=evidence
        )


def test_waiver_requires_actor_reason_and_keeps_original_failure_visible() -> None:
    failed = complete_gate(
        start_gate(
            GateRun(
                id="gate-run-1",
                gauntlet_run_id="gauntlet-1",
                gate=GateDefinition(id="tests", name="Tests"),
                work_item_id="work-1",
            )
        ),
        GateRunStatus.FAILED,
        decision=GateDecision(
            verdict=GateDecisionVerdict.FAIL,
            summary="Tests failed",
            reasons=("unit-test-failure",),
        ),
        evidence=(GateEvidence(id="log-1", kind="log", summary="pytest failure"),),
    )

    with pytest.raises(PreconditionError, match="actor must be non-empty"):
        WaiverRecord(
            id="waiver-1", gate_run_id=failed.id, actor="", reason="accepted risk"
        )

    with pytest.raises(PreconditionError, match="reason must be non-empty"):
        WaiverRecord(id="waiver-1", gate_run_id=failed.id, actor="reviewer", reason="")

    waived = waive_gate_failure(
        failed,
        WaiverRecord(
            id="waiver-1", gate_run_id=failed.id, actor="reviewer", reason="accepted"
        ),
    )

    assert waived.status is GateRunStatus.WAIVED
    assert waived.decision is not None
    assert waived.decision.verdict is GateDecisionVerdict.FAIL
    assert waived.waivers[0].original_verdict is GateDecisionVerdict.FAIL
    assert waived.evidence == failed.evidence


def test_required_gate_failure_blocks_passing_gauntlet_without_waiver() -> None:
    failed_required = complete_gate(
        start_gate(
            GateRun(
                id="gate-run-1",
                gauntlet_run_id="gauntlet-1",
                gate=GateDefinition(id="tests", name="Tests", required=True),
                work_item_id="work-1",
            )
        ),
        GateRunStatus.FAILED,
        decision=GateDecision(
            verdict=GateDecisionVerdict.FAIL,
            summary="Tests failed",
            reasons=("unit-test-failure",),
        ),
        evidence=(GateEvidence(id="log-1", kind="log", summary="pytest failure"),),
    )
    gauntlet = GauntletRun(
        id="gauntlet-1",
        work_item_id="work-1",
        gate_runs=(failed_required,),
    )

    final = finalize_gauntlet(gauntlet)

    assert final.status is GauntletStatus.FAILED
    assert final.final_decision is not None
    assert final.final_decision.verdict is GateDecisionVerdict.FAIL
    assert final.failed_gate_ids == ("gate-run-1",)
    assert final.unwaived_required_failed_gate_ids == ("gate-run-1",)

    with pytest.raises(PreconditionError, match="cannot mark gauntlet passed"):
        GauntletRun(
            id="gauntlet-1",
            work_item_id="work-1",
            gate_runs=(failed_required,),
            status=GauntletStatus.PASSED,
            final_decision=GateDecision(verdict=GateDecisionVerdict.PASS, summary="ok"),
        )


def test_waived_required_gate_allows_pass_but_preserves_failure() -> None:
    failed_required = complete_gate(
        start_gate(
            GateRun(
                id="gate-run-1",
                gauntlet_run_id="gauntlet-1",
                gate=GateDefinition(id="tests", name="Tests", required=True),
                work_item_id="work-1",
            )
        ),
        GateRunStatus.FAILED,
        decision=GateDecision(
            verdict=GateDecisionVerdict.FAIL,
            summary="Tests failed",
            reasons=("unit-test-failure",),
        ),
        evidence=(GateEvidence(id="log-1", kind="log", summary="pytest failure"),),
    )
    waived = waive_gate_failure(
        failed_required,
        WaiverRecord(
            id="waiver-1", gate_run_id=failed_required.id, actor="reviewer", reason="ok"
        ),
    )

    final = finalize_gauntlet(
        GauntletRun(id="gauntlet-1", work_item_id="work-1", gate_runs=(waived,))
    )

    assert final.status is GauntletStatus.PASSED_WITH_WAIVERS
    assert final.final_decision is not None
    assert final.final_decision.verdict is GateDecisionVerdict.WAIVED
    assert final.failed_gate_ids == ("gate-run-1",)
    assert final.waived_gate_ids == ("gate-run-1",)
    assert final.unwaived_required_failed_gate_ids == ()


def test_store_records_runs_transitions_waivers_and_lists_by_work_item() -> None:
    store = InMemoryGauntletStore()
    created_at = datetime(2026, 4, 22, tzinfo=timezone.utc)
    gauntlet = GauntletRun(
        id="gauntlet-1", work_item_id="work-1", created_at=created_at
    )

    store.create(gauntlet)
    gate_run = GateRun(
        id="gate-run-1",
        gauntlet_run_id=gauntlet.id,
        gate=GateDefinition(id="tests", name="Tests"),
        work_item_id=gauntlet.work_item_id,
    )
    store.append_gate_run(gauntlet.id, gate_run)
    running = store.transition_gate_run(gate_run.id, GateRunStatus.RUNNING)
    failed = store.transition_gate_run(
        running.id,
        GateRunStatus.FAILED,
        decision=GateDecision(
            verdict=GateDecisionVerdict.FAIL,
            summary="Tests failed",
            reasons=("unit-test-failure",),
        ),
        evidence=(GateEvidence(id="log-1", kind="log", summary="pytest failure"),),
    )
    waiver = WaiverRecord(
        id="waiver-1", gate_run_id=failed.id, actor="reviewer", reason="ok"
    )
    waived = store.record_waiver(failed.id, waiver)
    final = store.finalize(gauntlet.id)

    assert store.get(gauntlet.id) == final
    assert store.list_for_work_item("work-1") == (final,)
    assert store.list_for_work_item("other") == ()
    assert final.gate_runs == (waived,)
    assert final.failed_gate_ids == ("gate-run-1",)
    assert final.waived_gate_ids == ("gate-run-1",)


def test_store_rejects_duplicate_ids_and_preserves_order() -> None:
    store = InMemoryGauntletStore()
    gauntlet = GauntletRun(id="gauntlet-1", work_item_id="work-1")
    store.create(gauntlet)

    first = GateRun(
        id="gate-run-1",
        gauntlet_run_id=gauntlet.id,
        gate=GateDefinition(id="tests", name="Tests"),
        work_item_id="work-1",
    )
    second = GateRun(
        id="gate-run-2",
        gauntlet_run_id=gauntlet.id,
        gate=GateDefinition(id="lint", name="Lint"),
        work_item_id="work-1",
    )

    store.append_gate_run(gauntlet.id, first)
    store.append_gate_run(gauntlet.id, second)

    with pytest.raises(ValueError, match="duplicate gate run id"):
        store.append_gate_run(gauntlet.id, first)

    fetched = store.get(gauntlet.id)
    assert fetched is not None
    assert tuple(run.id for run in fetched.gate_runs) == ("gate-run-1", "gate-run-2")
