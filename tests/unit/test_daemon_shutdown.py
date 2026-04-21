"""Daemon graceful shutdown — drain in-flight tasks before stopping workers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends import registry
from maxwell_daemon.backends.base import ILLMBackend
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import TaskStatus


class SlowBackend(ILLMBackend):
    """RecordingBackend variant that sleeps to simulate long-running work."""

    name = "slow"

    def __init__(self, *, delay: float = 0.2, **_: Any) -> None:
        self._delay = delay

    async def complete(self, messages: Any, *, model: str, **_: Any) -> Any:
        from maxwell_daemon.backends.base import BackendResponse, TokenUsage

        await asyncio.sleep(self._delay)
        return BackendResponse(
            content="done",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=model,
            backend=self.name,
        )

    async def stream(self, *a: Any, **kw: Any):
        if False:
            yield ""

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> Any:
        from maxwell_daemon.backends.base import BackendCapabilities

        return BackendCapabilities(
            cost_per_1k_input_tokens=0.001, cost_per_1k_output_tokens=0.002
        )


async def _wait_for_status(daemon: Daemon, task_id: str, expected: TaskStatus) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status is expected:
            return
        await asyncio.sleep(0.02)
    final = daemon.get_task(task_id)
    raise AssertionError(
        f"task {task_id} did not reach {expected}; final={final.status if final else None}"
    )


@pytest.fixture
def slow_daemon(
    tmp_path: Path,
) -> Any:
    registry._factories["slow"] = SlowBackend
    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "slow", "model": "x"}},
            "agent": {"default_backend": "primary"},
        }
    )
    d = Daemon(cfg, ledger_path=tmp_path / "l.db")
    yield d
    registry._factories.pop("slow", None)


class TestGracefulShutdown:
    def test_drain_waits_for_in_flight_task(self, slow_daemon: Daemon) -> None:
        async def body() -> None:
            await slow_daemon.start(worker_count=1)
            task = slow_daemon.submit("hi")
            await _wait_for_status(slow_daemon, task.id, TaskStatus.RUNNING)
            await slow_daemon.stop(drain=True, timeout=2.0)
            # After graceful drain, the task should be completed, not cancelled.
            final = slow_daemon.get_task(task.id)
            assert final.status == TaskStatus.COMPLETED

        asyncio.run(body())

    def test_non_drain_cancels_quickly(self, slow_daemon: Daemon) -> None:
        async def body() -> None:
            await slow_daemon.start(worker_count=1)
            slow_daemon.submit("hi")
            await asyncio.sleep(0.02)
            # drain=False is the legacy fast-stop path — workers get cancelled.
            await slow_daemon.stop(drain=False)
            assert len(slow_daemon._workers) == 0

        asyncio.run(body())

    def test_drain_timeout_then_cancel(self, tmp_path: Path) -> None:
        """If the drain deadline passes, workers get cancelled anyway."""
        registry._factories["reallyslow"] = lambda **kw: SlowBackend(delay=5.0)
        try:
            cfg = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"primary": {"type": "reallyslow", "model": "x"}},
                    "agent": {"default_backend": "primary"},
                }
            )
            d = Daemon(cfg, ledger_path=tmp_path / "l.db")

            async def body() -> None:
                await d.start(worker_count=1)
                d.submit("hi")
                await asyncio.sleep(0.05)
                await d.stop(drain=True, timeout=0.1)
                assert len(d._workers) == 0

            asyncio.run(body())
        finally:
            registry._factories.pop("reallyslow", None)
