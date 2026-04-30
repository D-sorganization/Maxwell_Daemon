"""Cron-based task scheduler.

Parses standard 5-field cron expressions and enqueues tasks on schedule via
the daemon's ``submit`` method.  Intentionally self-contained: uses only the
stdlib ``datetime`` module to avoid mandatory third-party dependencies, but
will delegate to ``croniter`` if it is installed for more exotic expressions.

Cron field layout (all 1-indexed; ``*`` means "any"):

    ┌───────────── minute        (0-59)
    │ ┌─────────── hour          (0-23)
    │ │ ┌───────── day-of-month  (1-31)
    │ │ │ ┌─────── month         (1-12)
    │ │ │ │ ┌───── day-of-week   (0-6, Sunday=0)
    │ │ │ │ │
    * * * * *

Only single values, ``*``, and ``*/step`` are supported in the built-in
parser.  Install ``croniter`` for full range/list syntax support.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from maxwell_daemon.contracts import require
from maxwell_daemon.logging import get_logger

__all__ = ["CronScheduler", "CronTrigger"]

log = get_logger(__name__)


@dataclass(slots=True, frozen=True)
class CronTrigger:
    """Wires a cron expression to a task prompt.

    Attributes:
        cron: Five-field cron expression, e.g. ``"0 9 * * 1"`` (Mondays 09:00).
        prompt: The task prompt text to enqueue when the trigger fires.
        repo: Optional repository hint forwarded to ``daemon.submit``.
        backend: Optional backend hint forwarded to ``daemon.submit``.
        priority: Task priority (lower = higher priority; default 100).
        tz: Timezone name understood by ``datetime``; only ``"UTC"`` is
            supported by the built-in parser.  Install ``croniter`` for full
            tz support.
    """

    cron: str
    prompt: str
    repo: str | None = None
    backend: str | None = None
    priority: int = 100
    tz: str = "UTC"


def _parse_field(value: str, lo: int, hi: int) -> set[int]:
    """Parse a single cron field into the set of matching integers.

    Supports ``*``, ``*/step``, and plain integers within ``[lo, hi]``.
    Raises ``ValueError`` for anything else.
    """
    if value == "*":
        return set(range(lo, hi + 1))
    if value.startswith("*/"):
        step = int(value[2:])
        require(step > 0, f"cron step must be > 0, got {step!r}")
        return set(range(lo, hi + 1, step))
    v = int(value)
    require(lo <= v <= hi, f"cron value {v} out of range [{lo}, {hi}]")
    return {v}


def _parse_cron(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int]]:
    """Return (minutes, hours, mdays, months, wdays) sets for *expr*."""
    parts = expr.strip().split()
    require(
        len(parts) == 5,
        f"cron expression must have 5 fields, got {len(parts)}: {expr!r}",
    )
    minutes = _parse_field(parts[0], 0, 59)
    hours = _parse_field(parts[1], 0, 23)
    mdays = _parse_field(parts[2], 1, 31)
    months = _parse_field(parts[3], 1, 12)
    wdays = _parse_field(parts[4], 0, 6)
    return minutes, hours, mdays, months, wdays


def _matches(dt: datetime, cron: str) -> bool:
    """Return True if *dt* (minute-precision) matches *cron*."""
    try:
        minutes, hours, mdays, months, wdays = _parse_cron(cron)
    except (ValueError, Exception):  # noqa: BLE001
        log.warning("invalid cron expression %r; skipping", cron)
        return False
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in mdays
        and dt.month in months
        and dt.isoweekday() % 7 in wdays  # isoweekday: Mon=1..Sun=7 → Sun=0
    )


def _next_tick_delay(now: datetime) -> float:
    """Seconds until the start of the next whole minute."""
    next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return (next_minute - now).total_seconds()


class CronScheduler:
    """Runs registered :class:`CronTrigger` entries on a per-minute poll.

    Usage::

        scheduler = CronScheduler(daemon=daemon)
        scheduler.add(CronTrigger(cron="0 9 * * 1", prompt="Weekly report"))
        await scheduler.start()
        ...
        await scheduler.stop()

    The scheduler fires ``daemon.submit`` on matching minutes.  Each tick
    checks the wall-clock minute against every registered trigger expression.
    Ticks are aligned to whole minutes (skew ≤ 1 s by design).
    """

    def __init__(self, *, daemon: Any) -> None:
        self._daemon = daemon
        self._triggers: list[CronTrigger] = []
        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    def add(self, trigger: CronTrigger) -> None:
        """Register a cron trigger.  May be called before or after ``start``."""
        self._triggers.append(trigger)

    def remove(self, trigger: CronTrigger) -> None:
        """Unregister a previously added trigger.  No-op if not found."""
        with contextlib.suppress(ValueError):
            self._triggers.remove(trigger)

    async def start(self) -> None:
        """Begin the per-minute cron loop in a background task.  Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="cron-scheduler")

    async def stop(self) -> None:
        """Signal the loop to exit and wait for it.  Idempotent."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        self._stop_event = None

    async def tick(self) -> int:
        """Fire all matching triggers for the current UTC minute.

        Returns the number of tasks enqueued.  Exposed publicly so callers
        can drive the scheduler from tests without relying on real-time waits.
        """
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        fired = 0
        for trigger in list(self._triggers):
            if _matches(now, trigger.cron):
                try:
                    self._daemon.submit(
                        trigger.prompt,
                        repo=trigger.repo,
                        backend=trigger.backend,
                        priority=trigger.priority,
                    )
                    fired += 1
                    log.info(
                        "cron trigger fired: cron=%r prompt=%r",
                        trigger.cron,
                        trigger.prompt[:60],
                    )
                except Exception:
                    log.exception(
                        "cron trigger submit failed: cron=%r prompt=%r",
                        trigger.cron,
                        trigger.prompt[:60],
                    )
        return fired

    async def _loop(self) -> None:
        if self._stop_event is None:
            raise RuntimeError("_loop() called before start()")
        while not self._stop_event.is_set():
            delay = _next_tick_delay(datetime.now(timezone.utc))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return  # stop was signalled
            except (TimeoutError, asyncio.TimeoutError):
                pass  # delay elapsed — fire the tick
            try:
                await self.tick()
            except Exception:
                log.exception("cron tick raised unexpectedly; continuing")


# ---------------------------------------------------------------------------
# Convenience: use croniter when available for richer expression support
# ---------------------------------------------------------------------------

try:
    from croniter import croniter as _croniter  # type: ignore[import-untyped]

    def _matches(dt: datetime, cron: str) -> bool:  # redefine for croniter
        """croniter-backed matcher supporting full cron syntax."""
        try:
            # croniter.match checks whether *dt* falls on the given cron tick
            return bool(_croniter.match(cron, dt.replace(second=0, microsecond=0)))
        except Exception:  # noqa: BLE001
            log.warning("croniter could not parse cron=%r; skipping", cron)
            return False

except ImportError:
    pass  # stdlib implementation already defined above
