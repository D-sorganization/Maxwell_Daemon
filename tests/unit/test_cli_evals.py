"""CLI tests for ``maxwell-daemon eval`` commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from maxwell_daemon.cli.main import app
from maxwell_daemon.evals.runner import EvalRunner
from maxwell_daemon.evals.storage import EvalRunStore


def test_eval_list_shows_starter_scenarios() -> None:
    result = CliRunner().invoke(app, ["eval", "list"])

    assert result.exit_code == 0
    assert "single-file-bugfix" in result.stdout
    assert "gaai-story-evidence" in result.stdout


def test_eval_run_persists_smoke_suite(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["eval", "run", "--output", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "Eval run" in result.stdout
    assert (tmp_path / _run_id_from_output(result.stdout) / "run.json").is_file()


def test_eval_show_and_report_use_stored_run(tmp_path: Path) -> None:
    run, results = EvalRunner(tmp_path).run(["single-file-bugfix"])
    EvalRunStore(tmp_path).save(run, results)

    show = CliRunner().invoke(app, ["eval", "show", run.id, "--output", str(tmp_path)])
    report = CliRunner().invoke(app, ["eval", "report", run.id, "--output", str(tmp_path)])

    assert show.exit_code == 0, show.stdout
    assert "single-file-bugfix" in show.stdout
    assert report.exit_code == 0, report.stdout
    assert "# Eval Report" in report.stdout


def test_eval_compare_returns_zero_for_unchanged_runs(tmp_path: Path) -> None:
    run_a, results_a = EvalRunner(tmp_path / "a").run(["single-file-bugfix"])
    run_b, results_b = EvalRunner(tmp_path / "b").run(["single-file-bugfix"])
    store = EvalRunStore(tmp_path)
    store.save(run_a, results_a)
    store.save(run_b, results_b)

    result = CliRunner().invoke(
        app,
        ["eval", "compare", run_a.id, run_b.id, "--output", str(tmp_path)],
    )

    assert result.exit_code == 0, result.stdout
    assert "unchanged" in result.stdout


def _run_id_from_output(output: str) -> str:
    for token in output.split():
        if token.startswith("eval-"):
            return token.rstrip(":")
    raise AssertionError(output)
