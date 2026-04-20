"""Static tests for .github/workflows/conductor-fleet-dispatch.yml.

We can't run GitHub Actions in the unit test suite, but we can pin the
workflow's *shape*: required inputs, required secrets, action→command
mapping. These tests catch drift between the workflow and the CLI
commands it invokes.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "conductor-fleet-dispatch.yml"
)


def _load() -> dict:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


class TestWorkflowExists:
    def test_file_exists(self) -> None:
        assert WORKFLOW_PATH.is_file(), f"missing workflow at {WORKFLOW_PATH}"

    def test_valid_yaml(self) -> None:
        data = _load()
        assert isinstance(data, dict)
        assert data.get("name") == "Conductor Fleet Dispatch"


class TestTriggers:
    def test_workflow_dispatch_present(self) -> None:
        # PyYAML parses the ``on:`` key as True (YAML 1.1 bool) — accept both shapes.
        triggers = _load().get(True) or _load().get("on")
        assert triggers is not None
        assert "workflow_dispatch" in triggers

    def test_schedule_present(self) -> None:
        triggers = _load().get(True) or _load().get("on")
        schedule = triggers.get("schedule") or []
        assert schedule, "schedule must be configured for unattended runs"
        assert any("cron" in entry for entry in schedule)


class TestDispatchInputs:
    def test_action_input_is_choice(self) -> None:
        triggers = _load().get(True) or _load().get("on")
        inputs = triggers["workflow_dispatch"]["inputs"]
        action = inputs["action"]
        assert action["type"] == "choice"
        # Must cover full-run (default autonomous path) plus the discover/deliver split.
        options = set(action["options"])
        assert {"full-run", "discover", "deliver"} <= options

    def test_required_inputs_defined(self) -> None:
        triggers = _load().get(True) or _load().get("on")
        inputs = triggers["workflow_dispatch"]["inputs"]
        for key in ("action", "repos", "label", "max_stories", "dry_run"):
            assert key in inputs, f"missing input: {key}"


class TestJobShape:
    def test_self_hosted_runner(self) -> None:
        data = _load()
        runs_on = data["jobs"]["fleet-dispatch"]["runs-on"]
        # GitHub Actions renders a list literal; accept str ("self-hosted") too.
        joined = " ".join(runs_on) if isinstance(runs_on, list) else str(runs_on)
        assert "self-hosted" in joined
        assert "conductor-fleet" in joined

    def test_secrets_wired_to_env(self) -> None:
        data = _load()
        env = data["jobs"]["fleet-dispatch"]["env"]
        assert "GITHUB_TOKEN" in env
        assert "ANTHROPIC_API_KEY" in env
        assert env["CONDUCTOR_FLEET_CONFIG"].endswith("fleet.yaml")

    def test_concurrency_group_set(self) -> None:
        data = _load()
        conc = data["concurrency"]
        assert conc["group"] == "conductor-fleet-dispatch"
        assert conc["cancel-in-progress"] is False
