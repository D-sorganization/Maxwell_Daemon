"""Tests for the ``gh_proxy`` tool."""

from __future__ import annotations

import json
from typing import Any

import pytest

from maxwell_daemon.tools.gh_proxy import (
    GhProxyAllowlist,
    GhProxyError,
    _validate_params,
    make_gh_proxy,
)


class FakeGitHubClient:
    """Stub GitHubClient for unit tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def create_pull_request(self, **kwargs: Any) -> Any:
        self.calls.append(("create_pull_request", kwargs))
        return type("PR", (), {"url": "https://github.com/test/pr/1"})()

    async def create_issue(self, **kwargs: Any) -> str:
        self.calls.append(("create_issue", kwargs))
        return "https://github.com/test/issues/1"

    async def create_comment(self, **kwargs: Any) -> str:
        self.calls.append(("create_comment", kwargs))
        return "https://github.com/test/issues/1#issuecomment-1"


@pytest.fixture
def fake_client() -> FakeGitHubClient:
    return FakeGitHubClient()


class TestValidateParams:
    def test_valid_create_pr(self) -> None:
        _validate_params(
            "create_pr",
            {
                "repo": "owner/repo",
                "head": "feature",
                "base": "main",
                "title": "add feature",
                "body": "details",
            },
        )

    def test_missing_required_param(self) -> None:
        with pytest.raises(GhProxyError, match="missing required params"):
            _validate_params("create_pr", {"repo": "owner/repo"})

    def test_unknown_param(self) -> None:
        with pytest.raises(GhProxyError, match="unknown params"):
            _validate_params(
                "create_pr",
                {
                    "repo": "owner/repo",
                    "head": "feature",
                    "base": "main",
                    "title": "add feature",
                    "body": "details",
                    "extra": "bad",
                },
            )

    def test_unknown_operation(self) -> None:
        with pytest.raises(GhProxyError, match="unknown operation"):
            _validate_params("delete_repo", {})


class TestGhProxyAllowlist:
    def test_default_allowlist(self) -> None:
        allowlist = GhProxyAllowlist.default()
        assert allowlist.allowed("create_pr")
        assert allowlist.allowed("create_issue")
        assert allowlist.allowed("comment")
        assert not allowlist.allowed("set_state")
        assert not allowlist.allowed("delete_repo")

    def test_custom_allowlist(self) -> None:
        allowlist = GhProxyAllowlist(frozenset({"comment"}))
        assert allowlist.allowed("comment")
        assert not allowlist.allowed("create_pr")


class TestMakeGhProxy:
    @pytest.mark.asyncio
    async def test_denied_operation_returns_error(self, fake_client: FakeGitHubClient) -> None:
        tool = make_gh_proxy(
            client=fake_client,  # type: ignore[arg-type]
            allowlist=GhProxyAllowlist(frozenset({"comment"})),
            task_id="t-123",
        )
        result = await tool(
            "create_pr", {"repo": "owner/repo", "head": "f", "base": "m", "title": "t", "body": "b"}
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "not allowed" in parsed["error"]

    @pytest.mark.asyncio
    async def test_allowed_operation_succeeds(self, fake_client: FakeGitHubClient) -> None:
        tool = make_gh_proxy(
            client=fake_client,  # type: ignore[arg-type]
            allowlist=GhProxyAllowlist.default(),
            task_id="t-123",
        )
        result = await tool(
            "create_pr",
            {
                "repo": "owner/repo",
                "head": "feature",
                "base": "main",
                "title": "add feature",
                "body": "details",
            },
        )
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["url"] == "https://github.com/test/pr/1"
        assert fake_client.calls == [
            (
                "create_pull_request",
                {
                    "repo": "owner/repo",
                    "head": "feature",
                    "base": "main",
                    "title": "add feature",
                    "body": "details",
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_validation_error_returns_json(self, fake_client: FakeGitHubClient) -> None:
        tool = make_gh_proxy(
            client=fake_client,  # type: ignore[arg-type]
            allowlist=GhProxyAllowlist.default(),
            task_id="t-123",
        )
        result = await tool("create_pr", {"repo": "owner/repo"})
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "missing required params" in parsed["error"]

    @pytest.mark.asyncio
    async def test_execution_error_returns_json(self, fake_client: FakeGitHubClient) -> None:
        class BrokenClient:
            async def create_pull_request(self, **kwargs: Any) -> Any:
                raise RuntimeError("simulated failure")

        tool = make_gh_proxy(
            client=BrokenClient(),  # type: ignore[arg-type]
            allowlist=GhProxyAllowlist.default(),
            task_id="t-123",
        )
        result = await tool(
            "create_pr",
            {
                "repo": "owner/repo",
                "head": "feature",
                "base": "main",
                "title": "add feature",
                "body": "details",
            },
        )
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "simulated failure" in parsed["error"]
