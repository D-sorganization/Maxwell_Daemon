"""Tests for IssueExecutor's pr_target_branch + fallback logic (#65).

The executor must base PRs on the repo's configured target branch (default
``staging``). If the branch doesn't exist on the remote and fallback is
enabled, it quietly switches to the default branch; if fallback is disabled,
it raises so the operator sees the misconfiguration.
"""

from __future__ import annotations

from typing import Any

import pytest

from conductor.gh.client import Issue, PullRequest
from conductor.gh.executor import IssueExecutionError, IssueExecutor


class _GhStub:
    def __init__(
        self,
        *,
        branches: list[str] | None = None,
        default_branch: str = "main",
    ) -> None:
        self._branches = branches or ["main"]
        self._default = default_branch
        self.create_calls: list[dict[str, Any]] = []
        self.list_branches_calls = 0

    async def get_issue(self, repo: str, number: int) -> Issue:
        return Issue(
            number=number,
            title="test",
            body="body",
            state="OPEN",
            labels=[],
            url="https://example/issues/1",
        )

    async def list_branches(self, repo: str) -> list[str]:
        self.list_branches_calls += 1
        return list(self._branches)

    async def get_default_branch(self, repo: str) -> str:
        return self._default

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
        self.create_calls.append(
            {"repo": repo, "head": head, "base": base, "title": title, "draft": draft}
        )
        return PullRequest(number=1, url="https://example/pull/1", draft=draft)


class _WorkspaceStub:
    async def ensure_clone(self, repo: str, *, task_id: str) -> Any:
        return "/tmp/ws"

    async def create_branch(
        self, repo: str, branch: str, *, base: str = "main", task_id: str
    ) -> None:
        self.last_base = base

    async def apply_diff(self, repo: str, diff: str, *, task_id: str) -> None: ...

    async def commit_and_push(
        self, repo: str, *, branch: str, message: str, task_id: str
    ) -> None: ...


class _BackendStub:
    async def complete(self, messages: Any, *, model: str, **_: Any) -> Any:
        class _R:
            content = '{"plan":"p","diff":""}'

        return _R()


class TestResolvePrTargetBranch:
    async def test_uses_configured_branch_when_it_exists(self) -> None:
        gh = _GhStub(branches=["main", "staging"])
        target = await IssueExecutor.resolve_pr_target_branch(
            gh, "acme/foo", preferred="staging", fallback_to_default=True
        )
        assert target == "staging"
        assert gh.list_branches_calls == 1

    async def test_falls_back_when_branch_missing(self) -> None:
        gh = _GhStub(branches=["main"], default_branch="main")
        target = await IssueExecutor.resolve_pr_target_branch(
            gh, "acme/foo", preferred="staging", fallback_to_default=True
        )
        assert target == "main"

    async def test_fallback_disabled_raises(self) -> None:
        gh = _GhStub(branches=["main"])
        with pytest.raises(IssueExecutionError, match="staging"):
            await IssueExecutor.resolve_pr_target_branch(
                gh, "acme/foo", preferred="staging", fallback_to_default=False
            )

    async def test_preferred_is_default_branch_skips_list(self) -> None:
        """Optimisation: if preferred == default, no branch listing needed."""
        gh = _GhStub(branches=["main"], default_branch="main")
        target = await IssueExecutor.resolve_pr_target_branch(
            gh, "acme/foo", preferred="main", fallback_to_default=True
        )
        assert target == "main"
        assert gh.list_branches_calls == 0
