"""Tests for ``maxwell-daemon spec`` subcommands.

Thin integration tests that call the Typer CLI with a temp workspace and
assert the on-disk outcome. The heavy lifting is in
:mod:`maxwell_daemon.spec`; here we only verify the CLI plumbing.
"""

from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent

import pytest
from typer.testing import CliRunner

from maxwell_daemon.cli.spec import spec_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _write_feature(tmp_path: Path, body: str, name: str = "login.feature") -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(body).lstrip())
    return path


class TestSpecList:
    def test_empty_directory_message(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(spec_app, ["list", str(tmp_path)])
        assert result.exit_code == 0
        assert "No .feature" in result.output

    def test_lists_features_in_directory(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        _write_feature(
            tmp_path,
            """
            Feature: Login
              Scenario: Happy
                Given x
                When y
                Then z
            """,
            name="login.feature",
        )
        _write_feature(
            tmp_path,
            """
            Feature: Signup
              Scenario: Happy
                Given x
                When y
                Then z
            """,
            name="signup.feature",
        )
        result = runner.invoke(spec_app, ["list", str(tmp_path)])
        assert result.exit_code == 0
        assert "Login" in result.output
        assert "Signup" in result.output


class TestSpecShow:
    def test_shows_scenarios_and_steps(self, runner: CliRunner, tmp_path: Path) -> None:
        feature = _write_feature(
            tmp_path,
            """
            Feature: Login

              Scenario: Success
                Given a user
                When they log in
                Then they see the dashboard
            """,
        )
        result = runner.invoke(spec_app, ["show", str(feature)])
        assert result.exit_code == 0
        assert "Feature:" in result.output
        assert "Success" in result.output
        assert "Given" in result.output and "When" in result.output

    def test_malformed_feature_exits_non_zero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        path = tmp_path / "broken.feature"
        path.write_text("not a feature\n")
        result = runner.invoke(spec_app, ["show", str(path)])
        assert result.exit_code == 1


class TestSpecGenerate:
    def test_writes_scaffold_to_default_path(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        feature = _write_feature(
            tmp_path,
            """
            Feature: Login
              Scenario: Success
                Given a user
                When they log in
                Then they see the dashboard
            """,
        )
        # Run from inside tmp_path so the default target resolves there.
        result = runner.invoke(
            spec_app,
            ["generate", str(feature)],
            catch_exceptions=False,
            # Typer's runner doesn't chdir; invoke generates a relative target
            # which will appear under the current CWD. Use --output to make
            # the test path-robust.
        )
        # Without a chdir we can't assert the relative path; instead assert
        # the scaffold exists at a known absolute path via --output.
        output_path = tmp_path / "out" / "test_login.py"
        result = runner.invoke(
            spec_app, ["generate", str(feature), "--output", str(output_path)]
        )
        assert result.exit_code == 0, result.output
        assert output_path.is_file()
        body = output_path.read_text()
        assert "from pytest_bdd import" in body
        assert "scenarios(" in body
        assert "@given(" in body and "@when(" in body and "@then(" in body

    def test_refuses_overwrite_without_flag(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        feature = _write_feature(
            tmp_path,
            "Feature: X\n  Scenario: a\n    Given x\n    When y\n    Then z\n",
        )
        output_path = tmp_path / "out.py"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("# sentinel — must not be overwritten\n")

        result = runner.invoke(
            spec_app, ["generate", str(feature), "--output", str(output_path)]
        )
        assert result.exit_code == 1
        assert re.search(r"already\s+exists", result.output)
        assert output_path.read_text().startswith("# sentinel")

    def test_overwrite_flag_replaces_existing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        feature = _write_feature(
            tmp_path,
            "Feature: X\n  Scenario: a\n    Given x\n    When y\n    Then z\n",
        )
        output_path = tmp_path / "out.py"
        output_path.write_text("# sentinel\n")

        result = runner.invoke(
            spec_app,
            ["generate", str(feature), "--output", str(output_path), "--overwrite"],
        )
        assert result.exit_code == 0
        assert "sentinel" not in output_path.read_text()
        assert "from pytest_bdd" in output_path.read_text()
