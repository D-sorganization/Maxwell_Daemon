"""Regression checks for shipped IDE extension CI coverage."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW_PATH = Path(".github/workflows/ci.yml")
EXTENSIONS_DOC_PATH = Path("docs/development/extensions.md")


def _ci_workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _job(job_id: str) -> dict[str, Any]:
    jobs = _ci_workflow()["jobs"]
    job = jobs[job_id]
    assert isinstance(job, dict)
    return job


def _step_commands(job_id: str) -> str:
    steps = _job(job_id)["steps"]
    assert isinstance(steps, list)
    commands: list[str] = []
    for step in steps:
        if isinstance(step, dict):
            if "working-directory" in step:
                commands.append(str(step["working-directory"]))
            if "run" in step:
                commands.append(str(step["run"]))
            if "uses" in step:
                commands.append(str(step["uses"]))
    return "\n".join(commands)


def test_ci_builds_every_shipped_extension_surface() -> None:
    commands = _step_commands("ide-extensions")

    assert "extensions/vscode" in commands
    assert "npm ci" in commands
    assert "npm run compile" in commands
    assert "gradle -p extensions/jetbrains build --no-daemon" in commands
    assert "cargo check --manifest-path extensions/zed/Cargo.toml --locked" in commands
    assert "node --check extensions/obsidian/main.js" in commands


def test_extension_build_job_uses_local_runner_picker() -> None:
    job = _job("ide-extensions")

    assert job["needs"] == "pick-runner"
    assert job["runs-on"] == "${{ needs.pick-runner.outputs.runner }}"


def test_extension_builds_are_required_by_quality_gate() -> None:
    quality_gate = _job("quality-gate")

    assert "ide-extensions" in quality_gate["needs"]


def test_obsidian_artifact_only_status_is_documented() -> None:
    docs = EXTENSIONS_DOC_PATH.read_text(encoding="utf-8")

    assert "built-artifact-only" in docs
    assert "CI syntax-checks the artifact" in docs


def test_duplicate_vscode_extension_stays_removed() -> None:
    assert not Path("extensions/conductor-vscode").exists()
