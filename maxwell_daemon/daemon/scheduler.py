"""Periodic fleet issue discovery — the "always on" half of GAAI parity.

GAAI's discovery daemon polls every tracked repo every N minutes and queues
new stories. This scheduler is the equivalent for Maxwell-Daemon: given a list
of :class:`DiscoveryRepoSpec` entries and a daemon facade, it runs
``discover_issues`` on a timer and dedupes against issues already
submitted.

DbC:
  * interval_seconds must be positive.
  * start/stop are idempotent — calling start twice or stop-before-start
    is a no-op, not an error.

LOD:
  * Scheduler depends on a GitHub lister and a daemon facade (two
    protocols). It never reaches through them to SQLite or the LLM SDK.
  * Per-repo failures don't abort a tick — one broken repo must not
    strand the whole fleet.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from typing import Any

from maxwell_daemon.contracts import require
from maxwell_daemon.gh.discovery import DiscoveryFilter, discover_issues

__all__ = [
    "DiscoveryRepoSpec",
    "DiscoveryScheduler",
    "DiscoveryTick",
]

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class DiscoveryRepoSpec:
    """One repo the scheduler should poll."""

    repo: str
    labels: frozenset[str] = field(default_factory=frozenset)
    mode: str = "plan"


@dataclass(slots=True, frozen=True)
class DiscoveryTick:
    """Summary of one scheduler pass — what happened across all repos."""

    scanned: int
    dispatched: int
    skipped: int
    repos: tuple[str, ...]


class DiscoveryScheduler:
    """Runs :func:`discover_issues` across every repo on an interval.

    Deduplication persists *in-memory* across ticks within one scheduler
    lifetime. Restart loses the set; the daemon's durable task store is
    the second line of defence (already-active tasks won't re-enqueue).

    The ``start()``/``stop()`` pair wraps the background task. Exceptions
    from any one tick are logged and swallowed so a transient outage
    doesn't kill the scheduler.
    """

    def __init__(
        self,
        *,
        github: Any,
        daemon: Any,
        repos: list[DiscoveryRepoSpec],
        interval_seconds: float = 300.0,
        jitter: bool = True,
    ) -> None:
        require(
            interval_seconds > 0,
            f"DiscoveryScheduler: interval_seconds must be > 0 (got {interval_seconds})",
        )
        self._github = github
        self._daemon = daemon
        self._repos = list(repos)
        self._interval = float(interval_seconds)
        self._jitter = jitter
        # Per-repo set of issue numbers we've seen in any prior tick.
        self._dispatched: dict[str, set[int]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    async def run_once(self) -> DiscoveryTick:
        """Poll every configured repo exactly once and submit new issues."""
        total_scanned = total_dispatched = total_skipped = 0
        repo_names: list[str] = []

        for spec in self._repos:
            repo_names.append(spec.repo)
            seen = self._dispatched.setdefault(spec.repo, set())
            try:
                # Snapshot the current issue list so we can update dedup after
                # ``discover_issues`` completes. Two calls is acceptable here
                # — list_issues is cheap and the alternative would be
                # threading a mutable out-param through discover_issues.
                current_issues = await self._github.list_issues(spec.repo, state="open", limit=50)
            except Exception:
                log.warning("discovery list failed for repo=%s", spec.repo, exc_info=True)
                continue

            try:
                result = await discover_issues(
                    repo=spec.repo,
                    github=_SnapshotLister(current_issues),
                    daemon=self._daemon,
                    filters=DiscoveryFilter(required_labels=set(spec.labels)),
                    mode=spec.mode,
                    already_dispatched=seen,
                )
            except Exception:
                log.warning("discovery tick failed for repo=%s", spec.repo, exc_info=True)
                continue

            total_scanned += result.scanned
            total_dispatched += result.dispatched
            total_skipped += result.skipped

        return DiscoveryTick(
            scanned=total_scanned,
            dispatched=total_dispatched,
            skipped=total_skipped,
            repos=tuple(repo_names),
        )

    async def start(self) -> None:
        """Begin periodic discovery in a background task."""
        if self._task is not None and not self._task.done():
            return  # idempotent
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="discovery-scheduler")

    async def stop(self) -> None:
        """Signal the loop to exit and wait for it. Idempotent."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (TimeoutError, asyncio.TimeoutError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
            self._task = None
        self._stop_event = None

    async def _loop(self) -> None:
        assert self._stop_event is not None
        # Startup jitter: spread first-tick firing across the interval to
        # prevent thundering herd when multiple daemons start together.
        if self._jitter:
            jitter_delay = random.uniform(0, self._interval)
            log.debug("discovery scheduler startup jitter=%.2fs", jitter_delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=jitter_delay)
                return  # stop signalled during jitter delay
            except (TimeoutError, asyncio.TimeoutError):
                pass  # jitter elapsed, proceed to first tick
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception:
                log.warning("discovery tick raised; continuing", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                return  # stop signalled during the wait
            except (TimeoutError, asyncio.TimeoutError):
                continue  # interval elapsed; next tick


class _SnapshotLister:
    """Adapts a pre-fetched issue list to the protocol ``discover_issues`` expects.

    ``discover_issues`` calls ``github.list_issues(...)`` internally; we
    want to pass it the list we already pulled (so we don't make two
    network calls per repo per tick).
    """

    def __init__(self, issues: list[Any]) -> None:
        self._issues = issues

    async def list_issues(self, repo: str, *, state: str = "open", limit: int = 50) -> list[Any]:
        return self._issues
