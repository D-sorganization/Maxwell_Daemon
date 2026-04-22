"""CLI coverage for repo-carried memory lifecycle commands."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from maxwell_daemon.cli.main import app


def test_repo_memory_lifecycle_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    propose = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "propose",
            str(repo_root),
            "m1",
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
            "--body",
            "Use pytest tests/unit for local verification.",
            "--source",
            "issue-397",
            "--proposed-by",
            "delegate-1",
            "--reason",
            "validated in CI triage",
        ],
    )

    assert propose.exit_code == 0
    assert "Proposed memory entry m1" in propose.stdout

    accept = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "accept",
            str(repo_root),
            "m1",
            "--reviewer",
            "maintainer",
        ],
    )

    assert accept.exit_code == 0
    assert "accepted" in accept.stdout

    snapshot = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "snapshot",
            str(repo_root),
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
            "--issue-number",
            "397",
        ],
    )

    assert snapshot.exit_code == 0
    assert "Repo memory snapshot" in snapshot.stdout
    assert "Use pytest tests/unit" in snapshot.stdout
