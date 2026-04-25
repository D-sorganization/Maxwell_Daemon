"""CI-safe fake-agent runner for Maxwell eval scenarios."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import timezone
from pathlib import Path

from maxwell_daemon import __version__
from maxwell_daemon.contracts import ensure, require
from maxwell_daemon.evals.models import (
    EvalResult,
    EvalRun,
    EvalScenario,
    EvalStatus,
    utc_now,
)
from maxwell_daemon.evals.registry import (
    get_scenario,
    get_scoring_profile,
    list_scenarios,
)
from maxwell_daemon.evals.scoring import score_observation


class EvalRunner:
    """Run deterministic smoke scenarios without network, providers, or GitHub writes."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = Path(output_root).expanduser()

    def run(
        self,
        scenario_ids: list[str] | None = None,
        *,
        allow_non_fixture: bool = False,
        approvals: set[str] | None = None,
        preserve_workspaces: bool = False,
    ) -> tuple[EvalRun, list[EvalResult]]:
        scenarios = self._resolve_scenarios(scenario_ids)
        run_id = self._new_run_id()
        run_root = self._output_root / run_id
        run_root.mkdir(parents=True, exist_ok=False)
        run = EvalRun(
            id=run_id,
            scenario_ids=[scenario.id for scenario in scenarios],
            daemon_version=__version__,
            model_profile_ids=["scripted-agent"],
            routing_policy_id="deterministic-smoke",
            external_agent_adapter_ids=["fake-local-agent"],
            artifact_refs=[str(run_root)],
        )

        results = [
            self._run_one(
                scenario,
                run_id=run_id,
                run_root=run_root,
                allow_non_fixture=allow_non_fixture,
                approvals=approvals or set(),
                preserve_workspace=preserve_workspaces,
            )
            for scenario in scenarios
        ]
        failed = [
            result for result in results if result.status is not EvalStatus.PASSED
        ]
        run.completed_at = utc_now().astimezone(timezone.utc)
        run.status = EvalStatus.FAILED if failed else EvalStatus.PASSED
        run.summary = (
            f"{len(results) - len(failed)} passed, {len(failed)} failed across {len(results)} "
            "scenario(s)"
        )
        ensure(
            bool(results), "EvalRunner.run: every run must produce at least one result"
        )
        return run, results

    def _resolve_scenarios(self, scenario_ids: list[str] | None) -> list[EvalScenario]:
        if scenario_ids is None or not scenario_ids:
            return list_scenarios()
        return [get_scenario(scenario_id) for scenario_id in scenario_ids]

    def _run_one(
        self,
        scenario: EvalScenario,
        *,
        run_id: str,
        run_root: Path,
        allow_non_fixture: bool,
        approvals: set[str],
        preserve_workspace: bool,
    ) -> EvalResult:
        if not allow_non_fixture:
            require(
                scenario.fixture_repo_ref.startswith("fixture://"),
                "EvalRunner only runs fixture:// scenarios unless allow_non_fixture is true",
            )
        workspace = self._create_workspace(run_root, scenario)
        artifact_refs = self._write_artifacts(workspace, scenario)
        tests_added = scenario.requires_tests
        if tests_added:
            test_file = workspace / "tests" / f"test_{scenario.id.replace('-', '_')}.py"
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(
                "def test_regression() -> None:\n    assert True\n", encoding="utf-8"
            )

        approval_granted = scenario.id in approvals
        total, breakdown, category = score_observation(
            scenario,
            get_scoring_profile(scenario.scoring_profile_id),
            checks_passed=scenario.required_checks,
            checks_failed=[],
            artifact_refs=artifact_refs,
            unrelated_file_changes=[],
            tests_added=tests_added,
            disallowed_tool_invocations=[],
            approval_granted=approval_granted,
            risky_action_executed=False,
        )
        status = (
            EvalStatus.PASSED
            if total >= 80 and category.value == "none"
            else EvalStatus.FAILED
        )
        result = EvalResult(
            id=f"{run_id}:{scenario.id}",
            eval_run_id=run_id,
            scenario_id=scenario.id,
            status=status,
            score_total=total,
            score_breakdown=breakdown,
            checks_passed=scenario.required_checks,
            diff_summary=f"scripted fixture patch for {scenario.id}",
            trace_id=f"trace-{scenario.id}",
            cost_summary={"usd": 0.0, "tokens": 0.0},
            failure_category=category,
            artifact_refs=artifact_refs,
            approval_required=scenario.requires_approval,
            approval_granted=approval_granted,
            workspace_path=str(workspace) if preserve_workspace else None,
        )
        if not preserve_workspace:
            shutil.rmtree(workspace)
        return result

    def _create_workspace(self, run_root: Path, scenario: EvalScenario) -> Path:
        workspace = run_root / "workspaces" / scenario.id
        workspace.mkdir(parents=True, exist_ok=False)
        (workspace / "TASK.md").write_text(
            scenario.task_prompt + "\n", encoding="utf-8"
        )
        (workspace / "src").mkdir()
        (workspace / "src" / "example.py").write_text(
            "def placeholder() -> bool:\n    return True\n",
            encoding="utf-8",
        )
        return workspace

    def _write_artifacts(self, workspace: Path, scenario: EvalScenario) -> list[str]:
        refs: list[str] = []
        for artifact in scenario.expected_artifacts:
            path = workspace / artifact
            path.parent.mkdir(parents=True, exist_ok=True)
            if artifact.endswith(".json"):
                path.write_text(
                    json.dumps(
                        {
                            "scenario_id": scenario.id,
                            "trace_id": f"trace-{scenario.id}",
                            "acceptance_criteria": scenario.acceptance_criteria,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            else:
                path.write_text(f"artifact for {scenario.id}\n", encoding="utf-8")
            refs.append(artifact)
        return refs

    def _new_run_id(self) -> str:
        return f"eval-{uuid.uuid4().hex[:12]}"
