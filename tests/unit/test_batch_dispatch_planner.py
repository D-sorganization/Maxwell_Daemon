"""Tests for BatchDispatchPlanner — pure logic for multi-repo issue dispatch.

Planner is pure: given a list of repos, an issue-lister, and filter knobs,
it computes what would be submitted without touching the daemon. The CLI
wraps it with HTTP/console rendering.

All tests work with an in-memory `list_issues` callable — no network.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from maxwell_daemon.cli.batch_dispatch import (
    BatchDispatchPlan,
    BatchDispatchPlanner,
    BatchItem,
    RepoBatchSummary,
)
from maxwell_daemon.gh.client import Issue


def _issue(number: int, *, labels: Sequence[str] = (), title: str = "t", body: str = "") -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        state="OPEN",
        labels=list(labels),
        url=f"https://example/issues/{number}",
    )


class _FakeLister:
    """Test double — returns canned issues per repo."""

    def __init__(self, per_repo: dict[str, list[Issue]]) -> None:
        self._per_repo = per_repo
        self.calls: list[str] = []

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
        self.calls.append(repo)
        return list(self._per_repo.get(repo, []))


# ── Basic shape ──────────────────────────────────────────────────────────────


class TestBatchItemShape:
    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        item = BatchItem(repo="a/b", number=1, mode="plan")
        with pytest.raises(FrozenInstanceError):
            item.number = 2  # type: ignore[misc]

    def test_frozen_summary(self) -> None:
        from dataclasses import FrozenInstanceError

        s = RepoBatchSummary(repo="a/b", eligible=5, submitted=3, skipped=2)
        with pytest.raises(FrozenInstanceError):
            s.submitted = 4  # type: ignore[misc]


# ── Single-repo plan ────────────────────────────────────────────────────────


class TestSingleRepoPlan:
    async def test_all_issues_submitted_when_no_cap(self) -> None:
        lister = _FakeLister({"a/b": [_issue(1), _issue(2), _issue(3)]})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=["a/b"], mode="plan")
        assert [it.number for it in plan.items] == [1, 2, 3]
        assert plan.summaries == (RepoBatchSummary(repo="a/b", eligible=3, submitted=3, skipped=0),)

    async def test_max_stories_caps_submission(self) -> None:
        lister = _FakeLister({"a/b": [_issue(i) for i in range(5)]})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues, max_stories=2)
        plan = await planner.plan(repos=["a/b"])
        assert len(plan.items) == 2
        s = plan.summaries[0]
        assert s.eligible == 5
        assert s.submitted == 2
        assert s.skipped == 3

    async def test_label_filter_reduces_eligible(self) -> None:
        lister = _FakeLister(
            {
                "a/b": [
                    _issue(1, labels=["bug"]),
                    _issue(2, labels=["feature"]),
                    _issue(3, labels=["bug", "priority"]),
                ]
            }
        )
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=["a/b"], label="bug")
        assert sorted(it.number for it in plan.items) == [1, 3]
        assert plan.summaries[0].eligible == 2

    async def test_mode_is_threaded_into_items(self) -> None:
        lister = _FakeLister({"a/b": [_issue(1)]})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=["a/b"], mode="implement")
        assert plan.items[0].mode == "implement"


# ── Multi-repo fan-out ───────────────────────────────────────────────────────


class TestMultiRepoFanOut:
    async def test_fans_out_across_repos(self) -> None:
        lister = _FakeLister(
            {
                "a/b": [_issue(1), _issue(2)],
                "c/d": [_issue(10)],
            }
        )
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=["a/b", "c/d"])
        assert len(plan.items) == 3
        names = {it.repo for it in plan.items}
        assert names == {"a/b", "c/d"}
        assert len(plan.summaries) == 2
        # Planner calls list_issues once per repo.
        assert sorted(lister.calls) == ["a/b", "c/d"]

    async def test_per_repo_cap_independent(self) -> None:
        """A cap of 2 applies per repo, not across the fleet."""
        lister = _FakeLister(
            {
                "a/b": [_issue(1), _issue(2), _issue(3)],
                "c/d": [_issue(10), _issue(20), _issue(30)],
            }
        )
        planner = BatchDispatchPlanner(list_issues=lister.list_issues, max_stories=2)
        plan = await planner.plan(repos=["a/b", "c/d"])
        # 2 per repo = 4 total, not 2 total.
        assert len(plan.items) == 4
        by_repo = {s.repo: s for s in plan.summaries}
        assert by_repo["a/b"].submitted == 2
        assert by_repo["c/d"].submitted == 2

    async def test_empty_repo_yields_empty_summary(self) -> None:
        lister = _FakeLister({"a/b": [_issue(1)], "empty/repo": []})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=["a/b", "empty/repo"])
        by_repo = {s.repo: s for s in plan.summaries}
        assert by_repo["empty/repo"].eligible == 0
        assert by_repo["empty/repo"].submitted == 0

    async def test_no_repos_yields_empty_plan(self) -> None:
        lister = _FakeLister({})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        plan = await planner.plan(repos=[])
        assert plan == BatchDispatchPlan(items=(), summaries=())

    async def test_lister_failure_surfaces_as_empty_summary(self) -> None:
        async def bad_lister(repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
            if repo == "broken/repo":
                raise RuntimeError("boom")
            return [_issue(1)]

        planner = BatchDispatchPlanner(list_issues=bad_lister)
        plan = await planner.plan(repos=["ok/repo", "broken/repo"])
        by_repo = {s.repo: s for s in plan.summaries}
        # Broken repo surfaces as an empty summary rather than killing the whole batch.
        assert by_repo["broken/repo"].eligible == 0
        assert by_repo["broken/repo"].submitted == 0
        assert by_repo["ok/repo"].submitted == 1


# ── Preconditions ────────────────────────────────────────────────────────────


class TestPreconditions:
    async def test_invalid_mode_rejected(self) -> None:
        lister = _FakeLister({})
        planner = BatchDispatchPlanner(list_issues=lister.list_issues)
        with pytest.raises(ValueError, match="mode"):
            await planner.plan(repos=["a/b"], mode="destroy")

    def test_invalid_max_stories_rejected(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        lister = _FakeLister({})
        with pytest.raises(PreconditionError, match="max_stories"):
            BatchDispatchPlanner(list_issues=lister.list_issues, max_stories=0)


# ── Fleet manifest resolution ───────────────────────────────────────────────


class TestResolveReposFromManifest:
    def test_all_active_repos_included(self) -> None:
        from maxwell_daemon.cli.batch_dispatch import resolve_repos_from_manifest
        from maxwell_daemon.config.fleet import FleetManifest

        manifest = FleetManifest.model_validate(
            {
                "version": 1,
                "fleet": {"name": "f"},
                "repos": [
                    {"name": "A", "org": "acme"},
                    {"name": "B", "org": "acme", "enabled": False},
                    {"name": "C", "org": "acme"},
                ],
            }
        )
        resolved = resolve_repos_from_manifest(manifest)
        # Disabled repos excluded.
        assert sorted(resolved) == ["acme/A", "acme/C"]
