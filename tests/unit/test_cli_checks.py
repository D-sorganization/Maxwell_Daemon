"""`maxwell-daemon checks ...` subcommand tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_check(repo: Path) -> None:
    check_dir = repo / ".maxwell" / "checks"
    check_dir.mkdir(parents=True)
    (check_dir / "scope.md").write_text(
        """---
id: scope-drift
name: Scope Drift
severity: required
applies_to:
  globs: ["src/**"]
trigger:
  events: [pull_request]
---
Verify scope drift.
""",
        encoding="utf-8",
    )


def test_checks_list_json(runner: CliRunner, tmp_path: Path) -> None:
    _write_check(tmp_path)

    result = runner.invoke(app, ["checks", "list", "--repo", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["id"] == "scope-drift"


def test_checks_run_reports_matching_result(runner: CliRunner, tmp_path: Path) -> None:
    _write_check(tmp_path)

    result = runner.invoke(
        app,
        [
            "checks",
            "run",
            "--repo",
            str(tmp_path),
            "--changed-file",
            "src/app.py",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["conclusion"] == "pass"


def test_checks_run_skips_non_triggered_event(
    runner: CliRunner, tmp_path: Path
) -> None:
    _write_check(tmp_path)

    result = runner.invoke(
        app,
        [
            "checks",
            "run",
            "--repo",
            str(tmp_path),
            "--event",
            "task_completed",
            "--changed-file",
            "src/app.py",
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["conclusion"] == "skipped"
    assert payload[0]["metadata"]["event"] == "task_completed"
    assert payload[0]["metadata"]["trigger_events"] == ["pull_request"]


def test_checks_run_reports_loader_errors(runner: CliRunner, tmp_path: Path) -> None:
    check_dir = tmp_path / ".maxwell" / "checks"
    check_dir.mkdir(parents=True)
    (check_dir / "bad.md").write_text("no frontmatter", encoding="utf-8")

    result = runner.invoke(app, ["checks", "run", "--repo", str(tmp_path)])

    assert result.exit_code == 1
    assert "frontmatter" in result.stdout
