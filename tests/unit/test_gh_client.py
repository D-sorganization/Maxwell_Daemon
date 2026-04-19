"""GitHubClient — thin subprocess wrapper around the `gh` CLI.

We use gh because it handles auth, rate limits, and pagination for us. Tests
stub subprocess at the boundary so nothing hits the real API.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from conductor.gh import GhCliError, GitHubClient, Issue, PullRequest


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


class FakeRunner:
    """Stand-in for asyncio.create_subprocess_exec — captures calls, returns canned output."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._responses: dict[tuple[str, ...], tuple[int, bytes, bytes]] = {}

    def respond(
        self,
        *argv: str,
        returncode: int = 0,
        stdout: bytes | str = b"",
        stderr: bytes | str = b"",
    ) -> None:
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        self._responses[argv] = (returncode, stdout, stderr)

    async def __call__(self, *argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        return self._responses.get(argv, (0, b"", b""))


class TestListIssues:
    def test_parses_json_output(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "issue",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,body,state,labels,url",
            stdout=json.dumps(
                [
                    {
                        "number": 42,
                        "title": "fix the bug",
                        "body": "it broke",
                        "state": "OPEN",
                        "labels": [{"name": "bug"}],
                        "url": "https://github.com/owner/repo/issues/42",
                    }
                ]
            ),
        )
        client = GitHubClient(runner=fake_runner)

        issues = asyncio.run(client.list_issues("owner/repo"))

        assert len(issues) == 1
        assert issues[0].number == 42
        assert issues[0].title == "fix the bug"
        assert "bug" in issues[0].labels

    def test_rejects_malformed_repo(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        with pytest.raises(ValueError, match="owner/name"):
            asyncio.run(client.list_issues("not-a-repo"))

    def test_propagates_gh_failure(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "issue",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,body,state,labels,url",
            returncode=1,
            stderr=b"auth required",
        )
        client = GitHubClient(runner=fake_runner)
        with pytest.raises(GhCliError, match="auth required"):
            asyncio.run(client.list_issues("owner/repo"))


class TestCreateIssue:
    def test_sends_title_and_body(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "issue",
            "create",
            "--repo",
            "owner/repo",
            "--title",
            "Something broke",
            "--body",
            "details here",
            stdout=b"https://github.com/owner/repo/issues/99\n",
        )
        client = GitHubClient(runner=fake_runner)

        url = asyncio.run(
            client.create_issue("owner/repo", title="Something broke", body="details here")
        )

        assert url == "https://github.com/owner/repo/issues/99"

    def test_forwards_labels(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "issue",
            "create",
            "--repo",
            "owner/repo",
            "--title",
            "T",
            "--body",
            "B",
            "--label",
            "bug,good-first-issue",
            stdout=b"https://github.com/owner/repo/issues/1\n",
        )
        client = GitHubClient(runner=fake_runner)

        asyncio.run(
            client.create_issue(
                "owner/repo", title="T", body="B", labels=["bug", "good-first-issue"]
            )
        )


class TestGetIssue:
    def test_fetches_single_issue(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "issue",
            "view",
            "42",
            "--repo",
            "owner/repo",
            "--json",
            "number,title,body,state,labels,url",
            stdout=json.dumps(
                {
                    "number": 42,
                    "title": "t",
                    "body": "b",
                    "state": "OPEN",
                    "labels": [],
                    "url": "https://github.com/owner/repo/issues/42",
                }
            ),
        )
        client = GitHubClient(runner=fake_runner)
        issue = asyncio.run(client.get_issue("owner/repo", 42))
        assert issue.number == 42
        assert issue.title == "t"


class TestCreatePullRequest:
    def test_creates_draft_pr(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond(
            "gh",
            "pr",
            "create",
            "--repo",
            "owner/repo",
            "--head",
            "feature-branch",
            "--base",
            "main",
            "--title",
            "Fix #42",
            "--body",
            "plan",
            "--draft",
            stdout=b"https://github.com/owner/repo/pull/100\n",
        )
        client = GitHubClient(runner=fake_runner)

        pr = asyncio.run(
            client.create_pull_request(
                "owner/repo",
                head="feature-branch",
                base="main",
                title="Fix #42",
                body="plan",
                draft=True,
            )
        )

        assert isinstance(pr, PullRequest)
        assert pr.url == "https://github.com/owner/repo/pull/100"
        assert pr.number == 100


class TestAuth:
    def test_check_auth_success(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond("gh", "auth", "status", returncode=0)
        client = GitHubClient(runner=fake_runner)
        assert asyncio.run(client.check_auth()) is True

    def test_check_auth_failure(self, fake_runner: FakeRunner) -> None:
        fake_runner.respond("gh", "auth", "status", returncode=1, stderr=b"not logged in")
        client = GitHubClient(runner=fake_runner)
        assert asyncio.run(client.check_auth()) is False


class TestRepoValidation:
    def test_accepts_owner_name(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        # Doesn't raise — validation passes.
        client._validate_repo("owner/name")

    def test_rejects_missing_slash(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        with pytest.raises(ValueError):
            client._validate_repo("no-slash")

    def test_rejects_injection_chars(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        for bad in ("owner;rm/x", "owner/x`cat`", "own er/name", "-flag/repo"):
            with pytest.raises(ValueError):
                client._validate_repo(bad)


class TestModels:
    def test_issue_from_gh_payload(self) -> None:
        payload = {
            "number": 7,
            "title": "t",
            "body": "b",
            "state": "OPEN",
            "labels": [{"name": "bug"}, {"name": "p1"}],
            "url": "https://github.com/o/r/issues/7",
        }
        issue = Issue.from_gh(payload)
        assert issue.number == 7
        assert issue.is_open is True
        assert issue.labels == ["bug", "p1"]

    def test_issue_closed(self) -> None:
        payload = {
            "number": 1,
            "title": "t",
            "body": "",
            "state": "CLOSED",
            "labels": [],
            "url": "u",
        }
        assert Issue.from_gh(payload).is_open is False
