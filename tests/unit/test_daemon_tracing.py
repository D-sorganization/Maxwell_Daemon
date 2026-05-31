"""Daemon dispatch path emits OTel spans when tracing is configured.

Phase 2 observability slice of #896: the daemon's task dispatch path
(``Daemon._execute``) is instrumented with spans so a trace shows the
end-to-end lifecycle of a prompt task — dispatch wrapper plus the backend
completion call nested inside it.

These tests use the in-memory exporter the tracing module already supports
(``configure_tracing(use_memory_exporter=True)``) so they assert real span
emission without an OTLP collector.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from maxwell_daemon.backends import registry
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskStatus
from maxwell_daemon.tracing import _test_exporter, configure_tracing

T = TypeVar("T")


async def _wait_for_status(
    daemon: Daemon, task_id: str, expected: TaskStatus, timeout: float = 10.0
) -> Task:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        task = daemon.get_task(task_id)
        if task and task.status == expected:
            return task
        await asyncio.sleep(0.02)
    task = daemon.get_task(task_id)
    if task and task.status == expected:
        return task
    raise AssertionError(
        f"task {task_id} did not reach {expected}; final={task.status if task else None}"
    )


async def _with_daemon(
    config: MaxwellDaemonConfig,
    ledger_path: Path,
    *,
    body: Callable[[Daemon], Awaitable[T]],
) -> T:
    d = Daemon(
        config,
        ledger_path=ledger_path,
        task_store_path=ledger_path.with_suffix(".tasks.db"),
    )
    await d.start(worker_count=1)
    try:
        return await body(d)
    finally:
        await d.stop()


class TestDispatchSpans:
    def test_prompt_dispatch_emits_dispatch_and_backend_spans(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
    ) -> None:
        try:
            configure_tracing(use_memory_exporter=True)

            async def _body(d: Daemon) -> None:
                task = d.submit("hello", backend="primary")
                await _wait_for_status(d, task.id, TaskStatus.COMPLETED)

            asyncio.run(_with_daemon(minimal_config, isolated_ledger_path, body=_body))

            names = {s.name for s in _test_exporter().get_finished_spans()}
            assert "maxwell_daemon.task.dispatch" in names
            assert "maxwell_daemon.task.backend_complete" in names
        finally:
            configure_tracing(endpoint=None)

    def test_dispatch_span_carries_task_attributes(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
    ) -> None:
        try:
            configure_tracing(use_memory_exporter=True)

            async def _body(d: Daemon) -> str:
                task = d.submit("hello", backend="primary")
                await _wait_for_status(d, task.id, TaskStatus.COMPLETED)
                return task.id

            task_id = asyncio.run(_with_daemon(minimal_config, isolated_ledger_path, body=_body))

            dispatch = next(
                s
                for s in _test_exporter().get_finished_spans()
                if s.name == "maxwell_daemon.task.dispatch"
            )
            assert dispatch.attributes["task_id"] == task_id
            assert dispatch.attributes["kind"] == "prompt"
        finally:
            configure_tracing(endpoint=None)

    def test_failed_dispatch_records_exception_on_span(
        self,
        isolated_ledger_path: Path,
    ) -> None:
        from maxwell_daemon.backends.base import (
            BackendCapabilities,
            BackendResponse,
            ILLMBackend,
            Message,
        )

        class _RaisingBackend(ILLMBackend):
            name = "recording"

            def __init__(self, **_: object) -> None:
                pass

            async def complete(
                self, messages: list[Message], *, model: str, **_: object
            ) -> BackendResponse:
                raise RuntimeError("backend boom")

            async def stream(self, *a: object, **k: object):  # type: ignore[no-untyped-def]
                if False:
                    yield ""

            async def health_check(self) -> bool:
                return True

            def capabilities(self, model: str) -> BackendCapabilities:
                return BackendCapabilities()

        try:
            configure_tracing(use_memory_exporter=True)
            registry._factories["recording"] = _RaisingBackend  # type: ignore[assignment]
            config = MaxwellDaemonConfig.model_validate(
                {
                    "backends": {"primary": {"type": "recording", "model": "test-model"}},
                    "agent": {"default_backend": "primary"},
                }
            )

            async def _body(d: Daemon) -> None:
                task = d.submit("hello", backend="primary")
                await _wait_for_status(d, task.id, TaskStatus.FAILED)

            asyncio.run(_with_daemon(config, isolated_ledger_path, body=_body))

            dispatch_spans = [
                s
                for s in _test_exporter().get_finished_spans()
                if s.name == "maxwell_daemon.task.dispatch"
            ]
            assert dispatch_spans, "dispatch span should be emitted even on failure"
            # The span records the exception and sets an ERROR status because
            # the backend error propagates through the span context manager.
            assert any(s.status.status_code.name == "ERROR" for s in dispatch_spans)
        finally:
            registry._factories.pop("recording", None)
            configure_tracing(endpoint=None)
