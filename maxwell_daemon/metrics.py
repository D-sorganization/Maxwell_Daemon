"""Prometheus instrumentation.

Centralised metric definitions — everywhere else just calls ``record_request``
with a single flat set of kwargs. Having one helper keeps the label taxonomy
consistent across the codebase.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, Response
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "MAXWELL_COST_FORECAST_USD",
    "MAXWELL_DAEMON_ACTIVE_TASKS",
    "MAXWELL_DAEMON_LIVE_TASKS_DICT_SIZE",
    "MAXWELL_FREE_REQUESTS_TOTAL",
    "MAXWELL_LEDGER_CONNECTIONS_IN_USE",
    "MAXWELL_REQUESTS_TOTAL",
    "MAXWELL_REQUEST_COST",
    "MAXWELL_REQUEST_DURATION",
    "MAXWELL_TOKENS_TOTAL",
    "build_registry",
    "mount_metrics_endpoint",
    "record_request",
]

RequestStatus = Literal["success", "error", "budget_exceeded"]


MAXWELL_REQUESTS_TOTAL = Counter(
    "maxwell_daemon_requests_total",
    "Total agent requests partitioned by backend, model, and outcome",
    labelnames=("backend", "model", "status"),
)

MAXWELL_TOKENS_TOTAL = Counter(
    "maxwell_daemon_tokens_total",
    "Total tokens consumed (prompt + completion)",
    labelnames=("backend", "model"),
)

MAXWELL_REQUEST_COST = Counter(
    "maxwell_daemon_request_cost_usd_total",
    "Cumulative request cost in USD",
    labelnames=("backend", "model"),
)

MAXWELL_FREE_REQUESTS_TOTAL = Counter(
    "maxwell_daemon_free_requests_total",
    (
        "Successful requests with zero billed cost "
        "(e.g. local Ollama or cached provider hits). "
        "Complements maxwell_daemon_request_cost_usd_total so dashboards can "
        "distinguish 'never ran' from 'ran many free requests'."
    ),
    labelnames=("backend", "model"),
)

MAXWELL_COST_FORECAST_USD = Gauge(
    "maxwell_daemon_cost_forecast_usd",
    "Linear month-end spend forecast from the cost ledger",
)

MAXWELL_DAEMON_ACTIVE_TASKS = Gauge(
    "maxwell_daemon_active_tasks",
    "Number of tasks currently in a non-terminal state",
)

MAXWELL_DAEMON_LIVE_TASKS_DICT_SIZE = Gauge(
    "maxwell_daemon_live_tasks_dict_size",
    "Number of tasks currently held in the hot memory dict",
)

MAXWELL_LEDGER_CONNECTIONS_IN_USE = Gauge(
    "maxwell_ledger_connections_in_use",
    "Number of active SQLite connections in the ledger pool",
)


MAXWELL_REQUEST_DURATION = Histogram(
    "maxwell_daemon_request_duration_seconds",
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
    cost_usd: float | None = None,
    duration_seconds: float = 0.0,
) -> None:
    """Emit all per-request metrics in one call.

    Token and cost metrics are only incremented when status == "success" so that
    failed/rejected requests don't pollute spend dashboards. ``cost_usd=None``
    (the default) means cost is unknown — callers that don't have cost data
    (e.g. issue-executor paths) should omit the argument.  Explicitly passing
    ``cost_usd=0.0`` signals a genuinely free call (free-tier Ollama, cached
    hit) and is counted separately via ``MAXWELL_FREE_REQUESTS_TOTAL`` so
    dashboards can distinguish "unknown cost" from "verified free".
    """
    MAXWELL_REQUESTS_TOTAL.labels(backend=backend, model=model, status=status).inc()
    if status == "success":
        if tokens > 0:
            MAXWELL_TOKENS_TOTAL.labels(backend=backend, model=model).inc(tokens)
        if cost_usd is not None:
            if cost_usd > 0:
                MAXWELL_REQUEST_COST.labels(backend=backend, model=model).inc(cost_usd)
            elif cost_usd == 0.0:
                MAXWELL_FREE_REQUESTS_TOTAL.labels(backend=backend, model=model).inc()
        if duration_seconds > 0:
            MAXWELL_REQUEST_DURATION.labels(backend=backend, model=model).observe(duration_seconds)


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
