"""GitHubClient — thin subprocess wrapper around the `gh` CLI.

We use gh because it handles auth, rate limits, and pagination for us. Tests
stub subprocess at the boundary so nothing hits the real API.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from maxwell_daemon.gh import GhCliError, GitHubClient, Issue, PullRequest, RateLimitError
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
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b"API rate limit exceeded"),
                (1, b"", b"API rate limit exceeded"),
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

    def test_rate_limit_retry_succeeds_on_second_attempt(self) -> None:
        """Client retries after a rate-limit error and succeeds on the second attempt."""
        import json as _json

        issues_payload = _json.dumps(
            [{"number": 2, "title": "x", "body": "", "state": "OPEN", "labels": [], "url": "u"}]
        ).encode()

        runner = self._make_runner(
            [
                # First call: rate-limited
                (1, b"", b"rate limit exceeded"),
                # Retry: success
                (0, issues_payload, b""),
            ]
        )
        client = GitHubClient(runner=runner)
        issues = asyncio.run(client.list_issues("owner/repo"))
        assert len(issues) == 1

    def test_rate_limit_raises_after_max_retries_exhausted(self) -> None:
        """GitHubRateLimitError raised when all retry attempts are rate-limited."""
        runner = self._make_runner(
            [
                (1, b"", b"rate limit exceeded"),
                (1, b"", b"rate limit exceeded"),
                (1, b"", b"rate limit exceeded"),
                (1, b"", b"rate limit exceeded"),
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


# ---------------------------------------------------------------------------
# Rate-limit retry tests
# ---------------------------------------------------------------------------


class RetryRunner:
    """Runner that returns pre-configured responses in sequence for each call."""

    def __init__(self, responses: list[tuple[int, bytes, bytes]]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def __call__(self, *argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
        self.call_count += 1
        if self._responses:
            return self._responses.pop(0)
        return (0, b"", b"")


class TestRateLimitRetry:
    """Tests for _request_with_retry rate-limit handling."""

    def test_429_error_is_retried_and_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A rate-limit error followed by success returns the successful output."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        rate_limit_stderr = b"API rate limit exceeded. Retry-After: 5"
        success_stdout = (
            b'[{"number":1,"title":"t","body":"","state":"OPEN","labels":[],"url":"u"}]'
        )

        runner = RetryRunner(
            [
                (1, b"", rate_limit_stderr),  # first attempt
                (0, success_stdout, b""),  # second attempt
            ]
        )
        client = GitHubClient(runner=runner)

        result = asyncio.run(
            client._request_with_retry("issue", "list", "--repo", "owner/repo", max_retries=3)
        )

        assert result == success_stdout
        assert runner.call_count == 2
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 5  # Retry-After header value

    def test_429_exhausts_retries_raises_rate_limit_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When all retries are exhausted, RateLimitError is raised."""

        async def fake_sleep(secs: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        rate_limit_stderr = b"API rate limit exceeded for your token."
        runner = RetryRunner(
            [
                (1, b"", rate_limit_stderr),
                (1, b"", rate_limit_stderr),
                (1, b"", rate_limit_stderr),
                (1, b"", rate_limit_stderr),
            ]
        )
        client = GitHubClient(runner=runner)

        with pytest.raises(RateLimitError, match="rate limited"):
            asyncio.run(client._request_with_retry("issue", "list", max_retries=3))

        # 1 initial + 3 retries = 4 attempts total
        assert runner.call_count == 4

    def test_secondary_rate_limit_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secondary rate limit messages are also detected and retried."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        runner = RetryRunner(
            [
                (1, b"", b"You have exceeded a secondary rate limit."),
                (0, b"ok", b""),
            ]
        )
        client = GitHubClient(runner=runner)

        result = asyncio.run(client._request_with_retry("api", "repos/x/y", max_retries=2))

        assert result == b"ok"
        assert len(sleep_calls) == 1

    def test_non_rate_limit_error_is_not_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A generic failure (non rate-limit) raises GhCliError immediately without retrying."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        runner = RetryRunner(
            [
                (1, b"", b"Resource not found"),
                (0, b"should not reach here", b""),
            ]
        )
        client = GitHubClient(runner=runner)

        with pytest.raises(GhCliError):
            asyncio.run(client._request_with_retry("issue", "view", "99", max_retries=3))

        # Only 1 attempt — no retries for non-rate-limit errors.
        assert runner.call_count == 1
        assert len(sleep_calls) == 0

    def test_wait_capped_at_300_seconds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sleep duration is capped at 300 seconds even if Reset header implies longer."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # Simulate a reset timestamp far in the future (year 2100)
        future_reset = 4102444800  # 2100-01-01 epoch
        rate_stderr = f"API rate limit exceeded. X-RateLimit-Reset: {future_reset}".encode()

        runner = RetryRunner(
            [
                (1, b"", rate_stderr),
                (0, b"done", b""),
            ]
        )
        client = GitHubClient(runner=runner)

        asyncio.run(client._request_with_retry("api", "repos/x/y", max_retries=2))

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 300  # capped at 5 minutes

    def test_rate_limit_remaining_logged_from_stderr(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """X-RateLimit-Remaining in stderr output is logged at DEBUG level."""
        import logging

        async def fake_sleep(secs: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        runner = RetryRunner(
            [
                (0, b"data", b"X-RateLimit-Remaining: 42"),
            ]
        )
        client = GitHubClient(runner=runner)

        with caplog.at_level(logging.DEBUG, logger="maxwell_daemon.gh.client"):
            asyncio.run(client._request_with_retry("api", "repos/x/y", max_retries=1))

        assert any("42" in record.message for record in caplog.records)

    def test_list_issues_retries_on_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """list_issues uses _request_with_retry and therefore handles rate limits."""
        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        issues_json = json.dumps(
            [{"number": 1, "title": "t", "body": "b", "state": "OPEN", "labels": [], "url": "u"}]
        ).encode()

        runner = RetryRunner(
            [
                (1, b"", b"API rate limit exceeded"),
                (0, issues_json, b""),
            ]
        )
        client = GitHubClient(runner=runner)

        issues = asyncio.run(client.list_issues("owner/repo"))

        assert len(issues) == 1
        assert len(sleep_calls) == 1

    def test_get_issue_retries_on_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """get_issue uses _request_with_retry and therefore handles rate limits."""

        async def fake_sleep(secs: float) -> None:
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        issue_json = json.dumps(
            {"number": 7, "title": "t", "body": "b", "state": "OPEN", "labels": [], "url": "u"}
        ).encode()

        runner = RetryRunner(
            [
                (1, b"", b"secondary rate limit"),
                (0, issue_json, b""),
            ]
        )
        client = GitHubClient(runner=runner)

        issue = asyncio.run(client.get_issue("owner/repo", 7))
        assert issue.number == 7

    def test_rate_limit_error_is_subclass_of_gh_cli_error(self) -> None:
        """RateLimitError is a subclass of GhCliError for backwards compatibility."""
        err = RateLimitError("exhausted")
        assert isinstance(err, GhCliError)
        assert isinstance(err, RateLimitError)
