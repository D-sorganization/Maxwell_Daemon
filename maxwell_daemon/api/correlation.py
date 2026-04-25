"""Correlation-ID middleware for end-to-end request tracing.

Attaches a ``correlation_id`` (UUID4) to every inbound HTTP request and
propagates it through:

* The structlog context-vars so every log line emitted during the request
  carries the ID automatically.
* The ``X-Correlation-ID`` response header so callers can match log lines to
  their requests.

Usage — register once in ``create_app``::

    from maxwell_daemon.api.correlation import CorrelationIdMiddleware
    app.add_middleware(CorrelationIdMiddleware)

The middleware also reads an incoming ``X-Correlation-ID`` or ``X-Request-ID``
header so that upstream services (API gateways, load-balancers) can inject
their own trace IDs and have them forwarded transparently.

Programmatic access::

    from maxwell_daemon.api.correlation import get_correlation_id
    cid = get_correlation_id()   # returns '' outside a request context
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from maxwell_daemon.logging import bind_context, get_logger

__all__ = [
    "CorrelationIdMiddleware",
    "get_correlation_id",
]

# ---------------------------------------------------------------------------
# Module-level ContextVar so any code running inside a request can read the ID
# without needing a reference to the request object.
# ---------------------------------------------------------------------------

_CORRELATION_ID_VAR: ContextVar[str] = ContextVar("correlation_id", default="")

log = get_logger(__name__)


def get_correlation_id() -> str:
    """Return the correlation ID for the current request context.

    Returns an empty string when called outside a request (e.g. background
    tasks that were not started inside a request context).
    """
    return _CORRELATION_ID_VAR.get()


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """FastAPI/Starlette middleware that attaches a correlation ID to each request.

    Resolution order for the incoming ID (first wins):
    1. ``X-Correlation-ID`` header
    2. ``X-Request-ID`` header  (interoperability with older clients)
    3. Fresh ``uuid.uuid4()``

    The resolved ID is:
    * Set in the :data:`_CORRELATION_ID_VAR` context variable.
    * Bound into the structlog context-vars so all log lines carry it.
    * Echoed back in the ``X-Correlation-ID`` response header.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        raw = request.headers.get("x-correlation-id", "") or request.headers.get(
            "x-request-id", ""
        )
        try:
            correlation_id = str(uuid.UUID(raw)) if raw else str(uuid.uuid4())
        except ValueError:
            correlation_id = str(uuid.uuid4())

        token = _CORRELATION_ID_VAR.set(correlation_id)
        try:
            with bind_context(correlation_id=correlation_id):
                response: Response = await call_next(request)
        finally:
            _CORRELATION_ID_VAR.reset(token)

        response.headers["x-correlation-id"] = correlation_id
        return response


def install_correlation_middleware(app: Any) -> None:
    """Convenience helper — call instead of ``app.add_middleware(...)`` directly.

    Idempotent: if ``CorrelationIdMiddleware`` is already registered this is a
    no-op (guards against double-registration in tests that rebuild the app).
    """

    for m in getattr(app, "user_middleware", []):
        cls = getattr(m, "cls", None) or (
            m[1] if isinstance(m, tuple) and len(m) > 1 else None
        )
        if cls is CorrelationIdMiddleware:
            return

    app.add_middleware(CorrelationIdMiddleware)
    log.debug("correlation_id_middleware_installed")
