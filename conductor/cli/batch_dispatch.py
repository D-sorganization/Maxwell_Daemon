"""Pure-logic planner for ``conductor issue dispatch-batch``.

The CLI wraps this with console rendering and the HTTP call to the daemon;
**this module has no HTTP or I/O side effects** so it can be unit-tested
without a daemon or a network. That's the TDD + LOD split: the CLI uses
the planner; the planner uses an injected issue-lister; the lister uses
``gh``. One concern per layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from conductor.config.fleet import FleetManifest
from conductor.contracts import require
from conductor.gh.client import Issue

__all__ = [
    "BatchDispatchPlan",
    "BatchDispatchPlanner",
    "BatchItem",
    "RepoBatchSummary",
    "resolve_repos_from_manifest",
]


#: The issue-lister injected into the planner. Same shape as
#: ``GitHubClient.list_issues``.
ListIssuesFn = Callable[..., Awaitable[list[Issue]]]

_VALID_MODES = frozenset({"plan", "implement"})


@dataclass(slots=True, frozen=True)
class BatchItem:
    """One issue dispatch request that the daemon will consume."""

    repo: str
    number: int
    mode: str


@dataclass(slots=True, frozen=True)
class RepoBatchSummary:
    """Per-repo rollup of the plan — used for human-readable CLI output."""

    repo: str
    eligible: int
    submitted: int
    skipped: int


@dataclass(slots=True, frozen=True)
class BatchDispatchPlan:
    """What ``BatchDispatchPlanner.plan`` produces — ready to print or submit."""

    items: tuple[BatchItem, ...]
    summaries: tuple[RepoBatchSummary, ...]

    def total_submitted(self) -> int:
        return sum(s.submitted for s in self.summaries)

    def total_skipped(self) -> int:
        return sum(s.skipped for s in self.summaries)


class BatchDispatchPlanner:
    """Compute a :class:`BatchDispatchPlan` for a set of repos.

    The planner enforces two policies:
      * **Label filter** — keep only issues whose labels include ``label``.
      * **Per-repo cap** — ``max_stories`` issues per repo, preserving the
        order returned by the lister (latest-created first by default).

    Fan-out uses ``asyncio.gather`` so N repos are scanned concurrently.
    Per-repo lister failures surface as an empty summary rather than
    killing the batch — one broken repo shouldn't strand the rest.
    """

    def __init__(
        self,
        *,
        list_issues: ListIssuesFn,
        max_stories: int | None = None,
    ) -> None:
        if max_stories is not None:
            require(max_stories >= 1, f"max_stories must be >= 1 (got {max_stories})")
        self._list_issues = list_issues
        self._max_stories = max_stories

    async def plan(
        self,
        *,
        repos: Sequence[str],
        label: str | None = None,
        mode: str = "plan",
        state: str = "open",
        limit: int = 100,
    ) -> BatchDispatchPlan:
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {_VALID_MODES} (got {mode!r})")
        if not repos:
            return BatchDispatchPlan(items=(), summaries=())

        per_repo_results = await asyncio.gather(
            *[
                self._plan_one_repo(r, label=label, mode=mode, state=state, limit=limit)
                for r in repos
            ]
        )

        items: list[BatchItem] = []
        summaries: list[RepoBatchSummary] = []
        for repo_items, summary in per_repo_results:
            items.extend(repo_items)
            summaries.append(summary)
        return BatchDispatchPlan(items=tuple(items), summaries=tuple(summaries))

    async def _plan_one_repo(
        self,
        repo: str,
        *,
        label: str | None,
        mode: str,
        state: str,
        limit: int,
    ) -> tuple[list[BatchItem], RepoBatchSummary]:
        try:
            issues = await self._list_issues(repo, state=state, limit=limit)
        except Exception:
            return [], RepoBatchSummary(repo=repo, eligible=0, submitted=0, skipped=0)

        filtered = [i for i in issues if label is None or label in i.labels]
        eligible = len(filtered)

        cap = self._max_stories
        capped = filtered if cap is None else filtered[:cap]
        submitted = len(capped)
        skipped = eligible - submitted

        items = [BatchItem(repo=repo, number=i.number, mode=mode) for i in capped]
        return items, RepoBatchSummary(
            repo=repo, eligible=eligible, submitted=submitted, skipped=skipped
        )


def resolve_repos_from_manifest(manifest: FleetManifest) -> list[str]:
    """Expand ``--all`` into the list of enabled repos as ``owner/name`` strings."""
    return [entry.full_name for entry in manifest.active_repos()]
