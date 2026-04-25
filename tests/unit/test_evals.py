"""Unit tests for the deterministic Maxwell eval harness."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from maxwell_daemon.evals.models import (
    EvalResult,
    EvalRun,
    EvalScenario,
    EvalSourceType,
    EvalStatus,
    FailureCategory,
)
from maxwell_daemon.evals.registry import (
    DEFAULT_SCORING_PROFILE,
    get_scenario,
    list_scenarios,
)
from maxwell_daemon.evals.reports import compare_runs, render_markdown_report
from maxwell_daemon.evals.runner import EvalRunner
from maxwell_daemon.evals.scoring import score_observation
from maxwell_daemon.evals.storage import EvalRunStore


def test_scenario_validation_rejects_unknown_scoring_profile() -> None:
    with pytest.raises(ValidationError, match="unknown scoring profile"):
        EvalScenario(
            id="bad-profile",
            title="Bad",
            description="Bad profile",
            source_type=EvalSourceType.MANUAL_TASK,
            fixture_repo_ref="fixture://bad",
            task_prompt="do work",
            time_budget_seconds=60,
            scoring_profile_id="missing",
        )


def test_scenario_validation_rejects_non_fixture_refs() -> None:
    with pytest.raises(ValidationError, match="fixture://"):
        EvalScenario(
            id="unsafe",
            title="Unsafe",
            description="Unsafe ref",
            source_type=EvalSourceType.MANUAL_TASK,
            fixture_repo_ref="https://github.com/example/repo",
            task_prompt="do work",
            time_budget_seconds=60,
        )


def test_starter_suite_includes_required_scenario_types() -> None:
    scenarios = list_scenarios()
    source_types = {scenario.source_type for scenario in scenarios}

    assert len(scenarios) >= 3
    assert EvalSourceType.GAAI_STORY in source_types
    assert any(scenario.requires_approval for scenario in scenarios)


def test_runner_creates_ci_safe_results_and_removes_workspace(tmp_path: Path) -> None:
    run, results = EvalRunner(tmp_path).run(["single-file-bugfix"])

    assert run.status is EvalStatus.PASSED
    assert results[0].score_total == 100.0
    assert results[0].trace_id == "trace-single-file-bugfix"
    assert results[0].workspace_path is None
    assert not (tmp_path / run.id / "workspaces" / "single-file-bugfix").exists()


def test_runner_can_preserve_fixture_workspace_for_debugging(tmp_path: Path) -> None:
    _run, results = EvalRunner(tmp_path).run(
        ["single-file-bugfix"],
        preserve_workspaces=True,
    )

    workspace = results[0].workspace_path
    assert workspace is not None
    assert (Path(workspace) / "TASK.md").is_file()


def test_runner_refuses_unknown_scenarios(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        EvalRunner(tmp_path).run(["missing-scenario"])


def test_scoring_penalizes_disallowed_tools() -> None:
    scenario = get_scenario("approval-tool-policy")

    total, _breakdown, category = score_observation(
        scenario,
        DEFAULT_SCORING_PROFILE,
        checks_passed=scenario.required_checks,
        checks_failed=[],
        artifact_refs=scenario.expected_artifacts,
        unrelated_file_changes=[],
        tests_added=False,
        disallowed_tool_invocations=["shell:rm"],
        approval_granted=False,
    )

    assert total < 100.0
    assert category is FailureCategory.TOOL_POLICY


def test_scoring_penalizes_missing_required_tests() -> None:
    scenario = get_scenario("single-file-bugfix")

    total, _breakdown, category = score_observation(
        scenario,
        DEFAULT_SCORING_PROFILE,
        checks_passed=scenario.required_checks,
        checks_failed=[],
        artifact_refs=scenario.expected_artifacts,
        unrelated_file_changes=[],
        tests_added=False,
        disallowed_tool_invocations=[],
        approval_granted=False,
    )

    assert total == 50.0
    assert category is FailureCategory.TESTS


def test_scoring_penalizes_unrelated_changes() -> None:
    scenario = get_scenario("single-file-bugfix")

    total, _breakdown, category = score_observation(
        scenario,
        DEFAULT_SCORING_PROFILE,
        checks_passed=scenario.required_checks,
        checks_failed=[],
        artifact_refs=scenario.expected_artifacts,
        unrelated_file_changes=["README.md"],
        tests_added=True,
        disallowed_tool_invocations=[],
        approval_granted=False,
    )

    assert total == 45.0
    assert category is FailureCategory.IMPLEMENTATION


def test_approval_required_scenario_fails_if_risky_action_executes_without_approval() -> (
    None
):
    scenario = get_scenario("approval-tool-policy")

    total, _breakdown, category = score_observation(
        scenario,
        DEFAULT_SCORING_PROFILE,
        checks_passed=scenario.required_checks,
        checks_failed=[],
        artifact_refs=scenario.expected_artifacts,
        unrelated_file_changes=[],
        tests_added=False,
        disallowed_tool_invocations=[],
        approval_granted=False,
        risky_action_executed=True,
    )

    assert total < 100.0
    assert category is FailureCategory.TOOL_POLICY


def test_storage_round_trips_run_and_results(tmp_path: Path) -> None:
    run, results = EvalRunner(tmp_path / "runs").run(["single-file-bugfix"])
    store = EvalRunStore(tmp_path / "store")
    store.save(run, results)

    loaded_run = store.load_run(run.id)
    loaded_results = store.load_results(run.id)

    assert loaded_run.id == run.id
    assert loaded_results[0].scenario_id == "single-file-bugfix"


def test_markdown_report_includes_score_trace_and_artifacts() -> None:
    run = EvalRun(
        id="eval-test",
        scenario_ids=["single-file-bugfix"],
        daemon_version="test",
        status=EvalStatus.PASSED,
        summary="1 passed",
    )
    result = EvalResult(
        id="eval-test:single-file-bugfix",
        eval_run_id=run.id,
        scenario_id="single-file-bugfix",
        status=EvalStatus.PASSED,
        score_total=100.0,
        score_breakdown={"acceptance": 40.0},
        trace_id="trace-single-file-bugfix",
        artifact_refs=["patch.diff"],
    )

    report = render_markdown_report(run, [result])

    assert "single-file-bugfix" in report
    assert "100.00" in report
    assert "acceptance=40.0" in report
    assert "trace-single-file-bugfix" in report
    assert "patch.diff" in report


def test_compare_identifies_regressions_and_improvements() -> None:
    base_run = EvalRun(id="base", scenario_ids=["a", "b"], daemon_version="test")
    candidate_run = EvalRun(
        id="candidate", scenario_ids=["a", "b"], daemon_version="test"
    )
    base_results = [
        _result("base", "a", 100.0),
        _result("base", "b", 50.0),
    ]
    candidate_results = [
        _result("candidate", "a", 90.0),
        _result("candidate", "b", 80.0),
    ]

    comparison = compare_runs(base_run, base_results, candidate_run, candidate_results)

    assert [item.scenario_id for item in comparison.regressions] == ["a"]
    assert [item.scenario_id for item in comparison.improvements] == ["b"]


def _result(run_id: str, scenario_id: str, score: float) -> EvalResult:
    return EvalResult(
        id=f"{run_id}:{scenario_id}",
        eval_run_id=run_id,
        scenario_id=scenario_id,
        status=EvalStatus.PASSED,
        score_total=score,
    )
