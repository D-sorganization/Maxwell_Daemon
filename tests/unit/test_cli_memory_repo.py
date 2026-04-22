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


def test_repo_memory_listing_and_review_commands(tmp_path: Path) -> None:
    runner = CliRunner()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    empty_list = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "list",
            str(repo_root),
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
        ],
    )

    assert empty_list.exit_code == 0
    assert "No memory entries" in empty_list.stdout

    empty_snapshot = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "snapshot",
            str(repo_root),
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
        ],
    )

    assert empty_snapshot.exit_code == 0
    assert "No memory selected" in empty_snapshot.stdout

    for entry_id, body in (
        ("old", "Use pytest."),
        ("new", "Use pytest tests/unit."),
        ("rejected", "Prefer unrelated old workflow."),
        ("superseded", "Prefer a replaced workflow."),
    ):
        proposed = runner.invoke(
            app,
            [
                "memory",
                "repo",
                "propose",
                str(repo_root),
                entry_id,
                "--repo-id",
                "D-sorganization/Maxwell-Daemon",
                "--body",
                body,
                "--source",
                f"issue-397-{entry_id}",
                "--proposed-by",
                "delegate-1",
                "--reason",
                "validated in CI triage",
                "--scope",
                "issue",
                "--work-item-id",
                "397",
            ],
        )
        assert proposed.exit_code == 0

    proposals = runner.invoke(app, ["memory", "repo", "proposals", str(repo_root)])

    assert proposals.exit_code == 0
    assert "pending" in proposals.stdout
    assert "delegate-1" in proposals.stdout

    invalid_review = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "review",
            str(repo_root),
            "old",
            "--reviewer",
            "maintainer",
            "--status",
            "maybe",
        ],
    )

    assert invalid_review.exit_code == 2
    assert "--status must be accepted" in invalid_review.stdout

    accepted_old = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "accept",
            str(repo_root),
            "old",
            "--reviewer",
            "maintainer",
        ],
    )
    accepted_new = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "review",
            str(repo_root),
            "new",
            "--reviewer",
            "maintainer",
            "--status",
            "accepted",
        ],
    )
    rejected = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "reject",
            str(repo_root),
            "rejected",
            "--reviewer",
            "maintainer",
            "--reason",
            "not durable enough",
        ],
    )

    assert accepted_old.exit_code == 0
    assert accepted_new.exit_code == 0
    assert rejected.exit_code == 0
    assert "rejected" in rejected.stdout

    superseded = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "review",
            str(repo_root),
            "superseded",
            "--reviewer",
            "maintainer",
            "--status",
            "superseded",
        ],
    )
    assert superseded.exit_code == 0
    assert "superseded" in superseded.stdout

    active_list = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "list",
            str(repo_root),
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
        ],
    )

    assert active_list.exit_code == 0
    assert "old" in active_list.stdout
    assert "new" in active_list.stdout
    assert "rejected" not in active_list.stdout
    assert "superseded" not in active_list.stdout


def test_repo_memory_commands_report_invalid_review_and_empty_snapshot(tmp_path: Path) -> None:
    runner = CliRunner()
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    invalid = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "review",
            str(repo_root),
            "missing",
            "--reviewer",
            "critic",
            "--status",
            "paused",
        ],
    )
    assert invalid.exit_code == 2
    assert "--status must be accepted, rejected, or superseded" in invalid.stdout

    snapshot = runner.invoke(
        app,
        [
            "memory",
            "repo",
            "snapshot",
            str(repo_root),
            "--repo-id",
            "D-sorganization/Maxwell-Daemon",
        ],
    )
    assert snapshot.exit_code == 0
    assert "No memory selected" in snapshot.stdout
