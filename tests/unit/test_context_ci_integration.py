"""Tests for ContextBuilder + CIProfile integration.

The builder must produce a RepoContext with a populated ``ci_profile`` so
``RepoContext.to_prompt()`` can surface CI requirements to the agent.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from maxwell_daemon.gh.context import ContextBuilder, RepoContext


async def _null_runner(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
    """Empty git output so tests focus on CI detection, not git parsing."""
    if argv[:2] == ("git", "ls-files"):
        return 0, b"", b""
    if argv[:2] == ("git", "log"):
        return 0, b"", b""
    return 0, b"", b""


@pytest.fixture
def builder() -> ContextBuilder:
    return ContextBuilder(git_runner=_null_runner)


def _setup_ruff_repo(root: Path) -> None:
    (root / "pyproject.toml").write_text(dedent("""
            [tool.ruff]
            line-length = 100

            [tool.mypy]
            strict = true

            [tool.pytest.ini_options]
            addopts = "--cov-fail-under=80"
            """).lstrip())


class TestContextBuilderCIProfile:
    async def test_build_populates_ci_profile(
        self, tmp_path: Path, builder: ContextBuilder
    ) -> None:
        _setup_ruff_repo(tmp_path)
        ctx = await builder.build(tmp_path, "some issue")
        assert ctx.ci_profile is not None
        assert ctx.ci_profile.uses_ruff is True
        assert ctx.ci_profile.uses_mypy is True
        assert ctx.ci_profile.mypy_strict is True
        assert ctx.ci_profile.coverage_floor == 80.0

    async def test_build_empty_workspace_yields_empty_profile(
        self, tmp_path: Path, builder: ContextBuilder
    ) -> None:
        ctx = await builder.build(tmp_path, "issue")
        assert ctx.ci_profile is not None
        assert ctx.ci_profile.uses_ruff is False
        assert ctx.ci_profile.uses_mypy is False


class TestRepoContextPromptIncludesCI:
    async def test_prompt_renders_ci_section_when_profile_nontrivial(
        self, tmp_path: Path, builder: ContextBuilder
    ) -> None:
        _setup_ruff_repo(tmp_path)
        ctx = await builder.build(tmp_path, "issue")
        prompt = ctx.to_prompt(max_chars=5000)
        assert "CI requirements" in prompt
        assert "ruff" in prompt
        assert "mypy" in prompt
        assert "80" in prompt

    async def test_prompt_omits_ci_section_when_profile_empty(
        self, tmp_path: Path, builder: ContextBuilder
    ) -> None:
        ctx = await builder.build(tmp_path, "issue")
        prompt = ctx.to_prompt(max_chars=5000)
        assert "CI requirements" not in prompt

    def test_prompt_omits_ci_section_when_profile_none(self) -> None:
        """Backwards compat: if a caller constructs RepoContext manually with
        no ci_profile, to_prompt() must not raise."""
        ctx = RepoContext(language="python")
        prompt = ctx.to_prompt()
        assert "Language: python" in prompt
        assert "CI requirements" not in prompt
