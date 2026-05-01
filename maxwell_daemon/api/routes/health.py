"""Health and version endpoints (phase 1 of #793).

Extracted from ``maxwell_daemon/api/server.py`` so the operator-facing
liveness probes live in their own focused module.  These endpoints are
part of the stable, append-only contract consumed by ``runner-dashboard``
(see ``AGENTS.md`` and ``maxwell_daemon/api/contract.py``).

The shapes returned here MUST match ``HealthResponse`` and
``VersionResponse`` exactly.  Any breaking change requires bumping
``CONTRACT_VERSION``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from maxwell_daemon import __version__
from maxwell_daemon.api.contract import (
    CONTRACT_VERSION,
    HealthResponse,
    VersionResponse,
)
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

# Exceptions that indicate the daemon's internal state is inconsistent
# (partial initialisation, attribute missing, wrong type, etc.).  These
# are caught so that liveness probes degrade gracefully and never
# observe a 5xx from a half-initialised daemon.  Control signals such as
# KeyboardInterrupt, SystemExit, and asyncio.CancelledError are never
# swallowed.
_STATE_ERROR_TYPES: tuple[type[BaseException], ...] = (
    RuntimeError,
    AttributeError,
    TypeError,
    ValueError,
    OSError,
)


def register(app: FastAPI, daemon: Daemon) -> None:
    """Attach ``GET /api/version`` and ``GET /api/health`` to ``app``.

    These endpoints are intentionally orthogonal to the rest of the
    daemon's state: ``/api/version`` returns static build metadata, and
    ``/api/health`` degrades gracefully if ``daemon.state()`` raises so
    that liveness probes never observe a 5xx from a partially-initialised
    daemon.
    """

    @app.get("/api/version")
    async def api_version() -> VersionResponse:
        return VersionResponse(daemon=__version__, contract=CONTRACT_VERSION)

    @app.get("/health")
    async def legacy_health() -> dict[str, Any]:
        """Legacy liveness probe used by unit tests and some health checks."""
        try:
            state = daemon.state()
            uptime = (datetime.now(timezone.utc) - state.started_at).total_seconds()
            return {
                "status": "ok",
                "uptime_seconds": uptime,
                "version": __version__,
            }
        except _STATE_ERROR_TYPES as exc:
            log.exception(
                "legacy_health: daemon.state() raised; returning degraded",
                error=str(exc),
            )
            return {
                "status": "ok",
                "uptime_seconds": 0.0,
                "version": __version__,
            }

    @app.get("/healthz")
    async def legacy_healthz() -> dict[str, Any]:
        """Kubernetes-style liveness probe.

        Returns ``{"status": "ready"}`` when the daemon is healthy and
        backends are available, or ``{"status": "ok"}`` when alive but not
        yet ready.  HTTP 200 in both cases.
        """
        try:
            state = daemon.state()
            uptime = (datetime.now(timezone.utc) - state.started_at).total_seconds()
            probe_status = "ready" if state.backends_available else "ok"
            return {
                "status": probe_status,
                "uptime_seconds": uptime,
                "version": __version__,
            }
        except _STATE_ERROR_TYPES as exc:
            log.exception(
                "legacy_healthz: daemon.state() raised; returning degraded",
                error=str(exc),
            )
            return {
                "status": "ok",
                "uptime_seconds": 0.0,
                "version": __version__,
            }

    @app.get("/api/health")
    async def api_health() -> HealthResponse:
        try:
            state = daemon.state()
            uptime = (datetime.now(timezone.utc) - state.started_at).total_seconds()
            # "gate" concept: open when backends are available, closed otherwise.
            gate = "open" if state.backends_available else "closed"
            return HealthResponse(
                status="ok",
                uptime_seconds=uptime,
                gate=gate,
            )
        except _STATE_ERROR_TYPES as exc:
            log.exception(
                "api_health: daemon.state() raised; returning degraded",
                error=str(exc),
            )
            return HealthResponse(
                status="degraded",
                uptime_seconds=0.0,
                gate="closed",
            )

    @app.get("/readyz")
    async def legacy_readyz() -> dict[str, str]:
        """Legacy readiness probe."""
        try:
            state = daemon.state()
            if not state.backends_available:
                from fastapi import HTTPException

                raise HTTPException(503, "no backends available")
            return {"status": "ready"}
        except HTTPException:
            raise
        except _STATE_ERROR_TYPES as exc:
            log.exception(
                "legacy_readyz: daemon.state() raised; returning unavailable",
                error=str(exc),
            )
            from fastapi import HTTPException

            raise HTTPException(503, "no backends available") from None
