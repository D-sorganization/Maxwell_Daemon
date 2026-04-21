"""Thin async wrapper around the `gh` CLI.

Rationale: `gh` already solves auth, pagination, rate limits, and retries. We
just shape its output into typed records. The adapter is testable without a
network by injecting a `runner` that stubs subprocess.

Safety: we never pass user input through a shell. `asyncio.create_subprocess_exec`
takes argv as a list so shell meta-characters stay data, not code. Repo strings
and other bounded inputs get regex-validated before they reach the subprocess.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["GhCliError", "GitHubClient", "GitHubRateLimitError", "Issue", "PullRequest"]

log = logging.getLogger(__name__)


_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_ISSUE_FIELDS = "number,title,body,state,labels,url"

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


class GhCliError(RuntimeError):
    """Raised when a `gh` invocation exits non-zero."""


class GitHubRateLimitError(GhCliError):
    """Raised when the GitHub API rate limit is exhausted and cannot be waited out."""

    def __init__(self, message: str, *, reset_at: float | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at  # Unix timestamp when the limit resets, if known


# Exit codes emitted by `gh` when GitHub returns 403 or 429 (rate-limited).
_RATE_LIMIT_EXIT_CODES: frozenset[int] = frozenset({4})  # gh uses rc=4 for HTTP 4xx
# Error substrings that indicate a rate-limit response rather than an auth error.
_RATE_LIMIT_MARKERS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "429",
    "403",
    "secondary rate",
    "abuse detection",
)
# Maximum wall-clock seconds we are willing to sleep waiting for a reset.
_MAX_RATE_LIMIT_WAIT_SECONDS = 120.0
# Backoff schedule for transient 403/429 responses.
_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)


@dataclass(slots=True, frozen=True)
class Issue:
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    url: str

    @property
    def is_open(self) -> bool:
        return self.state.upper() == "OPEN"

    @classmethod
    def from_gh(cls, payload: dict[str, Any]) -> Issue:
        return cls(
            number=int(payload["number"]),
            title=payload.get("title", ""),
            body=payload.get("body", "") or "",
            state=payload.get("state", "OPEN"),
            labels=[label["name"] for label in payload.get("labels", [])],
            url=payload.get("url", ""),
        )


@dataclass(slots=True, frozen=True)
class PullRequest:
    number: int
    url: str
    draft: bool = False

    @classmethod
    def from_url(cls, url: str, draft: bool = False) -> PullRequest:
        match = re.search(r"/pull/(\d+)", url)
        if not match:
            raise ValueError(f"Unrecognised PR URL: {url!r}")
        return cls(number=int(match.group(1)), url=url, draft=draft)


async def _default_runner(*argv: str, cwd: str | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


class GitHubClient:
    def __init__(self, *, runner: RunnerFn | None = None) -> None:
        self._run = runner or _default_runner

    def _validate_repo(self, repo: str) -> None:
        if not _REPO_RE.match(repo):
            raise ValueError(f"Invalid repo {repo!r}: expected 'owner/name' with safe characters")

    @staticmethod
    def _is_rate_limit_error(rc: int, err: bytes) -> bool:
        """Heuristically detect a GitHub rate-limit response from `gh` output."""
        if rc == 0:
            return False
        err_text = err.decode(errors="replace").lower()
        return any(marker in err_text for marker in _RATE_LIMIT_MARKERS)

    async def _gh(self, *argv: str, cwd: str | None = None) -> bytes:
        """Run a ``gh`` sub-command, transparently retrying on rate-limit responses.

        When GitHub returns a rate-limit error (403 / 429 / secondary-rate) the
        method checks how long until the limit resets (via ``gh api rate_limit``
        if available, otherwise falls back to exponential back-off) and sleeps
        up to :data:`_MAX_RATE_LIMIT_WAIT_SECONDS` before retrying.  A
        :class:`GitHubRateLimitError` is raised only when the remaining wait
        exceeds that ceiling.
        """
        for attempt, backoff in enumerate(_BACKOFF_SECONDS):
            rc, out, err = await self._run("gh", *argv, cwd=cwd)
            if rc == 0:
                return out

            if self._is_rate_limit_error(rc, err):
                # Try to learn the reset timestamp from the rate_limit API.
                reset_at: float | None = await self._fetch_rate_limit_reset()
                if reset_at is not None:
                    wait = reset_at - time.time()
                    if wait > _MAX_RATE_LIMIT_WAIT_SECONDS:
                        raise GitHubRateLimitError(
                            f"GitHub rate limit exhausted; resets in {wait:.0f}s "
                            f"(ceiling is {_MAX_RATE_LIMIT_WAIT_SECONDS}s)",
                            reset_at=reset_at,
                        )
                    if wait > 0:
                        log.warning(
                            "GitHub rate limit hit; sleeping %.1fs until reset (attempt %d/%d)",
                            wait,
                            attempt + 1,
                            len(_BACKOFF_SECONDS),
                        )
                        await asyncio.sleep(wait)
                    continue  # retry immediately after sleeping

                # No reset info — fall back to exponential back-off.
                if attempt + 1 >= len(_BACKOFF_SECONDS):
                    raise GitHubRateLimitError(
                        f"gh {' '.join(argv)} rate-limited after {len(_BACKOFF_SECONDS)} retries: "
                        f"{err.decode(errors='replace').strip()}"
                    )
                log.warning(
                    "GitHub rate limit hit; backing off %.1fs (attempt %d/%d)",
                    backoff,
                    attempt + 1,
                    len(_BACKOFF_SECONDS),
                )
                await asyncio.sleep(backoff)
                continue

            raise GhCliError(
                f"gh {' '.join(argv)} failed (rc={rc}): {err.decode(errors='replace').strip()}"
            )

        # Exhausted all backoff attempts (should not normally be reached).
        rc, out, err = await self._run("gh", *argv, cwd=cwd)
        if rc != 0:
            raise GhCliError(
                f"gh {' '.join(argv)} failed (rc={rc}): {err.decode(errors='replace').strip()}"
            )
        return out

    async def _fetch_rate_limit_reset(self) -> float | None:
        """Ask GitHub for the rate-limit reset timestamp.

        Returns a Unix timestamp (float) when the core limit resets, or
        ``None`` if the call fails or the output is unparseable.
        """
        try:
            rc, out, _ = await self._run("gh", "api", "rate_limit")
            if rc != 0 or not out:
                return None
            data = json.loads(out)
            reset = data.get("resources", {}).get("core", {}).get("reset")
            return float(reset) if reset is not None else None
        except Exception:
            return None

    async def check_auth(self) -> bool:
        rc, _, _ = await self._run("gh", "auth", "status")
        return rc == 0

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
        self._validate_repo(repo)
        if state not in {"open", "closed", "all"}:
            raise ValueError(f"state must be one of open/closed/all, got {state!r}")
        out = await self._gh(
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            state,
            "--limit",
            str(limit),
            "--json",
            _ISSUE_FIELDS,
        )
        payload = json.loads(out) if out else []
        return [Issue.from_gh(item) for item in payload]

    async def get_issue(self, repo: str, number: int) -> Issue:
        self._validate_repo(repo)
        out = await self._gh(
            "issue",
            "view",
            str(int(number)),
            "--repo",
            repo,
            "--json",
            _ISSUE_FIELDS,
        )
        return Issue.from_gh(json.loads(out))

    async def create_issue(
        self,
        repo: str,
        *,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> str:
        self._validate_repo(repo)
        if not title.strip():
            raise ValueError("title required")
        argv = [
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
        ]
        if labels:
            argv += ["--label", ",".join(labels)]
        out = await self._gh(*argv)
        return out.decode().strip()

    async def list_branches(self, repo: str) -> list[str]:
        """Return every branch name on the remote, paginated across all pages.

        Used by the executor to decide whether a configured ``pr_target_branch``
        (e.g. ``staging``) actually exists before we base a PR on it.
        """
        self._validate_repo(repo)
        out = await self._gh("api", f"repos/{repo}/branches", "--paginate")
        payload = json.loads(out) if out else []
        return [str(b.get("name", "")) for b in payload if b.get("name")]

    async def get_default_branch(self, repo: str) -> str:
        """Return the repo's default branch (e.g. ``main`` or ``master``)."""
        self._validate_repo(repo)
        out = await self._gh("api", f"repos/{repo}")
        payload = json.loads(out) if out else {}
        branch = payload.get("default_branch")
        if not branch:
            raise GhCliError(f"repos/{repo} response missing default_branch field")
        return str(branch)

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
        self._validate_repo(repo)
        argv = [
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            argv.append("--draft")
        out = await self._gh(*argv)
        url = out.decode().strip().splitlines()[-1]
        return PullRequest.from_url(url, draft=draft)
