"""Tests for per-repo configurable system prompt (issue #151)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.config import MaxwellDaemonConfig, RepoConfig
from maxwell_daemon.core.repo_overrides import RepoOverrides, resolve_overrides
from maxwell_daemon.gh import Issue, PullRequest
from maxwell_daemon.gh.executor import _SYSTEM_PROMPT, IssueExecutor


def _issue(title: str = "Fix the bug", body: str = "details here") -> Issue:
    return Issue(number=42, title=title, body=body, state="OPEN", labels=[], url="https://x/i/42")


@dataclass
class FakeGitHub:
    issue: Issue
    created_pr: PullRequest = field(
        default_factory=lambda: PullRequest(number=1, url="https://x/pull/1", draft=True)
    )

    async def get_issue(self, repo: str, number: int) -> Issue:
        return self.issue

    async def create_pull_request(self, repo: str, **kwargs: Any) -> PullRequest:
        return self.created_pr


@dataclass
class FakeWorkspace:
    async def ensure_clone(self, repo: str, **_: Any) -> Path:
        return Path("/fake/repo")

    async def create_branch(self, *a: Any, **_: Any) -> None:
        pass

    async def apply_diff(self, *a: Any, **_: Any) -> None:
        pass

    async def commit_and_push(self, *a: Any, **_: Any) -> None:
        pass


class CapturingBackend(ILLMBackend):
    name = "capturing"

    def __init__(self) -> None:
        self.system_messages: list[str] = []

    async def complete(
        self, messages: list[Message], *, model: str, **kwargs: Any
    ) -> BackendResponse:
        for m in messages:
            if m.role == MessageRole.SYSTEM:
                self.system_messages.append(m.content)
        return BackendResponse(
            content=json.dumps({"plan": "the plan", "diff": ""}),
            finish_reason="stop",
            usage=TokenUsage(total_tokens=10),
            model=model,
            backend=self.name,
        )

    async def stream(self, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
        if False:
            yield ""

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities()


def _cfg(**kw: Any) -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"c": {"type": "ollama", "model": "m"}},
            "agent": {"default_backend": "c"},
            "repos": [{"name": "my-repo", "path": "/tmp/x", **kw}],
        }
    )


class TestBuildSystemPrompt:
    def test_no_overrides_returns_default(self) -> None:
        assert (
            IssueExecutor._build_system_prompt(
                overrides=None, repo="o/r", issue_title="t", issue_body="b"
            )
            == _SYSTEM_PROMPT
        )

    def test_overrides_without_prompt_fields_returns_default(self) -> None:
        assert (
            IssueExecutor._build_system_prompt(
                overrides=RepoOverrides(), repo="o/r", issue_title="t", issue_body="b"
            )
            == _SYSTEM_PROMPT
        )

    def test_system_prompt_prefix_is_prepended(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="USE PYTHON."),
            repo="o/r",
            issue_title="t",
            issue_body="b",
        )
        assert result.startswith("USE PYTHON.")
        assert _SYSTEM_PROMPT.strip() in result

    def test_prefix_and_default_are_separated(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="PREFIX"),
            repo="o/r",
            issue_title="t",
            issue_body="b",
        )
        assert "PREFIX\n\n" in result

    def test_template_substitution_repo(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="Repo: {repo}"),
            repo="myorg/myrepo",
            issue_title="t",
            issue_body="b",
        )
        assert "Repo: myorg/myrepo" in result

    def test_template_substitution_issue_title(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="Fix: {issue_title}"),
            repo="o/r",
            issue_title="My Bug",
            issue_body="b",
        )
        assert "Fix: My Bug" in result

    def test_template_substitution_issue_body_truncated(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="Body: {issue_body}"),
            repo="o/r",
            issue_title="t",
            issue_body="x" * 1000,
        )
        assert "Body: " + "x" * 500 in result
        assert "x" * 501 not in result

    def test_template_substitution_none_body(self) -> None:
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="Body: {issue_body}"),
            repo="o/r",
            issue_title="t",
            issue_body=None,  # type: ignore[arg-type]
        )
        assert "Body: " in result

    def test_system_prompt_file_is_read(self, tmp_path: Path) -> None:
        f = tmp_path / "p.md"
        f.write_text("File content.", encoding="utf-8")
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_file=f),
            repo="o/r",
            issue_title="t",
            issue_body="b",
        )
        assert result.startswith("File content.")
        assert _SYSTEM_PROMPT.strip() in result

    def test_system_prompt_file_takes_priority_over_prefix(self, tmp_path: Path) -> None:
        f = tmp_path / "p.md"
        f.write_text("From file.", encoding="utf-8")
        result = IssueExecutor._build_system_prompt(
            overrides=RepoOverrides(system_prompt_prefix="From prefix.", system_prompt_file=f),
            repo="o/r",
            issue_title="t",
            issue_body="b",
        )
        assert "From file." in result
        assert "From prefix." not in result

    def test_empty_prefix_string_returns_default(self) -> None:
        assert (
            IssueExecutor._build_system_prompt(
                overrides=RepoOverrides(system_prompt_prefix=""),
                repo="o/r",
                issue_title="t",
                issue_body="b",
            )
            == _SYSTEM_PROMPT
        )


class TestSystemPromptInExecuteIssue:
    def test_no_override_uses_default_prompt(self) -> None:
        backend = CapturingBackend()
        asyncio.run(
            IssueExecutor(
                github=FakeGitHub(issue=_issue()),
                workspace=FakeWorkspace(),
                backend=backend,
            ).execute_issue(repo="o/r", issue_number=42, model="m", mode="plan", overrides=None)
        )
        assert any("JSON" in msg or "json" in msg.lower() for msg in backend.system_messages)

    def test_prefix_override_reaches_backend(self) -> None:
        backend = CapturingBackend()
        asyncio.run(
            IssueExecutor(
                github=FakeGitHub(issue=_issue()),
                workspace=FakeWorkspace(),
                backend=backend,
            ).execute_issue(
                repo="o/r",
                issue_number=42,
                model="m",
                mode="plan",
                overrides=RepoOverrides(system_prompt_prefix="CUSTOM_PREFIX_MARKER"),
            )
        )
        assert any("CUSTOM_PREFIX_MARKER" in msg for msg in backend.system_messages)

    def test_file_override_reaches_backend(self, tmp_path: Path) -> None:
        f = tmp_path / "s.md"
        f.write_text("FILE_PROMPT_MARKER", encoding="utf-8")
        backend = CapturingBackend()
        asyncio.run(
            IssueExecutor(
                github=FakeGitHub(issue=_issue()),
                workspace=FakeWorkspace(),
                backend=backend,
            ).execute_issue(
                repo="o/r",
                issue_number=42,
                model="m",
                mode="plan",
                overrides=RepoOverrides(system_prompt_file=f),
            )
        )
        assert any("FILE_PROMPT_MARKER" in msg for msg in backend.system_messages)

    def test_repo_template_substitution_in_backend_message(self) -> None:
        backend = CapturingBackend()
        asyncio.run(
            IssueExecutor(
                github=FakeGitHub(issue=_issue()),
                workspace=FakeWorkspace(),
                backend=backend,
            ).execute_issue(
                repo="owner/testrepo",
                issue_number=42,
                model="m",
                mode="plan",
                overrides=RepoOverrides(system_prompt_prefix="Repo={repo}"),
            )
        )
        assert any("Repo=owner/testrepo" in msg for msg in backend.system_messages)


class TestRepoOverridesNewFields:
    def test_defaults_are_none(self) -> None:
        ov = RepoOverrides()
        assert ov.system_prompt_prefix is None
        assert ov.system_prompt_file is None

    def test_can_set_prefix(self) -> None:
        assert RepoOverrides(system_prompt_prefix="p").system_prompt_prefix == "p"

    def test_can_set_file(self, tmp_path: Path) -> None:
        p = tmp_path / "f.md"
        assert RepoOverrides(system_prompt_file=p).system_prompt_file == p


class TestRepoConfigSystemPromptFields:
    def test_accepts_system_prompt_prefix(self) -> None:
        rc = RepoConfig.model_validate(
            {"name": "x", "path": "/tmp/x", "system_prompt_prefix": "Use ruff."}
        )
        assert rc.system_prompt_prefix == "Use ruff."

    def test_accepts_system_prompt_file(self, tmp_path: Path) -> None:
        rc = RepoConfig.model_validate(
            {
                "name": "x",
                "path": "/tmp/x",
                "system_prompt_file": str(tmp_path / "p.md"),
            }
        )
        assert rc.system_prompt_file == tmp_path / "p.md"

    def test_defaults_to_none(self) -> None:
        rc = RepoConfig.model_validate({"name": "x", "path": "/tmp/x"})
        assert rc.system_prompt_prefix is None
        assert rc.system_prompt_file is None


class TestResolveOverridesSystemPromptPropagation:
    def test_propagates_prefix(self) -> None:
        assert (
            resolve_overrides(
                _cfg(system_prompt_prefix="Use mypy."), repo="my-repo"
            ).system_prompt_prefix
            == "Use mypy."
        )

    def test_propagates_file(self, tmp_path: Path) -> None:
        p = tmp_path / "p.md"
        assert (
            resolve_overrides(_cfg(system_prompt_file=str(p)), repo="my-repo").system_prompt_file
            == p
        )

    def test_missing_repo_leaves_fields_none(self) -> None:
        ov = resolve_overrides(_cfg(system_prompt_prefix="x"), repo="unknown")
        assert ov.system_prompt_prefix is None
        assert ov.system_prompt_file is None
