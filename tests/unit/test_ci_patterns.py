"""Tests for CIPatternDetector — infers a repo's CI contract from its files.

The goal is to turn "what the workspace has checked in" into a structured
``CIProfile`` the agent can read, so PRs don't come back in the first round
with ruff/mypy/coverage failures.

All tests work on a temporary workspace; no git, no network.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from conductor.gh.ci_patterns import (
    CIPatternDetector,
    CIProfile,
    detect_ci_profile,
)


def _write(workspace: Path, relpath: str, body: str) -> Path:
    p = workspace / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body).lstrip())
    return p


# ── CIProfile shape ──────────────────────────────────────────────────────────


class TestCIProfileDefaults:
    def test_empty_defaults_are_safe(self) -> None:
        p = CIProfile()
        assert p.uses_ruff is False
        assert p.uses_mypy is False
        assert p.uses_black is False
        assert p.uses_pytest is False
        assert p.coverage_floor is None
        assert p.has_precommit is False
        assert p.precommit_hooks == ()
        assert p.workflows == ()

    def test_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        p = CIProfile()
        with pytest.raises(FrozenInstanceError):
            p.uses_ruff = True  # type: ignore[misc]


class TestCIProfilePrompt:
    def test_empty_profile_renders_nothing(self) -> None:
        assert CIProfile().to_prompt() == ""

    def test_ruff_line_present(self) -> None:
        prompt = CIProfile(uses_ruff=True).to_prompt()
        assert "ruff" in prompt.lower()
        assert "ruff check" in prompt or "ruff check ." in prompt

    def test_ruff_version_when_known(self) -> None:
        prompt = CIProfile(uses_ruff=True, ruff_version="0.6.0").to_prompt()
        assert "0.6.0" in prompt

    def test_mypy_and_strict_noted(self) -> None:
        prompt = CIProfile(uses_mypy=True, mypy_strict=True).to_prompt()
        assert "mypy" in prompt.lower()
        assert "strict" in prompt.lower()

    def test_coverage_floor_rendered(self) -> None:
        prompt = CIProfile(uses_pytest=True, coverage_floor=80.0).to_prompt()
        assert "80" in prompt
        assert "coverage" in prompt.lower()

    def test_precommit_hooks_listed(self) -> None:
        prompt = CIProfile(has_precommit=True, precommit_hooks=("ruff", "mypy")).to_prompt()
        assert "pre-commit" in prompt.lower()
        assert "ruff" in prompt
        assert "mypy" in prompt


# ── Ruff detection ───────────────────────────────────────────────────────────


class TestRuffDetection:
    def test_detects_ruff_in_pyproject(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.ruff]
            line-length = 100
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_ruff is True

    def test_absent_when_no_ruff_section(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [project]
            name = "foo"
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_ruff is False

    def test_ruff_version_from_pre_commit(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.ruff]
            line-length = 100
            """,
        )
        _write(
            tmp_path,
            ".pre-commit-config.yaml",
            """
            repos:
              - repo: https://github.com/astral-sh/ruff-pre-commit
                rev: v0.7.4
                hooks:
                  - id: ruff
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_ruff is True
        assert profile.ruff_version == "v0.7.4"


# ── Mypy detection ───────────────────────────────────────────────────────────


class TestMypyDetection:
    def test_detects_mypy_from_pyproject(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.mypy]
            strict = true
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_mypy is True
        assert profile.mypy_strict is True

    def test_detects_mypy_from_ini_file(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "mypy.ini",
            """
            [mypy]
            strict = True
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_mypy is True
        assert profile.mypy_strict is True

    def test_not_strict_when_flag_absent(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.mypy]
            ignore_missing_imports = true
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_mypy is True
        assert profile.mypy_strict is False


# ── Pytest + coverage ────────────────────────────────────────────────────────


class TestPytestDetection:
    def test_detects_pytest_section(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.pytest.ini_options]
            testpaths = ["tests"]
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_pytest is True

    def test_coverage_floor_from_cov_fail_under(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.pytest.ini_options]
            addopts = "--cov=pkg --cov-fail-under=85"
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.uses_pytest is True
        assert profile.coverage_floor == 85.0

    def test_coverage_absent(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.pytest.ini_options]
            testpaths = ["tests"]
            """,
        )
        assert detect_ci_profile(tmp_path).coverage_floor is None


# ── Pre-commit ──────────────────────────────────────────────────────────────


class TestPreCommitDetection:
    def test_detects_precommit_config(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            ".pre-commit-config.yaml",
            """
            repos:
              - repo: https://github.com/astral-sh/ruff-pre-commit
                rev: v0.7.4
                hooks:
                  - id: ruff
                  - id: ruff-format
              - repo: https://github.com/pre-commit/mirrors-mypy
                rev: v1.11.0
                hooks:
                  - id: mypy
            """,
        )
        profile = detect_ci_profile(tmp_path)
        assert profile.has_precommit is True
        assert "ruff" in profile.precommit_hooks
        assert "mypy" in profile.precommit_hooks

    def test_no_precommit_no_hooks(self, tmp_path: Path) -> None:
        profile = detect_ci_profile(tmp_path)
        assert profile.has_precommit is False
        assert profile.precommit_hooks == ()


# ── Workflows ────────────────────────────────────────────────────────────────


class TestWorkflowDetection:
    def test_lists_workflow_basenames(self, tmp_path: Path) -> None:
        _write(tmp_path, ".github/workflows/ci.yml", "name: ci\n")
        _write(tmp_path, ".github/workflows/release.yml", "name: release\n")
        profile = detect_ci_profile(tmp_path)
        assert set(profile.workflows) == {"ci.yml", "release.yml"}

    def test_ignores_non_yaml(self, tmp_path: Path) -> None:
        _write(tmp_path, ".github/workflows/README.md", "docs\n")
        profile = detect_ci_profile(tmp_path)
        assert profile.workflows == ()


# ── Detector API ────────────────────────────────────────────────────────────


class TestDetectorPreconditions:
    def test_rejects_nonexistent_workspace(self, tmp_path: Path) -> None:
        ghost = tmp_path / "nope"
        with pytest.raises(Exception, match="workspace"):
            CIPatternDetector(ghost).detect()

    def test_rejects_file_as_workspace(self, tmp_path: Path) -> None:
        f = tmp_path / "f"
        f.write_text("x")
        with pytest.raises(Exception, match="workspace"):
            CIPatternDetector(f).detect()


class TestDetectorCombined:
    def test_full_config_detected_end_to_end(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "pyproject.toml",
            """
            [tool.ruff]
            line-length = 100

            [tool.mypy]
            strict = true

            [tool.pytest.ini_options]
            addopts = "--cov=pkg --cov-fail-under=80"
            """,
        )
        _write(
            tmp_path,
            ".pre-commit-config.yaml",
            """
            repos:
              - repo: https://github.com/astral-sh/ruff-pre-commit
                rev: v0.7.4
                hooks:
                  - id: ruff
            """,
        )
        _write(tmp_path, ".github/workflows/ci.yml", "name: ci\n")

        profile = detect_ci_profile(tmp_path)

        assert profile.uses_ruff is True
        assert profile.ruff_version == "v0.7.4"
        assert profile.uses_mypy is True
        assert profile.mypy_strict is True
        assert profile.uses_pytest is True
        assert profile.coverage_floor == 80.0
        assert profile.has_precommit is True
        assert "ruff" in profile.precommit_hooks
        assert "ci.yml" in profile.workflows

    def test_nothing_detected_on_empty_workspace(self, tmp_path: Path) -> None:
        profile = detect_ci_profile(tmp_path)
        assert profile == CIProfile()
