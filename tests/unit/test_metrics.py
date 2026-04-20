"""Prometheus metrics — counters, histograms, and /metrics exposure."""

from __future__ import annotations

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from maxwell_daemon.metrics import (
    MAXWELL_REQUESTS_TOTAL,
    MAXWELL_TOKENS_TOTAL,
    build_registry,
    mount_metrics_endpoint,
    record_request,
)


class TestRecordRequest:
    def test_counts_successful_request(self) -> None:
        before = MAXWELL_REQUESTS_TOTAL.labels(
            backend="claude", model="m", status="success"
        )._value.get()
        record_request(
            backend="claude",
            model="m",
            status="success",
            tokens=100,
            cost_usd=0.05,
            duration_seconds=1.5,
        )
        after = MAXWELL_REQUESTS_TOTAL.labels(
            backend="claude", model="m", status="success"
        )._value.get()
        assert after == before + 1

    def test_records_token_total(self) -> None:
        before = MAXWELL_TOKENS_TOTAL.labels(backend="claude", model="m")._value.get()
        record_request(
            backend="claude",
            model="m",
            status="success",
            tokens=500,
            cost_usd=0.01,
            duration_seconds=0.5,
        )
        after = MAXWELL_TOKENS_TOTAL.labels(backend="claude", model="m")._value.get()
        assert after == before + 500

    def test_error_status_skips_token_and_cost(self) -> None:
        # Error path still bumps the request counter but not tokens/cost.
        tokens_before = MAXWELL_TOKENS_TOTAL.labels(backend="claude", model="err")._value.get()
        record_request(backend="claude", model="err", status="error")
        tokens_after = MAXWELL_TOKENS_TOTAL.labels(backend="claude", model="err")._value.get()
        assert tokens_after == tokens_before


class TestMetricsEndpoint:
    def test_endpoint_returns_200(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()
        mount_metrics_endpoint(app)
        client = TestClient(app)
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "maxwell_daemon_" in r.text

    def test_content_type_is_prometheus_text(self) -> None:
        from fastapi import FastAPI

        app = FastAPI()
        mount_metrics_endpoint(app)
        client = TestClient(app)
        r = client.get("/metrics")
        assert r.headers["content-type"].startswith("text/plain")


class TestBuildRegistry:
    def test_returns_collector_registry(self) -> None:
        reg = build_registry()
        assert isinstance(reg, CollectorRegistry)
