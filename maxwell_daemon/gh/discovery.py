"""Discovery agent — scan issues and queue refined IssueTasks.

Closes one of the two GAAI parity gaps. Intentionally simple: list → filter
→ dispatch. Smarter refinement (story-shaping) is an LLM-time concern handled
by the issue template pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from maxwell_daemon.gh.client import Issue

__all__ = ["DiscoveryFilter", "DiscoveryResult", "discover_issues"]


@dataclass(slots=True)
class DiscoveryFilter:
    required_labels: set[str] = field(default_factory=set)
    excluded_labels: set[str] = field(default_factory=set)

    def matches(self, issue: Issue) -> bool:
        labels = set(issue.labels)
        if self.required_labels and not (self.required_labels & labels):
            return False
        return not self.excluded_labels & labels


@dataclass(slots=True, frozen=True)
class DiscoveryResult:
    repo: str
    scanned: int
    dispatched: int
    skipped: int
    task_ids: list[str]


class _GitHubProto(Protocol):
    async def list_issues(
        self, repo: str, *, state: str = "open", limit: int = 50
    ) -> list[Issue]: ...


class _DaemonProto(Protocol):
    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
    ) -> Any: ...


async def discover_issues(
    *,
    repo: str,
    github: _GitHubProto,
    daemon: _DaemonProto,
    filters: DiscoveryFilter | None = None,
    mode: str = "plan",
    state: str = "open",
    limit: int = 50,
    max_dispatch: int | None = None,
    already_dispatched: set[int] | None = None,
) -> DiscoveryResult:
    """List matching issues and dispatch them as IssueTasks on the daemon.

    :param already_dispatched: issue numbers the caller already queued; skipped
        here to avoid double-dispatch. Callers are expected to track this
        across discovery runs.
    """
    filters = filters or DiscoveryFilter()
    seen = already_dispatched or set()
    issues = await github.list_issues(repo, state=state, limit=limit)

    dispatched: list[str] = []
    skipped = 0
    for issue in issues:
        if issue.number in seen:
            skipped += 1
            continue
        if not filters.matches(issue):
            skipped += 1
            continue
        if max_dispatch is not None and len(dispatched) >= max_dispatch:
            skipped += 1
            continue
        task = daemon.submit_issue(repo=repo, issue_number=issue.number, mode=mode)
        dispatched.append(task.id)

    return DiscoveryResult(
        repo=repo,
        scanned=len(issues),
        dispatched=len(dispatched),
        skipped=skipped,
        task_ids=dispatched,
    )
