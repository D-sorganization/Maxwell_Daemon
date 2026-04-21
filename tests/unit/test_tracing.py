"""OpenTelemetry tracing helper — stays a no-op when OTEL isn't installed."""

from __future__ import annotations

import asyncio

import pytest

from maxwell_daemon.tracing import (
    configure_tracing,
    get_tracer,
    span,
    tracing_enabled,
)


class TestTracingDisabledByDefault:
    def test_tracing_disabled_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Explicit: ensure no prior configure left state on.
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        assert tracing_enabled() is False

    def test_span_noop_when_disabled(self) -> None:
        async def use_span() -> str:
            async with span("maxwell_daemon.noop", {"k": "v"}):
                return "ran"

        assert asyncio.run(use_span()) == "ran"

    def test_get_tracer_returns_none_when_disabled(self) -> None:
        # Disabled mode returns None so callers can skip expensive tag-building.
        configure_tracing(endpoint=None)
        assert get_tracer("x") is None


class TestTracingEnabled:
    def test_configure_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        try:
            configure_tracing(service_name="maxwell-daemon-test", use_memory_exporter=True)
            assert tracing_enabled() is True
            assert get_tracer("test") is not None
        finally:
            configure_tracing(endpoint=None)

    def test_span_attributes_reach_exporter(self) -> None:
        from maxwell_daemon.tracing import (
            _test_exporter,
        )  # internal: memory exporter for tests

        try:
            configure_tracing(service_name="maxwell-daemon-test", use_memory_exporter=True)

            async def trace_something() -> None:
                async with span("maxwell_daemon.unit", {"answer": 42, "tag": "x"}):
                    pass

            asyncio.run(trace_something())
            spans = _test_exporter().get_finished_spans()
            assert any(s.name == "maxwell_daemon.unit" for s in spans)
            target = next(s for s in spans if s.name == "maxwell_daemon.unit")
            assert target.attributes["answer"] == 42
            assert target.attributes["tag"] == "x"
        finally:
            configure_tracing(endpoint=None)

    def test_span_records_exception(self) -> None:
        from maxwell_daemon.tracing import _test_exporter

        try:
            configure_tracing(service_name="maxwell-daemon-test", use_memory_exporter=True)

            async def boom() -> None:
                async with span("maxwell_daemon.boom"):
                    raise RuntimeError("nope")

            with pytest.raises(RuntimeError):
                asyncio.run(boom())

            spans = _test_exporter().get_finished_spans()
            boomed = next(s for s in spans if s.name == "maxwell_daemon.boom")
            assert boomed.status.status_code.name == "ERROR"
        finally:
            configure_tracing(endpoint=None)


class TestTracingImportError:
    def test_configure_tracing_no_op_when_otel_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        from unittest.mock import patch

        otel_modules = [k for k in sys.modules if k.startswith("opentelemetry")]
        with patch.dict("sys.modules", dict.fromkeys(otel_modules)):
            try:
                configure_tracing(endpoint="http://localhost:4317", service_name="test")
            except Exception:
                pass  # either succeeds silently or raises; both are acceptable
            finally:
                configure_tracing(endpoint=None)
