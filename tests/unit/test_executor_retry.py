"""IssueExecutor — diff-apply retry with LLM refinement on failure."""

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
    TokenUsage,
)
from maxwell_daemon.gh import Issue, PullRequest
from maxwell_daemon.gh.executor import IssueExecutionError, IssueExecutor
from maxwell_daemon.gh.workspace import WorkspaceError


@dataclass
class _GH:
    issue: Issue
    pr_calls: list[dict[str, Any]] = field(default_factory=list)

    async def get_issue(self, repo: str, number: int) -> Issue:
        return self.issue

    async def create_pull_request(
        self,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> PullRequest:
        self.pr_calls.append({"body": body, "head": head})
        return PullRequest(number=1, url="u", draft=draft)


@dataclass
class _WS:
    apply_fails_first_n: int = 0
    apply_calls: list[str] = field(default_factory=list)

    async def ensure_clone(self, repo: str, **_: Any) -> Path:
        return Path("/fake")

    async def create_branch(self, repo: str, branch: str, *, base: str = "main", **_: Any) -> None:
        pass

    async def apply_diff(self, repo: str, diff: str, **_: Any) -> None:
        self.apply_calls.append(diff)
        if len(self.apply_calls) <= self.apply_fails_first_n:
            raise WorkspaceError("patch does not apply")

    async def commit_and_push(self, repo: str, *, branch: str, message: str, **_: Any) -> None:
        pass


class _ScriptedBackend(ILLMBackend):
    """Returns a sequence of canned responses, one per call."""

    name = "scripted"

    def __init__(self, responses: list[dict[str, str]]) -> None:
        self._responses = responses
        self.calls: list[list[Message]] = []

    async def complete(self, messages: list[Message], *, model: str, **_: Any) -> BackendResponse:
        self.calls.append(messages)
        payload = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return BackendResponse(
            content=json.dumps(payload),
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


def _issue() -> Issue:
    return Issue(number=1, title="t", body="b", state="OPEN", labels=[], url="u")


class TestDiffRetry:
    def test_retries_with_refinement_on_apply_failure(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS(apply_fails_first_n=1)
        backend = _ScriptedBackend(
            [
                {"plan": "first attempt", "diff": "bad diff\n"},
                {"plan": "corrected", "diff": "diff --git a/x b/x\n"},
            ]
        )
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend, max_diff_retries=2)

        result = asyncio.run(
            executor.execute_issue(repo="o/r", issue_number=1, model="m", mode="implement")
        )
        assert result.applied_diff is True
        assert len(ws.apply_calls) == 2  # first failed, second succeeded
        # Second LLM call should have received the failure message.
        second_prompt = backend.calls[1][-1].content
        assert "did not apply" in second_prompt or "failed" in second_prompt

    def test_gives_up_after_max_retries(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS(apply_fails_first_n=99)
        backend = _ScriptedBackend([{"plan": "p", "diff": "diff --git a/x b/x\n"}])
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend, max_diff_retries=2)

        with pytest.raises(IssueExecutionError, match=r"after \d+ attempt"):
            asyncio.run(
                executor.execute_issue(repo="o/r", issue_number=1, model="m", mode="implement")
            )
        # Initial attempt + 2 retries = 3 total apply calls.
        assert len(ws.apply_calls) == 3

    def test_plan_mode_never_retries(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS()
        backend = _ScriptedBackend([{"plan": "p", "diff": ""}])
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend, max_diff_retries=5)

        asyncio.run(executor.execute_issue(repo="o/r", issue_number=1, model="m", mode="plan"))
        # Plan mode doesn't touch the workspace.
        assert ws.apply_calls == []
        assert len(backend.calls) == 1
