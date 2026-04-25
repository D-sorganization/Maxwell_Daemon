"""IssueExecutor — turn a GitHub issue into a draft PR via an LLM.

Uses the RecordingBackend (test double) for the LLM and inline fakes for the
GitHubClient / Workspace so we can assert the orchestration order without
hitting the network or filesystem.
"""

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
from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore
from maxwell_daemon.gh import Issue, PullRequest
from maxwell_daemon.gh.executor import IssueExecutionError, IssueExecutor


@dataclass
class FakeGitHub:
    issue: Issue
    created_pr: PullRequest = field(
        default_factory=lambda: PullRequest(number=100, url="https://x/pull/100", draft=True)
    )
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
        self.pr_calls.append(
            {
                "repo": repo,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": draft,
            }
        )
        return self.created_pr


@dataclass
class FakeWorkspace:
    log: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def ensure_clone(self, repo: str, **_: Any) -> Path:
        self.log.append(("clone", (repo,)))
        return Path("/fake") / repo.split("/", 1)[1]

    async def create_branch(self, repo: str, branch: str, *, base: str = "main", **_: Any) -> None:
        self.log.append(("branch", (repo, branch, base)))

    async def apply_diff(self, repo: str, diff: str, **_: Any) -> None:
        self.log.append(("apply", (repo, len(diff))))

    async def commit_and_push(self, repo: str, *, branch: str, message: str, **_: Any) -> None:
        self.log.append(("commit_push", (repo, branch, message)))


class ScriptedBackend(ILLMBackend):
    """LLM test double that returns a canned JSON response."""

    name = "scripted"

    def __init__(self, *, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def complete(
        self, messages: list[Message], *, model: str, **kwargs: Any
    ) -> BackendResponse:
        return BackendResponse(
            content=json.dumps(self._payload),
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=100, total_tokens=150),
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


class AsyncOnlyMemory:
    def __init__(self) -> None:
        from maxwell_daemon.memory import ScratchPad

        self.scratchpad = ScratchPad()
        self.assembled: list[dict[str, Any]] = []
        self.recorded: list[dict[str, Any]] = []

    async def assemble_context_async(self, **kwargs: Any) -> str:
        self.assembled.append(kwargs)
        return "async memory context"

    def assemble_context(self, **_: Any) -> str:
        raise AssertionError("sync assemble_context should not be used")

    async def record_outcome_async(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)

    def record_outcome(self, **_: Any) -> None:
        raise AssertionError("sync record_outcome should not be used")


def _issue(body: str = "fix the bug") -> Issue:
    return Issue(
        number=42,
        title="Fix the bug",
        body=body,
        state="OPEN",
        labels=["bug"],
        url="https://github.com/o/r/issues/42",
    )


class TestPlanMode:
    def test_opens_draft_pr_with_plan_body(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(payload={"plan": "add a test and fix the off-by-one", "diff": ""})
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend)

        result = asyncio.run(
            executor.execute_issue(
                repo="owner/repo", issue_number=42, model="fake-model", mode="plan"
            )
        )

        assert result.pr_url == "https://x/pull/100"
        assert len(gh.pr_calls) == 1
        assert "off-by-one" in gh.pr_calls[0]["body"]
        assert gh.pr_calls[0]["draft"] is True
        # Plan mode: no diff applied
        steps = [s[0] for s in ws.log]
        assert "apply" not in steps

    def test_records_plan_diff_and_pr_body_artifacts(self, tmp_path: Path) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(payload={"plan": "artifact plan", "diff": ""})
        artifact_store = ArtifactStore(tmp_path / "artifacts.db", blob_root=tmp_path / "blobs")
        executor = IssueExecutor(
            github=gh,
            workspace=ws,
            backend=backend,
            artifact_store=artifact_store,
        )

        asyncio.run(
            executor.execute_issue(
                repo="owner/repo",
                issue_number=42,
                model="fake-model",
                mode="plan",
                task_id="task-42",
            )
        )

        artifacts = artifact_store.list_for_task("task-42")
        assert [artifact.kind for artifact in artifacts] == [
            ArtifactKind.PLAN,
            ArtifactKind.DIFF,
            ArtifactKind.PR_BODY,
        ]
        assert artifact_store.read_text(artifacts[0].id) == "artifact plan"
        assert "Closes #42" in artifact_store.read_text(artifacts[2].id)

    def test_uses_async_memory_methods_when_available(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(payload={"plan": "memory-aware plan", "diff": ""})
        memory = AsyncOnlyMemory()
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend, memory=memory)

        asyncio.run(
            executor.execute_issue(
                repo="owner/repo",
                issue_number=42,
                model="fake-model",
                mode="plan",
                task_id="task-42",
            )
        )

        assert memory.assembled[0]["repo"] == "owner/repo"
        assert memory.assembled[0]["task_id"] == "task-42"
        assert memory.recorded[0]["pr_url"] == "https://x/pull/100"


class TestImplementMode:
    def test_applies_diff_when_provided(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(
            payload={
                "plan": "fix it",
                "diff": "diff --git a/x b/x\n@@ -0,0 +1 @@\n+x\n",
            }
        )
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend)

        asyncio.run(
            executor.execute_issue(
                repo="owner/repo", issue_number=42, model="fake-model", mode="implement"
            )
        )

        steps = [s[0] for s in ws.log]
        assert steps == ["clone", "branch", "apply", "commit_push"]

    def test_skips_commit_when_no_diff(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(payload={"plan": "just a note", "diff": ""})
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend)

        with pytest.raises(IssueExecutionError, match="no diff"):
            asyncio.run(
                executor.execute_issue(
                    repo="owner/repo",
                    issue_number=42,
                    model="fake-model",
                    mode="implement",
                )
            )


class TestLLMResponseParsing:
    def test_strips_markdown_fences(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()

        class FencedBackend(ILLMBackend):
            name = "fenced"

            async def complete(self, *a: Any, **kw: Any) -> BackendResponse:
                content = '```json\n{"plan":"hi","diff":""}\n```'
                return BackendResponse(
                    content=content,
                    finish_reason="stop",
                    usage=TokenUsage(total_tokens=10),
                    model=kw["model"],
                    backend=self.name,
                )

            async def stream(self, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
                if False:
                    yield ""

            async def health_check(self) -> bool:
                return True

            def capabilities(self, model: str) -> BackendCapabilities:
                return BackendCapabilities()

        executor = IssueExecutor(github=gh, workspace=ws, backend=FencedBackend())
        asyncio.run(
            executor.execute_issue(repo="owner/repo", issue_number=42, model="m", mode="plan")
        )
        assert "hi" in gh.pr_calls[0]["body"]

    def test_non_json_response_raises(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()

        class BrokenBackend(ILLMBackend):
            name = "broken"

            async def complete(self, *a: Any, **kw: Any) -> BackendResponse:
                return BackendResponse(
                    content="not json at all",
                    finish_reason="stop",
                    usage=TokenUsage(total_tokens=5),
                    model=kw["model"],
                    backend=self.name,
                )

            async def stream(self, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
                if False:
                    yield ""

            async def health_check(self) -> bool:
                return True

            def capabilities(self, model: str) -> BackendCapabilities:
                return BackendCapabilities()

        executor = IssueExecutor(github=gh, workspace=ws, backend=BrokenBackend())
        with pytest.raises(IssueExecutionError, match="parse"):
            asyncio.run(
                executor.execute_issue(repo="owner/repo", issue_number=42, model="m", mode="plan")
            )


class TestBranchNaming:
    def test_derives_branch_from_issue_number(self) -> None:
        gh = FakeGitHub(issue=_issue())
        ws = FakeWorkspace()
        backend = ScriptedBackend(payload={"plan": "ok", "diff": ""})
        executor = IssueExecutor(github=gh, workspace=ws, backend=backend)

        asyncio.run(
            executor.execute_issue(repo="owner/repo", issue_number=42, model="m", mode="plan")
        )
        assert gh.pr_calls[0]["head"] == "maxwell-daemon/issue-42"
