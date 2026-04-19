"""In-process async event bus.

Fan-out publish/subscribe with bounded per-subscriber queues. Slow subscribers
lose events rather than blocking publishers — this is a telemetry bus, not a
durable queue. Durability belongs in the cost ledger and task store.

The bus is deliberately decoupled from the daemon: callers publish, subscribers
consume. This lets the WebSocket endpoint, metrics exporter, and any future
downstream consumer plug in without the daemon knowing they exist.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = ["Event", "EventBus", "EventKind"]


class EventKind(str, Enum):
    TASK_QUEUED = "task_queued"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    BUDGET_ALERT = "budget_alert"
    BACKEND_HEALTH = "backend_health"


@dataclass(slots=True)
class Event:
    kind: EventKind
    payload: dict[str, Any]
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind.value,
                "ts": self.ts.isoformat(),
                "payload": self.payload,
            }
        )


class EventBus:
    """Bounded-queue fan-out event bus.

    Each subscriber owns an :class:`asyncio.Queue` with a configurable capacity.
    Publishing uses ``put_nowait``: if a subscriber's queue is full, we drop the
    event for that subscriber and continue. This is the only safe choice for
    telemetry — blocking on the slowest consumer would make the daemon's hot
    path depend on subscriber liveness.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    async def publish(self, event: Event) -> None:
        # No lock needed for a snapshot read; set iteration is atomic.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Telemetry is best-effort; never block a publisher.
                continue

    def subscribe(self, *, queue_size: int = 32) -> _Subscription:
        """Register a subscriber and return an async iterator over its events.

        Registration happens synchronously at call time, so
        :meth:`subscriber_count` reflects the new subscriber immediately.
        Unregistration happens when the returned iterator is closed (via
        ``aclose()`` or when the last reference is dropped).
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        self._subscribers.add(queue)
        return _Subscription(self, queue)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)


class _Subscription:
    """Async iterator backed by a queue, with explicit unsubscribe on close."""

    def __init__(self, bus: EventBus, queue: asyncio.Queue[Event]) -> None:
        self._bus = bus
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> _Subscription:
        return self

    async def __anext__(self) -> Event:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._bus._unsubscribe(self._queue)

    def __del__(self) -> None:
        # Safety net: if the subscription is GC'd without explicit close,
        # still deregister from the bus so subscriber_count() stays accurate.
        if not self._closed:
            self._bus._unsubscribe(self._queue)
            self._closed = True
