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
from collections.abc import AsyncIterator
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
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Telemetry is best-effort; never block a publisher.
                continue

    async def subscribe(self, *, queue_size: int = 32) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    def subscriber_count(self) -> int:
        return len(self._subscribers)
