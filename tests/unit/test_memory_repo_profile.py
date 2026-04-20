"""RepoProfile — durable facts about a repo the agent has learned."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.memory import RepoProfile


@pytest.fixture
def profile(tmp_path: Path) -> RepoProfile:
    return RepoProfile(tmp_path / "memory.db")


class TestLearnAndRecall:
    def test_empty_profile(self, profile: RepoProfile) -> None:
        assert profile.facts("o/r") == {}

    def test_learn_then_recall(self, profile: RepoProfile) -> None:
        profile.learn("o/r", "language", "python")
        profile.learn("o/r", "test_runner", "pytest")
        assert profile.facts("o/r") == {"language": "python", "test_runner": "pytest"}

    def test_overwrite(self, profile: RepoProfile) -> None:
        profile.learn("o/r", "style", "snake_case")
        profile.learn("o/r", "style", "snake_case, line 100")
        assert profile.facts("o/r")["style"] == "snake_case, line 100"

    def test_isolated_per_repo(self, profile: RepoProfile) -> None:
        profile.learn("a/x", "lang", "python")
        profile.learn("b/y", "lang", "rust")
        assert profile.facts("a/x")["lang"] == "python"
        assert profile.facts("b/y")["lang"] == "rust"

    def test_forget(self, profile: RepoProfile) -> None:
        profile.learn("o/r", "k", "v")
        assert profile.forget("o/r", "k") is True
        assert profile.facts("o/r") == {}

    def test_forget_missing_returns_false(self, profile: RepoProfile) -> None:
        assert profile.forget("o/r", "ghost") is False

    def test_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "memory.db"
        p1 = RepoProfile(path)
        p1.learn("o/r", "lang", "python")
        p2 = RepoProfile(path)
        assert p2.facts("o/r")["lang"] == "python"


class TestRender:
    def test_renders_as_markdown_bullets(self, profile: RepoProfile) -> None:
        profile.learn("o/r", "language", "python")
        profile.learn("o/r", "test_runner", "pytest")
        rendered = profile.render("o/r")
        assert "- language: python" in rendered
        assert "- test_runner: pytest" in rendered

    def test_empty_repo_renders_empty(self, profile: RepoProfile) -> None:
        assert profile.render("o/r") == ""

    def test_render_respects_max_chars(self, profile: RepoProfile) -> None:
        profile.learn("o/r", "big", "x" * 5000)
        rendered = profile.render("o/r", max_chars=500)
        assert len(rendered) <= 600


class TestContractEnforcement:
    def test_rejects_empty_repo(self, profile: RepoProfile) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError):
            profile.learn("", "k", "v")

    def test_rejects_empty_key(self, profile: RepoProfile) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError):
            profile.learn("o/r", "", "v")
