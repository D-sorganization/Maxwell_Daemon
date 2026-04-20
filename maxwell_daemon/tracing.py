"""OpenTelemetry tracing — optional dependency, zero-cost when off.

Callers use two entrypoints:

* ``configure_tracing(service_name=..., endpoint=...)`` — call once at startup.
  Absent an endpoint, tracing is disabled and all later ``span()`` calls are
  no-ops.
* ``async with span("maxwell_daemon.phase", {"key": "value"})`` — opens a span,
  captures exceptions, records attributes, closes the span on exit.

We treat OpenTelemetry as an *optional* dependency: if ``opentelemetry-sdk``
isn't installed, everything still works, it just can't trace. That keeps the
default install small.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

__all__ = [
    "configure_tracing",
    "get_tracer",
    "span",
    "tracing_enabled",
]

_state: dict[str, Any] = {
    "enabled": False,
    "tracer_provider": None,
    "memory_exporter": None,
}


def tracing_enabled() -> bool:
    return bool(_state["enabled"])


def configure_tracing(
    *,
    service_name: str = "maxwell-daemon",
    endpoint: str | None = "auto",
    use_memory_exporter: bool = False,
) -> None:
    """Configure the global tracer provider.

    :param endpoint: OTLP HTTP/gRPC endpoint. ``None`` disables tracing. ``"auto"``
        reads ``OTEL_EXPORTER_OTLP_ENDPOINT`` from the environment and, if unset,
        also disables tracing.
    :param use_memory_exporter: attach an in-memory exporter instead. Used by
        tests so they can inspect emitted spans without an OTLP collector.
    """
    import os

    if endpoint == "auto":
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None

    if not use_memory_exporter and endpoint is None:
        _state.update(enabled=False, tracer_provider=None, memory_exporter=None)
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            SimpleSpanProcessor,
        )
    except ImportError:  # optional dep missing
        _state.update(enabled=False)
        return

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))

    if use_memory_exporter:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        _state["memory_exporter"] = exporter
    else:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        _state["memory_exporter"] = None

    # OTel only lets you set the global provider once per process — after that,
    # set_tracer_provider is a no-op. We keep our own reference so repeated
    # configure_tracing() calls (tests, reloads) actually swap the exporter.
    with contextlib.suppress(Exception):
        trace.set_tracer_provider(provider)
    _state.update(enabled=True, tracer_provider=provider)


def get_tracer(name: str) -> Any:
    """Return a tracer when enabled, ``None`` when disabled."""
    if not tracing_enabled():
        return None
    provider = _state["tracer_provider"]
    return provider.get_tracer(name) if provider else None


@contextlib.asynccontextmanager
async def span(name: str, attributes: dict[str, Any] | None = None) -> AsyncIterator[None]:
    """Open a span. Becomes a no-op when tracing is disabled."""
    if not tracing_enabled():
        yield
        return

    from opentelemetry.trace import Status, StatusCode

    # Use *our* provider reference so tests that reconfigure see the right
    # exporter, not whatever happens to be installed as the global.
    provider = _state["tracer_provider"]
    tracer = provider.get_tracer("maxwell-daemon")
    with tracer.start_as_current_span(name) as s:
        if attributes:
            for k, v in attributes.items():
                s.set_attribute(k, v)
        try:
            yield
        except Exception as exc:
            s.record_exception(exc)
            s.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def _test_exporter() -> Any:
    """Return the in-memory exporter. Only meaningful after
    ``configure_tracing(use_memory_exporter=True)``."""
    return _state["memory_exporter"]
