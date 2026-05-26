from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def _load_pyproject() -> dict[str, Any]:
    return tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))


def test_pytest_timeout_plugin_declared_when_timeout_configured() -> None:
    pyproject = _load_pyproject()
    pytest_config = pyproject["tool"]["pytest"]["ini_options"]
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert pytest_config["timeout"] > 0
    assert pytest_config["timeout_method"] == "thread"
    assert any(dep.lower().startswith("pytest-timeout") for dep in dev_dependencies)


def test_ci_test_matrix_has_job_timeout() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]

    assert test_job["timeout-minutes"] == 45


def test_ci_test_matrix_targets_desktop_linux_runners() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]

    assert test_job["runs-on"] == ["self-hosted", "Linux", "X64", "d-sorg-fleet"]


def test_ci_compatibility_lanes_do_not_collect_coverage() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    test_job = workflow["jobs"]["test"]
    steps = test_job["steps"]

    coverage_steps = [step for step in steps if step.get("name") == "Run tests with coverage"]
    compatibility_steps = [step for step in steps if step.get("name") == "Run compatibility tests"]

    assert len(coverage_steps) == 1
    assert coverage_steps[0]["if"] == "matrix.python-version == '3.12'"
    assert "--cov=maxwell_daemon" in coverage_steps[0]["run"]

    assert len(compatibility_steps) == 1
    assert compatibility_steps[0]["if"] == "matrix.python-version != '3.12'"
    assert "--no-cov" in compatibility_steps[0]["run"]
