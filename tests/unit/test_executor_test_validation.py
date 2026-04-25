"""IssueExecutor — test-validation loop integrates with the diff-retry path."""

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
from maxwell_daemon.gh.test_runner import TestResult


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


@dataclass
class _WS:
    apply_calls: int = 0

    async def ensure_clone(self, repo: str, **_: Any) -> Path:
        return Path("/fake")

    async def create_branch(self, *a: Any, **kw: Any) -> None:
        pass

    async def apply_diff(self, repo: str, diff: str, **_: Any) -> None:
        self.apply_calls += 1

    async def commit_and_push(self, *a: Any, **kw: Any) -> None:
        pass


@dataclass
class _TestRunnerStub:
    results: list[TestResult]
    calls: int = 0

    async def detect_and_run(
        self,
        repo_path: Path,
        *,
        command: list[str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> TestResult:
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


class _ScriptedBackend(ILLMBackend):
    name = "scripted"

    def __init__(self, responses: list[dict[str, str]]) -> None:
        self._responses = responses
        self.calls: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], *, model: str, **_: Any
    ) -> BackendResponse:
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


class TestsPassFastPath:
    def test_passing_tests_open_pr_without_retry(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS()
        backend = _ScriptedBackend([{"plan": "fix", "diff": "diff --git a/x b/x\n"}])
        runner = _TestRunnerStub(
            [
                TestResult(
                    passed=True,
                    command="pytest",
                    returncode=0,
                    duration_seconds=0.1,
                    output_tail="ok",
                )
            ]
        )
        executor = IssueExecutor(
            github=gh, workspace=ws, backend=backend, test_runner=runner
        )
        result = asyncio.run(
            executor.execute_issue(
                repo="o/r", issue_number=1, model="m", mode="implement"
            )
        )
        assert result.applied_diff is True
        assert "tests passed" in gh.pr_calls[0]["body"].lower()
        assert runner.calls == 1
        assert len(backend.calls) == 1  # no retry needed


class TestsFailRetry:
    def test_failing_tests_trigger_refinement_then_succeed(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS()
        backend = _ScriptedBackend(
            [
                {"plan": "first", "diff": "diff --git a/x b/x\n"},
                {"plan": "second", "diff": "diff --git a/x b/x\n"},
            ]
        )
        runner = _TestRunnerStub(
            [
                TestResult(
                    passed=False,
                    command="pytest",
                    returncode=1,
                    duration_seconds=0.1,
                    output_tail="FAILED test_x",
                ),
                TestResult(
                    passed=True,
                    command="pytest",
                    returncode=0,
                    duration_seconds=0.1,
                    output_tail="ok",
                ),
            ]
        )
        executor = IssueExecutor(
            github=gh,
            workspace=ws,
            backend=backend,
            test_runner=runner,
            max_test_retries=2,
        )
        result = asyncio.run(
            executor.execute_issue(
                repo="o/r", issue_number=1, model="m", mode="implement"
            )
        )
        assert result.applied_diff is True
        assert runner.calls == 2
        assert len(backend.calls) == 2
        # Second LLM call should mention the test failure.
        second_prompt = backend.calls[1][-1].content
        assert "FAILED" in second_prompt or "failed" in second_prompt

    def test_gives_up_after_max_test_retries(self) -> None:
        gh = _GH(issue=_issue())
        ws = _WS()
        backend = _ScriptedBackend([{"plan": "p", "diff": "diff --git a/x b/x\n"}])
        runner = _TestRunnerStub(
            [
                TestResult(
                    passed=False,
                    command="pytest",
                    returncode=1,
                    duration_seconds=0.1,
                    output_tail="FAILED",
                )
            ]
        )
        executor = IssueExecutor(
            github=gh,
            workspace=ws,
            backend=backend,
            test_runner=runner,
            max_test_retries=1,
        )
        with pytest.raises(IssueExecutionError, match="tests still failing"):
            asyncio.run(
                executor.execute_issue(
                    repo="o/r", issue_number=1, model="m", mode="implement"
                )
            )
        assert runner.calls == 2  # initial + 1 retry


class TestRunnerOptional:
    def test_no_runner_means_no_validation(self) -> None:
        """If no test_runner is injected, executor skips validation entirely."""
        gh = _GH(issue=_issue())
        ws = _WS()
        backend = _ScriptedBackend([{"plan": "p", "diff": "diff --git a/x b/x\n"}])
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend)
        result = asyncio.run(
            executor.execute_issue(
                repo="o/r", issue_number=1, model="m", mode="implement"
            )
        )
        assert result.applied_diff is True
        assert "tests" not in gh.pr_calls[0]["body"].lower()
