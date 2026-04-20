"""Discovery agent — scan a repo's open issues and queue IssueTasks."""

from __future__ import annotations

import asyncio
from typing import Any

from maxwell_daemon.gh import Issue
from maxwell_daemon.gh.discovery import (
    DiscoveryFilter,
    DiscoveryResult,
    discover_issues,
)


class _FakeGH:
    def __init__(self, issues: list[Issue]) -> None:
        self._issues = issues
        self.list_calls: list[dict[str, Any]] = []

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
        self.list_calls.append({"repo": repo, "state": state, "limit": limit})
        return self._issues


class _FakeDaemon:
    def __init__(self) -> None:
        self.dispatches: list[dict[str, Any]] = []

    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
    ) -> Any:
        self.dispatches.append({"repo": repo, "number": issue_number, "mode": mode})

        class _T:
            id = f"task-{len(self.dispatches)}"

        return _T()


def _issue(*, number: int, title: str = "t", labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=title,
        body="",
        state="OPEN",
        labels=list(labels or []),
        url=f"https://github.com/o/r/issues/{number}",
    )


class TestDiscoveryFilter:
    def test_no_filter_accepts_all(self) -> None:
        f = DiscoveryFilter()
        assert f.matches(_issue(number=1, labels=[])) is True

    def test_label_required(self) -> None:
        f = DiscoveryFilter(required_labels={"triage"})
        assert f.matches(_issue(number=1, labels=["triage"])) is True
        assert f.matches(_issue(number=2, labels=["bug"])) is False

    def test_label_excluded(self) -> None:
        f = DiscoveryFilter(excluded_labels={"wontfix"})
        assert f.matches(_issue(number=1, labels=["bug"])) is True
        assert f.matches(_issue(number=2, labels=["wontfix"])) is False

    def test_both_constraints(self) -> None:
        f = DiscoveryFilter(required_labels={"bug"}, excluded_labels={"blocked"})
        assert f.matches(_issue(number=1, labels=["bug"])) is True
        assert f.matches(_issue(number=2, labels=["bug", "blocked"])) is False


class TestDiscover:
    def test_dispatches_all_when_no_filter(self) -> None:
        gh = _FakeGH([_issue(number=1), _issue(number=2)])
        daemon = _FakeDaemon()
        result = asyncio.run(discover_issues(repo="o/r", github=gh, daemon=daemon))
        assert result.scanned == 2
        assert result.dispatched == 2

    def test_respects_filter(self) -> None:
        gh = _FakeGH(
            [
                _issue(number=1, labels=["triage"]),
                _issue(number=2, labels=["bug"]),
                _issue(number=3, labels=["triage", "blocked"]),
            ]
        )
        daemon = _FakeDaemon()
        result = asyncio.run(
            discover_issues(
                repo="o/r",
                github=gh,
                daemon=daemon,
                filters=DiscoveryFilter(required_labels={"triage"}, excluded_labels={"blocked"}),
            )
        )
        assert result.scanned == 3
        assert result.dispatched == 1
        assert daemon.dispatches[0]["number"] == 1

    def test_mode_threaded_through(self) -> None:
        gh = _FakeGH([_issue(number=5)])
        daemon = _FakeDaemon()
        asyncio.run(discover_issues(repo="o/r", github=gh, daemon=daemon, mode="implement"))
        assert daemon.dispatches[0]["mode"] == "implement"

    def test_deduplicates_already_dispatched(self) -> None:
        """Don't double-dispatch if the caller passes known-seen issue numbers."""
        gh = _FakeGH([_issue(number=1), _issue(number=2)])
        daemon = _FakeDaemon()
        result = asyncio.run(
            discover_issues(repo="o/r", github=gh, daemon=daemon, already_dispatched={1})
        )
        assert result.scanned == 2
        assert result.dispatched == 1
        assert daemon.dispatches[0]["number"] == 2

    def test_respects_dispatch_cap(self) -> None:
        gh = _FakeGH([_issue(number=i) for i in range(10)])
        daemon = _FakeDaemon()
        result = asyncio.run(discover_issues(repo="o/r", github=gh, daemon=daemon, max_dispatch=3))
        assert result.dispatched == 3


class TestResult:
    def test_shape(self) -> None:
        r = DiscoveryResult(
            repo="o/r", scanned=5, dispatched=3, skipped=2, task_ids=["t1", "t2", "t3"]
        )
        assert r.scanned == 5 and r.dispatched == 3
