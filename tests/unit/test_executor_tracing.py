"""IssueExecutor emits expected spans when tracing is configured."""

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
    TokenUsage,
)
from maxwell_daemon.gh import Issue, PullRequest
from maxwell_daemon.gh.executor import IssueExecutor
from maxwell_daemon.tracing import _test_exporter, configure_tracing


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
        self.pr_calls.append({"body": body})
        return PullRequest(number=1, url="u", draft=draft)


class _Backend(ILLMBackend):
    name = "b"

    async def complete(
        self, messages: list[Message], *, model: str, **_: Any
    ) -> BackendResponse:
        return BackendResponse(
            content=json.dumps({"plan": "p", "diff": ""}),
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


@dataclass
class _WS:
    async def ensure_clone(self, *a: Any, **kw: Any) -> Path:
        return Path("/fake")

    async def create_branch(self, *a: Any, **kw: Any) -> None:
        pass

    async def apply_diff(self, *a: Any, **kw: Any) -> None:
        pass

    async def commit_and_push(self, *a: Any, **kw: Any) -> None:
        pass


class TestSpans:
    def test_plan_mode_emits_fetch_draft_and_open_pr(self) -> None:
        try:
            configure_tracing(use_memory_exporter=True)
            gh = _GH(
                issue=Issue(
                    number=1, title="t", body="b", state="OPEN", labels=[], url="u"
                )
            )
            executor = IssueExecutor(github=gh, workspace=_WS(), backend=_Backend())
            asyncio.run(
                executor.execute_issue(
                    repo="o/r", issue_number=1, model="m", mode="plan"
                )
            )
            names = {s.name for s in _test_exporter().get_finished_spans()}
            assert {
                "maxwell_daemon.issue.fetch",
                "maxwell_daemon.issue.draft",
                "maxwell_daemon.issue.open_pr",
            } <= names
        finally:
            configure_tracing(endpoint=None)

    def test_spans_carry_issue_number_attribute(self) -> None:
        try:
            configure_tracing(use_memory_exporter=True)
            gh = _GH(
                issue=Issue(
                    number=7, title="t", body="b", state="OPEN", labels=[], url="u"
                )
            )
            executor = IssueExecutor(github=gh, workspace=_WS(), backend=_Backend())
            asyncio.run(
                executor.execute_issue(
                    repo="o/r", issue_number=7, model="m", mode="plan"
                )
            )
            fetch = next(
                s
                for s in _test_exporter().get_finished_spans()
                if s.name == "maxwell_daemon.issue.fetch"
            )
            assert fetch.attributes["issue"] == 7
            assert fetch.attributes["repo"] == "o/r"
        finally:
            configure_tracing(endpoint=None)
