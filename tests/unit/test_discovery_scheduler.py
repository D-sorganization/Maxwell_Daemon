"""Tests for DiscoveryScheduler — the cron-driven fleet issue discoverer.

Scheduler polls every configured repo on an interval, dedupes against an
already-dispatched set, and submits new stories to the daemon.

All time is simulated via an injected clock to keep tests deterministic.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
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
    async def test_dispatches_new_issues_from_one_repo(self, tmp_path: Path) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"]), _issue(2, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            interval_seconds=60,
            dedup_path=tmp_path / "dedup.json",
        )
        tick = await sched.run_once()
        assert tick.dispatched == 2
        assert [s["issue_number"] for s in daemon.submitted] == [1, 2]

    async def test_label_filter_drops_non_matching(self, tmp_path: Path) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"]), _issue(2, labels=["other"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=tmp_path / "dedup.json",
        )
        tick = await sched.run_once()
        assert tick.dispatched == 1
        assert daemon.submitted[0]["issue_number"] == 1

    async def test_already_dispatched_is_skipped_next_run(self, tmp_path: Path) -> None:
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=tmp_path / "dedup.json",
        )
        first = await sched.run_once()
        second = await sched.run_once()
        assert first.dispatched == 1
        assert second.dispatched == 0
        assert len(daemon.submitted) == 1

    async def test_fans_out_across_multiple_repos(self, tmp_path: Path) -> None:
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
            dedup_path=tmp_path / "dedup.json",
        )
        tick = await sched.run_once()
        assert tick.dispatched == 2
        submitted_repos = {s["repo"] for s in daemon.submitted}
        assert submitted_repos == {"a/b", "c/d"}

    async def test_broken_repo_does_not_kill_tick(self, tmp_path: Path) -> None:
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
            dedup_path=tmp_path / "dedup.json",
        )
        tick = await sched.run_once()
        # Broken repo didn't prevent ok/repo from dispatching.
        assert tick.dispatched == 1
        assert daemon.submitted[0]["repo"] == "ok/repo"

    async def test_dedup_persisted_to_disk(self, tmp_path: Path) -> None:
        """Dispatched issue numbers are written to the dedup file (#149)."""
        dedup_file = tmp_path / "dedup.json"
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"]), _issue(2, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        await sched.run_once()
        assert dedup_file.exists(), "dedup file must be written after dispatch"
        persisted = json.loads(dedup_file.read_text())
        assert set(persisted.get("a/b", [])) == {1, 2}

    async def test_dedup_loaded_on_restart(self, tmp_path: Path) -> None:
        """A new scheduler instance loads persisted dedup and skips already-seen issues (#149)."""
        dedup_file = tmp_path / "dedup.json"
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})

        # First "run" — dispatches issue 1 and persists the dedup file.
        sched1 = DiscoveryScheduler(
            github=gh,
            daemon=_FakeDaemon(),
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        await sched1.run_once()

        # Simulate a restart: create a fresh scheduler with the same dedup file.
        daemon2 = _FakeDaemon()
        sched2 = DiscoveryScheduler(
            github=gh,
            daemon=daemon2,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        tick = await sched2.run_once()
        assert tick.dispatched == 0, "issue already dispatched in previous run must not re-dispatch"
        assert len(daemon2.submitted) == 0


# ── Start/stop lifecycle ─────────────────────────────────────────────────────


class TestLifecycle:
    async def test_start_schedules_periodic_ticks(self, tmp_path: Path) -> None:
        """Start the scheduler, wait long enough for 1-2 ticks, stop, verify calls."""
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            interval_seconds=0.01,  # tiny interval for test
            dedup_path=tmp_path / "dedup.json",
        )
        await sched.start()
        await asyncio.sleep(0.05)
        await sched.stop()
        # First tick dispatches; subsequent ticks see it already dispatched.
        assert daemon.submitted
        assert daemon.submitted[0]["issue_number"] == 1

    async def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        sched = DiscoveryScheduler(
            github=_FakeGitHub({}),
            daemon=_FakeDaemon(),
            repos=[],
            interval_seconds=60,
            dedup_path=tmp_path / "dedup.json",
        )
        await sched.stop()  # Safe even if never started.
        await sched.stop()  # Double-stop must not explode.

    async def test_start_twice_is_idempotent(self, tmp_path: Path) -> None:
        sched = DiscoveryScheduler(
            github=_FakeGitHub({}),
            daemon=_FakeDaemon(),
            repos=[],
            interval_seconds=0.01,
            dedup_path=tmp_path / "dedup.json",
        )
        await sched.start()
        await sched.start()  # second start is a no-op, not an error
        await sched.stop()


# ── Preconditions ────────────────────────────────────────────────────────────


class TestPreconditions:
    def test_rejects_zero_interval(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="interval"):
            DiscoveryScheduler(
                github=_FakeGitHub({}),
                daemon=_FakeDaemon(),
                repos=[],
                interval_seconds=0,
                dedup_path=tmp_path / "dedup.json",
            )

    def test_rejects_negative_interval(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="interval"):
            DiscoveryScheduler(
                github=_FakeGitHub({}),
                daemon=_FakeDaemon(),
                repos=[],
                interval_seconds=-1,
                dedup_path=tmp_path / "dedup.json",
            )


# ── Dedup persistence edge cases ────────────────────────────────────────────


class TestDedupPersistence:
    async def test_dedup_file_not_saved_when_nothing_dispatched(self, tmp_path: Path) -> None:
        """Dedup file is only written when something is actually dispatched."""
        dedup_file = tmp_path / "dedup.json"
        gh = _FakeGitHub({"a/b": []})  # no issues
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        tick = await sched.run_once()
        assert tick.dispatched == 0
        # Dedup file is NOT written when nothing was dispatched.
        assert not dedup_file.exists()

    async def test_dedup_file_unreadable_starts_fresh(self, tmp_path: Path) -> None:
        """A corrupt dedup file causes the scheduler to start with empty dedup."""
        dedup_file = tmp_path / "dedup.json"
        dedup_file.write_text("this is not valid json {{{")
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        tick = await sched.run_once()
        # Corrupt file → start fresh → issue 1 is dispatched
        assert tick.dispatched == 1

    async def test_dedup_path_none_uses_default_path(self, tmp_path: Path) -> None:
        """When dedup_path is None, the scheduler uses the default path (not None internally)."""
        from maxwell_daemon.daemon.scheduler import _DEFAULT_DEDUP_PATH

        gh = _FakeGitHub({})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[],
            dedup_path=None,
        )
        # dedup_path=None maps to _DEFAULT_DEDUP_PATH internally
        assert sched._dedup_path == _DEFAULT_DEDUP_PATH

    async def test_save_dedup_logs_on_failure(self, tmp_path: Path) -> None:
        """_save_dedup swallows exceptions when write fails."""
        import unittest.mock as mock

        dedup_file = tmp_path / "dedup.json"
        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            dedup_path=dedup_file,
        )
        # Patch write_text to raise an OSError — _save_dedup must swallow it
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            # Should not raise
            tick = await sched.run_once()
        assert isinstance(tick, DiscoveryTick)


class TestSchedulerLoop:
    async def test_stop_with_running_task_cancels_cleanly(self, tmp_path: Path) -> None:
        """stop() with a long-interval task causes the task to be cancelled without error."""
        gh = _FakeGitHub({})
        sched = DiscoveryScheduler(
            github=gh,
            daemon=_FakeDaemon(),
            repos=[],
            interval_seconds=100.0,  # very long — will never tick before stop
            jitter=False,
            dedup_path=tmp_path / "dedup.json",
        )
        await sched.start()
        # stop() should cancel the background task cleanly
        await sched.stop()
        assert sched._task is None

    async def test_loop_continues_after_run_once_exception(self, tmp_path: Path) -> None:
        """Exceptions from run_once in _loop are swallowed — the loop keeps running."""
        import unittest.mock as mock

        gh = _FakeGitHub({"a/b": [_issue(1, labels=["deliver"])]})
        daemon = _FakeDaemon()
        sched = DiscoveryScheduler(
            github=gh,
            daemon=daemon,
            repos=[DiscoveryRepoSpec(repo="a/b", labels={"deliver"})],
            interval_seconds=0.01,
            jitter=False,
            dedup_path=tmp_path / "dedup.json",
        )
        # Make run_once raise on the first call, succeed on subsequent
        call_count = 0
        original_run_once = sched.run_once

        async def patched_run_once() -> DiscoveryTick:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated first-tick failure")
            return await original_run_once()

        sched.run_once = patched_run_once  # type: ignore[method-assign]
        await sched.start()
        await asyncio.sleep(0.08)
        await sched.stop()
        # The loop continued past the first exception
        assert call_count >= 2
