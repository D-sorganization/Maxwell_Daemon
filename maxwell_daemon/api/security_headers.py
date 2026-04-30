"""HTTP security-headers middleware (Phase 1 of #797).

This module installs a small Starlette middleware that attaches a set of
defensive HTTP response headers to every response served by the FastAPI app.
The headers are deliberately conservative and additive:

* ``X-Content-Type-Options: nosniff`` — block MIME sniffing.
* ``X-Frame-Options: DENY`` — disallow framing (clickjacking protection).
* ``Referrer-Policy: strict-origin-when-cross-origin`` — limit referrer leakage.
* ``Permissions-Policy: geolocation=(), microphone=(), camera=()`` — disable
  powerful browser APIs by default.
* ``Content-Security-Policy`` — locked down to ``'self'`` for the bundled
  static UI (``script-src 'self'``, ``style-src 'self' 'unsafe-inline'``,
  ``img-src 'self' data:``).
* ``Strict-Transport-Security`` — only emitted when the
  ``MAXWELL_HSTS_ENABLED`` environment variable is set to a truthy value
  (default off so plain-HTTP development keeps working).

Each header is **only** set when the response does not already carry it,
which lets individual handlers (e.g. the docs UI) override values when
needed.

Wire-up — register at the front of the middleware stack in
``create_app()``::

    from maxwell_daemon.api.security_headers import install_security_headers
    install_security_headers(app)

Idempotent: re-registering is a no-op (guards tests that rebuild the app).
"""

from __future__ import annotations

import os
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from maxwell_daemon.logging import get_logger

__all__ = [
    "DEFAULT_CSP",
    "DEFAULT_PERMISSIONS_POLICY",
    "DEFAULT_REFERRER_POLICY",
    "DEFAULT_STRICT_TRANSPORT_SECURITY",
    "SecurityHeadersMiddleware",
    "install_security_headers",
]

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Default header values — exported for tests and documentation parity.
# ---------------------------------------------------------------------------

DEFAULT_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"
)

DEFAULT_PERMISSIONS_POLICY = "geolocation=(), microphone=(), camera=()"

DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"

DEFAULT_STRICT_TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

_HSTS_ENV_VAR = "MAXWELL_HSTS_ENABLED"


def _hsts_enabled() -> bool:
    """Return whether HSTS should be advertised.

    HSTS is gated by the ``MAXWELL_HSTS_ENABLED`` environment variable so
    that local development over plain HTTP does not get permanently pinned
    to HTTPS in browsers. Truthy values: ``1``, ``true``, ``yes``, ``on``
    (case-insensitive).
    """

    raw = os.environ.get(_HSTS_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach a conservative set of security headers to every response.

    Each header is only set when not already present on the outgoing
    response, so per-route overrides remain possible.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        response: Response = await call_next(request)
        headers = response.headers

        if "x-content-type-options" not in headers:
            headers["X-Content-Type-Options"] = "nosniff"
        if "x-frame-options" not in headers:
            headers["X-Frame-Options"] = "DENY"
        if "referrer-policy" not in headers:
            headers["Referrer-Policy"] = DEFAULT_REFERRER_POLICY
        if "permissions-policy" not in headers:
            headers["Permissions-Policy"] = DEFAULT_PERMISSIONS_POLICY
        if "content-security-policy" not in headers:
            headers["Content-Security-Policy"] = DEFAULT_CSP
        if _hsts_enabled() and "strict-transport-security" not in headers:
            headers["Strict-Transport-Security"] = DEFAULT_STRICT_TRANSPORT_SECURITY

        return response


def install_security_headers(app: Any) -> None:
    """Convenience helper — register :class:`SecurityHeadersMiddleware`.

    Idempotent: if the middleware is already registered this is a no-op.
    Should be called early in ``create_app()`` so the headers are emitted
    on every response regardless of which handler produced it.
    """

    for m in getattr(app, "user_middleware", []):
        cls = getattr(m, "cls", None) or (m[1] if isinstance(m, tuple) and len(m) > 1 else None)
        if cls is SecurityHeadersMiddleware:
            return

    app.add_middleware(SecurityHeadersMiddleware)
    log.debug("security_headers_middleware_installed")
