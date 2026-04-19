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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["GhCliError", "GitHubClient", "Issue", "PullRequest"]


_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_ISSUE_FIELDS = "number,title,body,state,labels,url"

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


class GhCliError(RuntimeError):
    """Raised when a `gh` invocation exits non-zero."""


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

    async def _gh(self, *argv: str, cwd: str | None = None) -> bytes:
        rc, out, err = await self._run("gh", *argv, cwd=cwd)
        if rc != 0:
            raise GhCliError(
                f"gh {' '.join(argv)} failed (rc={rc}): {err.decode(errors='replace').strip()}"
            )
        return out

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
