"""Prometheus instrumentation.

Centralised metric definitions — everywhere else just calls ``record_request``
with a single flat set of kwargs. Having one helper keeps the label taxonomy
consistent across the codebase.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

__all__ = [
    "CONDUCTOR_REQUESTS_TOTAL",
    "CONDUCTOR_REQUEST_COST",
    "CONDUCTOR_REQUEST_DURATION",
    "CONDUCTOR_TOKENS_TOTAL",
    "build_registry",
    "mount_metrics_endpoint",
    "record_request",
]

RequestStatus = Literal["success", "error", "budget_exceeded"]


CONDUCTOR_REQUESTS_TOTAL = Counter(
    "conductor_requests_total",
    "Total agent requests partitioned by backend, model, and outcome",
    labelnames=("backend", "model", "status"),
)

CONDUCTOR_TOKENS_TOTAL = Counter(
    "conductor_tokens_total",
    "Total tokens consumed (prompt + completion)",
    labelnames=("backend", "model"),
)

CONDUCTOR_REQUEST_COST = Counter(
    "conductor_request_cost_usd_total",
    "Cumulative request cost in USD",
    labelnames=("backend", "model"),
)

CONDUCTOR_REQUEST_DURATION = Histogram(
    "conductor_request_duration_seconds",
    "Per-request wall-clock duration",
    labelnames=("backend", "model"),
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)


def record_request(
    *,
    backend: str,
    model: str,
    status: RequestStatus,
    tokens: int = 0,
    cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
) -> None:
    """Emit all per-request metrics in one call.

    Token and cost metrics are only incremented when status == "success" so that
    failed/rejected requests don't pollute spend dashboards.
    """
    CONDUCTOR_REQUESTS_TOTAL.labels(backend=backend, model=model, status=status).inc()
    if status == "success":
        if tokens > 0:
            CONDUCTOR_TOKENS_TOTAL.labels(backend=backend, model=model).inc(tokens)
        if cost_usd > 0:
            CONDUCTOR_REQUEST_COST.labels(backend=backend, model=model).inc(cost_usd)
        if duration_seconds > 0:
            CONDUCTOR_REQUEST_DURATION.labels(backend=backend, model=model).observe(
                duration_seconds
            )


def build_registry() -> CollectorRegistry:
    """Return a fresh CollectorRegistry — useful for isolated scrapes or testing."""
    return CollectorRegistry()


def mount_metrics_endpoint(app: FastAPI, *, path: str = "/metrics") -> None:
    """Attach a Prometheus text-format endpoint to a FastAPI app."""

    @app.get(path, include_in_schema=False)
    async def _metrics() -> Response:
        return Response(
            content=generate_latest(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
