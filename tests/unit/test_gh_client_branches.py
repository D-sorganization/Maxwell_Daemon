"""Tests for GitHubClient.list_branches and get_default_branch.

These methods support the staging-branch workflow (#65): the executor needs
to know which branches exist on the remote so it can fall back from
``staging`` to the default branch when ``staging`` doesn't exist yet.
"""

from __future__ import annotations

import json

import pytest

from conductor.gh.client import GhCliError, GitHubClient


class _StubRunner:
    """Inject canned (rc, stdout, stderr) responses keyed by argv."""

    def __init__(self, canned: dict[tuple[str, ...], tuple[int, bytes, bytes]]) -> None:
        self.canned = canned
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
        self.calls.append(tuple(argv))
        for key, resp in self.canned.items():
            if argv[: len(key)] == key:
                return resp
        return 1, b"", f"no canned response for {argv}".encode()


class TestListBranches:
    async def test_returns_branch_names(self) -> None:
        payload = json.dumps([{"name": "main"}, {"name": "staging"}, {"name": "dev"}])
        runner = _StubRunner({("gh", "api", "repos/acme/foo/branches"): (0, payload.encode(), b"")})
        gh = GitHubClient(runner=runner)
        branches = await gh.list_branches("acme/foo")
        assert branches == ["main", "staging", "dev"]

    async def test_invalid_repo_rejected(self) -> None:
        gh = GitHubClient(runner=_StubRunner({}))
        with pytest.raises(ValueError, match="Invalid repo"):
            await gh.list_branches("not-valid")

    async def test_gh_error_propagates(self) -> None:
        runner = _StubRunner({("gh", "api", "repos/acme/foo/branches"): (1, b"", b"api boom")})
        gh = GitHubClient(runner=runner)
        with pytest.raises(GhCliError, match="api boom"):
            await gh.list_branches("acme/foo")

    async def test_paginates_or_limits(self) -> None:
        """Sanity: we pass --paginate so gh collects all pages (large fleets)."""
        payload = json.dumps([{"name": "main"}])
        runner = _StubRunner({("gh", "api", "repos/acme/foo/branches"): (0, payload.encode(), b"")})
        gh = GitHubClient(runner=runner)
        await gh.list_branches("acme/foo")
        argv = runner.calls[0]
        assert "--paginate" in argv


class TestGetDefaultBranch:
    async def test_returns_default_branch(self) -> None:
        payload = json.dumps({"default_branch": "trunk"})
        runner = _StubRunner({("gh", "api", "repos/acme/foo"): (0, payload.encode(), b"")})
        gh = GitHubClient(runner=runner)
        assert await gh.get_default_branch("acme/foo") == "trunk"

    async def test_missing_field_surfaces_as_error(self) -> None:
        runner = _StubRunner({("gh", "api", "repos/acme/foo"): (0, b'{"id": 1}', b"")})
        gh = GitHubClient(runner=runner)
        with pytest.raises(GhCliError, match="default_branch"):
            await gh.get_default_branch("acme/foo")
