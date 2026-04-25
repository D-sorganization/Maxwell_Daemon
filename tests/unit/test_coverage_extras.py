"""Extra coverage tests for issue #151."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.config import RepoConfig
from maxwell_daemon.core.repo_overrides import RepoOverrides
from maxwell_daemon.gh import Issue, PullRequest
from maxwell_daemon.gh.executor import IssueExecutionError, IssueExecutor


def _issue() -> Issue:
    return Issue(
        number=1, title="Bug", body="body", state="OPEN", labels=[], url="https://x/i/1"
    )


@dataclass
class FakeGitHub:
    issue: Issue
    pr: PullRequest = field(
        default_factory=lambda: PullRequest(number=1, url="https://x/p/1", draft=True)
    )

    async def get_issue(self, repo: str, number: int) -> Issue:
        return self.issue

    async def create_pull_request(self, repo: str, **kwargs: Any) -> PullRequest:
        return self.pr


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


class TestPickStaticMethod:
    def test_pick_returns_default_when_overrides_is_none(self) -> None:
        assert IssueExecutor._pick(None, "context_max_chars", 9999) == 9999

    def test_pick_returns_override_value_when_set(self) -> None:
        assert (
            IssueExecutor._pick(
                RepoOverrides(context_max_chars=12345), "context_max_chars", 9999
            )
            == 12345
        )

    def test_pick_returns_default_when_override_field_is_none(self) -> None:
        assert (
            IssueExecutor._pick(
                RepoOverrides(context_max_chars=None), "context_max_chars", 9999
            )
            == 9999
        )


class TestParseResponseEdgeCases:
    def test_empty_plan_raises(self) -> None:
        with pytest.raises(IssueExecutionError, match="missing non-empty 'plan'"):
            IssueExecutor._parse_response(json.dumps({"plan": "", "diff": ""}))

    def test_missing_plan_key_raises(self) -> None:
        with pytest.raises(IssueExecutionError, match="missing non-empty 'plan'"):
            IssueExecutor._parse_response(json.dumps({"diff": "x"}))


class TestRepoConfigExpandPath:
    def test_expand_path_accepts_path_object(self, tmp_path: Path) -> None:
        rc = RepoConfig.model_validate({"name": "x", "path": tmp_path})
        assert rc.path == tmp_path

    def test_expand_path_expands_tilde(self) -> None:
        rc = RepoConfig.model_validate({"name": "x", "path": "~/myrepo"})
        assert not str(rc.path).startswith("~")


class TestDraftChangeWithContextAndMemory:
    def test_memory_and_context_included_in_prompt(self) -> None:
        captured_messages: list[list[Message]] = []

        class CaptureMsgBackend(ILLMBackend):
            name = "capture"

            async def complete(
                self, messages: list[Message], *, model: str, **kw: Any
            ) -> BackendResponse:
                captured_messages.append(messages)
                return BackendResponse(
                    content=json.dumps({"plan": "ok", "diff": ""}),
                    finish_reason="stop",
                    usage=TokenUsage(total_tokens=5),
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

        asyncio.run(
            IssueExecutor(
                github=FakeGitHub(issue=_issue()),
                workspace=FakeWorkspace(),
                backend=CaptureMsgBackend(),
            )._draft_change(
                issue_title="title",
                issue_body="body",
                model="m",
                context="CONTEXT_MARKER",
                memory="MEMORY_MARKER",
                labels=[],
            )
        )
        assert captured_messages
        user_text = next(
            m for m in captured_messages[0] if m.role == MessageRole.USER
        ).content
        assert "CONTEXT_MARKER" in user_text
        assert "MEMORY_MARKER" in user_text
