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


def test_ci_pick_runner_stays_lightweight() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    pick_runner = workflow["jobs"]["pick-runner"]

    setup_python_steps = [
        step
        for step in pick_runner["steps"]
        if step.get("uses", "").startswith("actions/setup-python")
    ]

    assert setup_python_steps == []


def test_desktop_smoke_budget_allows_loaded_self_hosted_runners() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    desktop_smoke = workflow["jobs"]["desktop-smoke"]

    smoke_steps = [
        step
        for step in desktop_smoke["steps"]
        if step.get("run", "").endswith("npm run smoke:launch")
    ]

    assert len(smoke_steps) == 2
    for step in smoke_steps:
        assert int(step["env"]["MAXWELL_DESKTOP_LAUNCH_BUDGET_MS"]) >= 180000


def test_anti_phantom_guard_uses_workflow_token_with_comment_scope() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/anti-phantom-merge.yml").read_text(encoding="utf-8")
    )

    assert workflow["permissions"]["pull-requests"] == "read"
    assert workflow["permissions"]["issues"] == "write"

    guard_steps = workflow["jobs"]["guard"]["steps"]
    guard_step = next(step for step in guard_steps if step["name"] == "Anti-phantom guard")

    assert guard_step["env"]["GH_TOKEN"] == "${{ github.token }}"


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
    compatibility_run = compatibility_steps[0]["run"]
    assert "--no-cov" in compatibility_run
    assert "tests/unit/test_auth.py" in compatibility_run
    assert "tests/unit/test_auth_optional_pyjwt.py" in compatibility_run
    assert "tests/unit/test_api_websocket_auth.py" in compatibility_run
    assert "tests/unit/test_roles.py" in compatibility_run
    assert "tests/integration/test_serve_jwt_wiring.py" in compatibility_run
    assert "tests/unit/test_ci_timeout_contract.py" in compatibility_run
    assert ".venv/bin/python -m pytest -p no:xvfb --timeout=300 --no-cov" not in compatibility_run


def test_secret_scan_uses_event_ranges_instead_of_full_history() -> None:
    workflow = yaml.safe_load(Path(".github/workflows/ci.yml").read_text(encoding="utf-8"))
    secret_scan = workflow["jobs"]["secret-scan"]
    run_step = next(step for step in secret_scan["steps"] if step.get("name") == "Run gitleaks")
    script = run_step["run"]

    assert "${{ github.event.pull_request.base.sha }}" in script
    assert "${{ github.event.pull_request.head.sha }}" in script
    assert "${{ github.event.before }}" in script
    assert "${{ github.sha }}" in script
    assert '--log-opts="${base}..${head}"' in script
    assert "gitleaks git --redact --verbose ." not in script
