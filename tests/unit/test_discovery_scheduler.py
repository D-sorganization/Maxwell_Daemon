"""Tests for DiscoveryScheduler — the cron-driven fleet issue discoverer.

Scheduler polls every configured repo on an interval, dedupes against an
already-dispatched set, and submits new stories to the daemon.

All time is simulated via an injected clock to keep tests deterministic.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from maxwell_daemon.daemon.scheduler import (
    DiscoveryRepoSpec,
    DiscoveryScheduler,
    DiscoveryTick,
)
from maxwell_daemon.gh.client import Issue


# ── Helpers ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeDaemon:
    """Records every submit_issue call."""

    submitted: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.submitted = []

    def submit_issue(
        self,
        *,
        repo: str,
        issue_number: int,
        mode: str = "plan",
        backend: str | None = None,
        model: str | None = None,
    ) -> Any:
        task_id = f"task-{len(self.submitted)}"
        self.submitted.append(
            {"id": task_id, "repo": repo, "issue_number": issue_number, "mode": mode}
        )

        class _Task:
            id = task_id

        return _Task


class _FakeGitHub:
    def __init__(self, canned: dict[str, list[Issue]]) -> None:
        self._canned = canned

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Issue]:
        return list(self._canned.get(repo, []))


def _issue(number: int, *, labels: list[str] | None = None) -> Issue:
    return Issue(
        number=number,
        title=f"t{number}",
        body="",
        state="OPEN",
        labels=labels or [],
        url=f"https://example/issues/{number}",
    )


# ── Data classes ─────────────────────────────────────────────────────────────


class TestShapes:
    def test_discovery_tick_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        tick = DiscoveryTick(scanned=3, dispatched=1, skipped=2, repos=("a/b",))
        with pytest.raises(FrozenInstanceError):
            tick.scanned = 5  # type: ignore[misc]


# ── Single-tick behaviour ────────────────────────────────────────────────────


class TestRunOnce:
    async def test_dispatches_new_issues_from_one_repo(self) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"]), _issue(2, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            interval_seconds=60,
        )
        tick = await sched.run_once()
        assert tick.dispatched == 2
        assert [s["issue_number"] for s in daemon.submitted] == [1, 2]

    async def test_label_filter_drops_non_matching(self) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"]), _issue(2, labels=["other"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
        )
        tick = await sched.run_once()
        assert tick.dispatched == 1
        assert daemon.submitted[0]["issue_number"] == 1

    async def test_already_dispatched_is_skipped_next_run(self) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
        )
        first = await sched.run_once()
        second = await sched.run_once()
        assert first.dispatched == 1
        assert second.dispatched == 0
        assert len(daemon.submitted) == 1

    async def test_fans_out_across_multiple_repos(self) -> None:
        gh = _FakeGitHub(
            {
                "a/b": [_issue(1, labels=["deliver"])],
                "c/d": [_issue(10, labels=["deliver"])],
            }
        )
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[
                DiscoveryRepoSpec(repo="a/b", labels={"deliver"}),
                DiscoveryRepoSpec(repo="c/d", labels={"deliver"}),
            ],
        )
        tick = await sched.run_once()
        assert tick.dispatched == 2
        submitted_repos = {s["repo"] for s in daemon.submitted}
        assert submitted_repos == {"a/b", "c/d"}

    async def test_broken_repo_does_not_kill_tick(self) -> None:
        class _Flaky:
            async def list_issues(
                self, repo: str, *, state: str = "open", limit: int = 50
            ) -> list[Issue]:
                if repo == "bad/repo":
                    raise RuntimeError("boom")
                return [_issue(1, labels=["deliver"])]

        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=_Flaky(),
            daemon=daemon,
            repos=[
                DiscoveryRepoSpec(repo="bad/repo", labels={"deliver"}),
                DiscoveryRepoSpec(repo="ok/repo", labels={"deliver"}),
            ],
        )
        tick = await sched.run_once()
        # Broken repo didn't prevent ok/repo from dispatching.
        assert tick.dispatched == 1
        assert daemon.submitted[0]["repo"] == "ok/repo"


# ── Start/stop lifecycle ─────────────────────────────────────────────────────


class TestLifecycle:
    async def test_start_schedules_periodic_ticks(self) -> None:
        """Start the scheduler, wait long enough for 1-2 ticks, stop, verify calls."""
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            interval_seconds=0.01,  # tiny interval for test
        )
        await sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()
        # First tick dispatches; subsequent ticks see it already dispatched.
        assert daemon.submitted
        assert daemon.submitted[0]["issue_number"] == 1

    async def test_stop_is_idempotent(self) -> None:
        sched = DiscoveryScheduler(
            github=_FakeGitHub({}),
            daemon=_FakeDaemon(),
            repos=[],
            interval_seconds=60,
        )
        await sched.stop()  # Safe even if never started.
        await sched.stop()  # Double-stop must not explode.

    async def test_start_twice_is_idempotent(self) -> None:
        sched = DiscoveryScheduler(
            github=_FakeGitHub({}),
            daemon=_FakeDaemon(),
            repos=[],
            interval_seconds=0.01,
        )
        await sched.start()
        await sched.start()  # second start is a no-op, not an error
        await sched.stop()


# ── Preconditions ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_rejects_zero_interval(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="interval"):
            DiscoveryScheduler(
                github=_FakeGitHub({}),
                daemon=_FakeDaemon(),
                repos=[],
                interval_seconds=0,
            )

    def test_rejects_negative_interval(self) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="interval"):
            DiscoveryScheduler(
                github=_FakeGitHub({}),
                daemon=_FakeDaemon(),
                repos=[],
                interval_seconds=-1,
            )
