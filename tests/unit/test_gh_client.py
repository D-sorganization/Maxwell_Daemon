"""GitHubClient — thin subprocess wrapper around the `gh` CLI.

We use gh because it handles auth, rate limits, and pagination for us. Tests
stub subprocess at the boundary so nothing hits the real API.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from maxwell_daemon.gh import GhCliError, GitHubClient, Issue, PullRequest
from maxwell_daemon.gh.client import GitHubRateLimitError


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


class TestRateLimitHandling:
    """GitHubClient retries on rate-limit responses and backs off (#152)."""

    def _make_runner(self, responses: list[tuple[int, bytes, bytes]]) -> object:
        """Return a runner that yields responses in sequence."""

        class _SeqRunner:
            def __init__(self, seq: list[tuple[int, bytes, bytes]]) -> None:
                self._seq = list(seq)
                self._idx = 0
                self.calls: list[tuple[str, ...]] = []

            async def __call__(
                self, *argv: str, cwd: str | None = None
            ) -> tuple[int, bytes, bytes]:
                self.calls.append(argv)
                if self._idx < len(self._seq):
                    result = self._seq[self._idx]
                    self._idx += 1
                    return result
                return (0, b"[]", b"")

        return _SeqRunner(responses)

    def test_rate_limit_marker_detected(self) -> None:
        """_is_rate_limit_error returns True for recognisable rate-limit messages."""
        assert GitHubClient._is_rate_limit_error(1, b"API rate limit exceeded") is True
        assert GitHubClient._is_rate_limit_error(1, b"secondary rate limit") is True
        assert GitHubClient._is_rate_limit_error(1, b"429 Too Many Requests") is True
        assert GitHubClient._is_rate_limit_error(0, b"rate limit") is False  # rc=0 means success
        assert GitHubClient._is_rate_limit_error(1, b"Repository not found") is False

    def test_retries_once_on_rate_limit_with_backoff(self) -> None:
        """Client retries after a rate-limit error and succeeds on the second attempt."""
        import json as _json

        issues_payload = _json.dumps(
            [{"number": 1, "title": "t", "body": "", "state": "OPEN", "labels": [], "url": "u"}]
        ).encode()

        runner = self._make_runner(
            [
                # First call: rate-limited
                (1, b"", b"API rate limit exceeded for installation"),
                # rate_limit API probe (returns no reset info → backoff)
                (1, b"", b""),
                # Retry of the original command: success
                (0, issues_payload, b""),
            ]
        )
        client = GitHubClient(runner=runner)
        issues = asyncio.run(client.list_issues("owner/repo"))
        assert len(issues) == 1
        assert issues[0].number == 1

    def test_raises_rate_limit_error_after_all_retries_exhausted(self) -> None:
        """GitHubRateLimitError is raised when every retry attempt is rate-limited."""
        runner = self._make_runner(
            [
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b""),  # rate_limit probe fails
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b""),  # rate_limit probe fails
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b""),  # rate_limit probe fails
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b""),  # rate_limit probe fails
            ]
        )
        client = GitHubClient(runner=runner)
        with pytest.raises(GitHubRateLimitError):
            asyncio.run(client.list_issues("owner/repo"))

    def test_non_rate_limit_error_raises_immediately(self) -> None:
        """Errors that are not rate-limit related raise GhCliError without retrying."""
        runner = self._make_runner(
            [
                (1, b"", b"Repository not found"),
            ]
        )
        client = GitHubClient(runner=runner)
        with pytest.raises(GhCliError, match="Repository not found"):
            asyncio.run(client.list_issues("owner/repo"))
        # Only one call made — no retries for non-rate-limit errors.
        assert len(runner.calls) == 1  # type: ignore[attr-defined]

    def test_rate_limit_reset_respected_when_available(self) -> None:
        """When the rate_limit API returns a reset time, the client waits for it."""
        import json as _json
        import time

        issues_payload = _json.dumps(
            [{"number": 2, "title": "x", "body": "", "state": "OPEN", "labels": [], "url": "u"}]
        ).encode()
        # Reset is 1 second in the future — well within the ceiling.
        reset_ts = int(time.time()) + 1
        rate_limit_payload = _json.dumps(
            {"resources": {"core": {"remaining": 0, "reset": reset_ts}}}
        ).encode()

        runner = self._make_runner(
            [
                # First call: rate-limited
                (1, b"", b"rate limit exceeded"),
                # rate_limit probe: returns reset timestamp
                (0, rate_limit_payload, b""),
                # Retry after sleeping: success
                (0, issues_payload, b""),
            ]
        )
        client = GitHubClient(runner=runner)
        issues = asyncio.run(client.list_issues("owner/repo"))
        assert len(issues) == 1

    def test_rate_limit_raises_when_reset_exceeds_ceiling(self) -> None:
        """GitHubRateLimitError raised when reset time exceeds the ceiling."""
        import json as _json
        import time

        # Reset is far in the future — exceeds _MAX_RATE_LIMIT_WAIT_SECONDS (120).
        reset_ts = int(time.time()) + 9999
        rate_limit_payload = _json.dumps(
            {"resources": {"core": {"remaining": 0, "reset": reset_ts}}}
        ).encode()

        runner = self._make_runner(
            [
                # First call: rate-limited
                (1, b"", b"rate limit exceeded"),
                # rate_limit probe: returns a far-future reset timestamp
                (0, rate_limit_payload, b""),
            ]
        )
        client = GitHubClient(runner=runner)
        with pytest.raises(GitHubRateLimitError):
            asyncio.run(client.list_issues("owner/repo"))

    def test_fetch_rate_limit_reset_returns_none_on_failure(self) -> None:
        """_fetch_rate_limit_reset returns None when the API call fails."""
        runner = self._make_runner([(1, b"", b"auth required")])
        client = GitHubClient(runner=runner)
        result = asyncio.run(client._fetch_rate_limit_reset())
        assert result is None

    def test_fetch_rate_limit_reset_returns_none_on_bad_json(self) -> None:
        """_fetch_rate_limit_reset returns None when output is not valid JSON."""
        runner = self._make_runner([(0, b"not json", b"")])
        client = GitHubClient(runner=runner)
        result = asyncio.run(client._fetch_rate_limit_reset())
        assert result is None


class TestPullRequestFromUrl:
    def test_from_url_raises_on_invalid_url(self) -> None:
        with pytest.raises(ValueError, match="Unrecognised PR URL"):
            PullRequest.from_url("https://github.com/owner/repo/issues/42")

    def test_from_url_extracts_number(self) -> None:
        pr = PullRequest.from_url("https://github.com/owner/repo/pull/99")
        assert pr.number == 99
        assert pr.url == "https://github.com/owner/repo/pull/99"


class TestListIssuesInvalidState:
    def test_invalid_state_raises(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        with pytest.raises(ValueError, match="state"):
            asyncio.run(client.list_issues("owner/repo", state="invalid"))


class TestCreateIssueValidation:
    def test_empty_title_raises(self, fake_runner: FakeRunner) -> None:
        client = GitHubClient(runner=fake_runner)
        with pytest.raises(ValueError, match="title"):
            asyncio.run(client.create_issue("owner/repo", title="  ", body="body"))
