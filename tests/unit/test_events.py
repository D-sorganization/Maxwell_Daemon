"""Event bus — async pub/sub for task lifecycle events.

Kept orthogonal to the daemon on purpose: anything that needs to know about
task state changes subscribes to the bus, rather than reaching into the Daemon's
internals. That makes the daemon testable without a bus, and the bus testable
without a daemon.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from conductor.events import Event, EventBus, EventKind


@dataclass
class _Captured:
    items: list[Event]


def _drain(bus: EventBus) -> _Captured:
    out = _Captured(items=[])

    async def collect() -> None:
        async for ev in bus.subscribe(queue_size=8):
            out.items.append(ev)
            if len(out.items) >= 3:
                return

    asyncio.run(collect())
    return out


class TestEventBus:
    def test_subscriber_receives_publishes(self) -> None:
        async def run() -> list[Event]:
            bus = EventBus()
            received: list[Event] = []

            async def reader() -> None:
                async for ev in bus.subscribe(queue_size=4):
                    received.append(ev)
                    if ev.payload.get("last"):
                        return

            task = asyncio.create_task(reader())
            await asyncio.sleep(0.01)
            await bus.publish(Event(kind=EventKind.TASK_QUEUED, payload={"id": "a"}))
            await bus.publish(
                Event(kind=EventKind.TASK_COMPLETED, payload={"id": "a", "last": True})
            )
            await asyncio.wait_for(task, timeout=1.0)
            return received

        events = asyncio.run(run())
        assert len(events) == 2
        assert events[0].kind is EventKind.TASK_QUEUED
        assert events[1].kind is EventKind.TASK_COMPLETED

    def test_multiple_subscribers_each_receive_all_events(self) -> None:
        async def run() -> tuple[list[Event], list[Event]]:
            bus = EventBus()
            a: list[Event] = []
            b: list[Event] = []

            async def collect(target: list[Event]) -> None:
                async for ev in bus.subscribe(queue_size=4):
                    target.append(ev)
                    if ev.payload.get("last"):
                        return

            ta = asyncio.create_task(collect(a))
            tb = asyncio.create_task(collect(b))
            await asyncio.sleep(0.01)
            await bus.publish(Event(kind=EventKind.TASK_QUEUED, payload={"id": "x"}))
            await bus.publish(Event(kind=EventKind.TASK_FAILED, payload={"id": "x", "last": True}))
            await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1.0)
            return a, b

        a, b = asyncio.run(run())
        assert [e.kind for e in a] == [EventKind.TASK_QUEUED, EventKind.TASK_FAILED]
        assert [e.kind for e in b] == [EventKind.TASK_QUEUED, EventKind.TASK_FAILED]

    def test_slow_subscriber_is_dropped_not_blocking(self) -> None:
        async def run() -> int:
            bus = EventBus()
            fast_count = 0

            async def fast() -> None:
                nonlocal fast_count
                async for _ in bus.subscribe(queue_size=4):
                    fast_count += 1
                    if fast_count >= 3:
                        return

            async def slow() -> None:
                # Never reads — should be dropped, not block the publisher.
                q = bus.subscribe(queue_size=1)
                await asyncio.sleep(2.0)
                # Drain once so the generator exits cleanly.
                async for _ in q:
                    break

            ft = asyncio.create_task(fast())
            st = asyncio.create_task(slow())
            await asyncio.sleep(0.05)
            for i in range(5):
                await bus.publish(Event(kind=EventKind.TASK_QUEUED, payload={"i": i}))
                await asyncio.sleep(0.01)
            await asyncio.wait_for(ft, timeout=1.0)
            st.cancel()
            return fast_count

        count = asyncio.run(run())
        assert count >= 3

    def test_unsubscribe_on_generator_close(self) -> None:
        async def run() -> int:
            bus = EventBus()
            gen = bus.subscribe(queue_size=1)
            assert bus.subscriber_count() == 1  # synchronous registration
            await gen.aclose()
            return bus.subscriber_count()

        remaining = asyncio.run(run())
        assert remaining == 0


class TestEvent:
    def test_event_has_timestamp(self) -> None:
        ev = Event(kind=EventKind.TASK_QUEUED, payload={})
        assert ev.ts is not None

    def test_to_json_is_serializable(self) -> None:
        import json

        ev = Event(kind=EventKind.TASK_COMPLETED, payload={"id": "x", "cost": 0.1})
        data = json.loads(ev.to_json())
        assert data["kind"] == "task_completed"
        assert data["payload"]["id"] == "x"
        assert "ts" in data
