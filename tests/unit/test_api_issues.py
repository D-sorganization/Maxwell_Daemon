"""REST API — /api/v1/issues and /api/v1/issues/dispatch endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.gh import Issue


class FakeIssueExecutor:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def execute_issue(
        self, *, repo: str, issue_number: int, model: str, mode: str = "plan", **_: Any
    ) -> Any:
        from maxwell_daemon.gh.executor import IssueResult

        return IssueResult(
            issue_number=issue_number,
            pr_url=f"https://github.com/{repo}/pull/1",
            pr_number=1,
            plan="fake plan",
            applied_diff=False,
        )


class FakeGitHubClient:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def create_issue(
        self, repo: str, *, title: str, body: str, labels: list[str] | None = None
    ) -> str:
        return f"https://github.com/{repo}/issues/7"

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 25) -> list[Issue]:
        return [
            Issue(
                number=1,
                title="First",
                body="",
                state="OPEN",
                labels=["bug"],
                url=f"https://github.com/{repo}/issues/1",
            )
        ]


@pytest.fixture
def daemon(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Daemon]:
    monkeypatch.setattr("maxwell_daemon.api.server.GitHubClient", FakeGitHubClient, raising=False)
    monkeypatch.setattr("maxwell_daemon.gh.GitHubClient", FakeGitHubClient)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        workspace_root=tmp_path / "ws",
    )
    d.set_issue_collaborators(
        github_client=FakeGitHubClient(),
        workspace=object(),
        executor_factory=lambda gh, ws, be: FakeIssueExecutor(),
    )
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon)) as c:
        yield c


class TestCreateIssue:
    def test_creates_and_returns_url(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues",
            json={"repo": "owner/repo", "title": "Fix it", "body": "details"},
        )
        assert r.status_code == 201
        assert r.json()["url"].endswith("/issues/7")

    def test_dispatch_flag_queues_task(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues",
            json={
                "repo": "owner/repo",
                "title": "Auto",
                "body": "go",
                "dispatch": True,
                "mode": "plan",
            },
        )
        assert r.status_code == 201
        assert "task_id" in r.json()

    def test_rejects_invalid_repo(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues",
            json={"repo": "not a repo", "title": "x", "body": ""},
        )
        assert r.status_code == 422


class TestDispatchIssue:
    def test_queues_issue_task(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues/dispatch",
            json={"repo": "owner/repo", "number": 42, "mode": "plan"},
        )
        assert r.status_code == 202
        body = r.json()
        assert body["kind"] == "issue"
        assert body["issue_repo"] == "owner/repo"
        assert body["issue_number"] == 42

    def test_rejects_bad_mode(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues/dispatch",
            json={"repo": "o/r", "number": 1, "mode": "yolo"},
        )
        assert r.status_code == 422

    def test_rejects_zero_number(self, client: TestClient) -> None:
        r = client.post(
            "/api/v1/issues/dispatch",
            json={"repo": "o/r", "number": 0},
        )
        assert r.status_code == 422


class TestListIssues:
    def test_returns_list(self, client: TestClient) -> None:
        r = client.get("/api/v1/issues/owner/repo?state=open&limit=5")
        assert r.status_code == 200
        body = r.json()
        assert body[0]["number"] == 1
        assert body[0]["title"] == "First"
