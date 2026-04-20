"""End-to-end: create issue → dispatch daemon → daemon opens draft PR.

Fakes the GitHub CLI and the LLM but exercises the real daemon, API, event bus,
and ledger. Proves the whole dispatch loop works without touching the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.config import MaxwellDaemonConfig, save_config
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.gh import Issue, PullRequest


class StubGitHub:
    """In-process stand-in for the real GitHubClient — no subprocess involved."""

    def __init__(self) -> None:
        self._next_issue = 1
        self._issues: dict[tuple[str, int], Issue] = {}
        self._prs: list[dict[str, Any]] = []

    async def create_issue(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> str:
        n = self._next_issue
        self._next_issue += 1
        self._issues[(repo, n)] = Issue(
            number=n,
            title=title,
            body=body,
            state="OPEN",
            labels=list(labels or []),
            url=f"https://github.com/{repo}/issues/{n}",
        )
        return f"https://github.com/{repo}/issues/{n}"

    async def get_issue(self, repo: str, number: int) -> Issue:
        return self._issues[(repo, number)]

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
        self._prs.append({"repo": repo, "head": head, "base": base, "title": title, "body": body})
        n = 1000 + len(self._prs)
        return PullRequest(number=n, url=f"https://github.com/{repo}/pull/{n}", draft=draft)

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 25) -> list[Issue]:
        return [i for (r, _), i in self._issues.items() if r == repo]


class StubBackend(ILLMBackend):
    name = "stub"

    def __init__(self, **kw: Any) -> None:
        pass

    async def complete(
        self, messages: list[Message], *, model: str, **kwargs: Any
    ) -> BackendResponse:
        # Return a valid JSON plan (no diff, so implement mode would fail).
        return BackendResponse(
            content='{"plan": "Add a failing test, then fix the logic.", "diff": ""}',
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            model=model,
            backend=self.name,
        )

    async def stream(self, *a: Any, **kw: Any):
        if False:
            yield ""

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(cost_per_1k_input_tokens=0.001, cost_per_1k_output_tokens=0.002)


@pytest.fixture
def register_stub() -> Iterator[None]:
    from maxwell_daemon.backends import registry

    registry._factories["stub"] = StubBackend
    yield
    registry._factories.pop("stub", None)


@pytest.fixture
def full_system(
    tmp_path: Path,
    register_stub: None,
) -> Iterator[tuple[Daemon, TestClient, StubGitHub, asyncio.AbstractEventLoop]]:
    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "stub", "model": "stub-v1"}},
            "agent": {"default_backend": "primary"},
        }
    )
    cfg_path = tmp_path / "c.yaml"
    save_config(cfg, cfg_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = Daemon(cfg, ledger_path=tmp_path / "l.db", workspace_root=tmp_path / "ws")
    stub_gh = StubGitHub()

    # The daemon's issue path needs collaborators wired in.
    from maxwell_daemon.gh.executor import IssueExecutor

    daemon.set_issue_collaborators(
        github_client=stub_gh,
        workspace=object(),  # unused in plan mode
        executor_factory=lambda gh, ws, be: IssueExecutor(github=stub_gh, workspace=ws, backend=be),
    )

    loop.run_until_complete(daemon.start(worker_count=1))

    with TestClient(create_app(daemon, github_client=stub_gh)) as client:
        try:
            yield daemon, client, stub_gh, loop
        finally:
            loop.run_until_complete(daemon.stop())
            loop.close()
            asyncio.set_event_loop(None)


def _wait_done(
    client: TestClient, loop: asyncio.AbstractEventLoop, task_id: str, timeout: float = 5.0
) -> dict[str, Any]:
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        t = client.get(f"/api/v1/tasks/{task_id}").json()
        if t["status"] in {"completed", "failed"}:
            return t
        loop.run_until_complete(asyncio.sleep(0.05))
    raise AssertionError(f"task did not complete: {t}")


class TestIssueCreationAndDispatch:
    def test_create_then_dispatch_roundtrip(
        self,
        full_system: tuple[Daemon, TestClient, StubGitHub, asyncio.AbstractEventLoop],
    ) -> None:
        _, client, stub_gh, loop = full_system

        # 1. Create the issue.
        r = client.post(
            "/api/v1/issues",
            json={"repo": "owner/project", "title": "Bug in parser", "body": "Repro: ..."},
        )
        assert r.status_code == 201
        assert r.json()["url"].endswith("/issues/1")

        # 2. Dispatch the daemon against it.
        r = client.post(
            "/api/v1/issues/dispatch",
            json={"repo": "owner/project", "number": 1, "mode": "plan"},
        )
        assert r.status_code == 202
        task_id = r.json()["id"]

        # 3. Wait for the daemon to finish.
        final = _wait_done(client, loop, task_id)
        assert final["status"] == "completed"
        assert final["kind"] == "issue"
        assert final["pr_url"].endswith("/pull/1001")

        # 4. The stub should have seen one create_pr call.
        assert len(stub_gh._prs) == 1
        assert stub_gh._prs[0]["repo"] == "owner/project"

    def test_create_with_dispatch_flag(
        self,
        full_system: tuple[Daemon, TestClient, StubGitHub, asyncio.AbstractEventLoop],
    ) -> None:
        _, client, _stub_gh, loop = full_system

        r = client.post(
            "/api/v1/issues",
            json={
                "repo": "owner/project",
                "title": "Auto",
                "body": "go",
                "dispatch": True,
                "mode": "plan",
            },
        )
        assert r.status_code == 201
        task_id = r.json()["task_id"]

        final = _wait_done(client, loop, task_id)
        assert final["status"] == "completed"
        assert final["pr_url"] is not None

    def test_multiple_issues_dispatch_concurrently(
        self,
        full_system: tuple[Daemon, TestClient, StubGitHub, asyncio.AbstractEventLoop],
    ) -> None:
        """User can keep adding issues while existing ones are in flight."""
        _, client, _, loop = full_system

        task_ids: list[str] = []
        for i in range(3):
            client.post(
                "/api/v1/issues",
                json={
                    "repo": "owner/project",
                    "title": f"Issue {i}",
                    "body": "",
                    "dispatch": True,
                    "mode": "plan",
                },
            )
            # All previous issues are visible immediately; daemon works through
            # them in the background without blocking submission.
            task_ids.append(client.get("/api/v1/tasks").json()[-1]["id"])

        for tid in task_ids:
            final = _wait_done(client, loop, tid)
            assert final["status"] == "completed"
