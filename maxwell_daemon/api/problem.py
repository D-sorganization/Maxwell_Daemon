"""RFC 7807 ``application/problem+json`` exception handler for FastAPI.

Single code path that translates any :class:`maxwell_daemon.errors.MaxwellError`
to a stable, machine-readable HTTP response. Counterpart to
:mod:`maxwell_daemon.errors`; see ``docs/reviews/2026-05-22-adversarial-review.md``
§4 for the motivation.

Design (DRY/LoD):

* **DRY** — one handler covers the entire :class:`MaxwellError` subtree;
  the response body comes from a single ``error.to_problem_detail()`` call.
* **LoD** — the handler talks to ``error.to_problem_detail()`` and
  ``type(error).http_status``. It does **not** reach into the error's
  ``_extras`` or fields, and it does not introspect the request beyond
  reading a single correlation header.
* **Idempotent install** — ``install_problem_handler(app)`` can be called
  multiple times without registering duplicate handlers. This protects
  against fixture/production double-install bugs.
"""

from __future__ import annotations

from typing import Final

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from maxwell_daemon.errors import MaxwellError

__all__ = ["PROBLEM_JSON_MEDIA_TYPE", "install_problem_handler"]

#: RFC 7807 §3 content type.
PROBLEM_JSON_MEDIA_TYPE: Final[str] = "application/problem+json"

#: Internal sentinel — set on the FastAPI app the first time the handler is
#: installed so subsequent calls become no-ops. Using a private attribute on
#: the app object keeps the idempotence check entirely local (LoD).
_INSTALL_SENTINEL: Final[str] = "_maxwell_problem_handler_installed"

_log = structlog.get_logger(__name__)


def install_problem_handler(app: FastAPI) -> None:
    """Register the RFC 7807 handler for :class:`MaxwellError` on ``app``.

    Idempotent: calling more than once has no effect (returns early).
    """
    if getattr(app, _INSTALL_SENTINEL, False):
        return

    @app.exception_handler(MaxwellError)
    async def _handle_maxwell_error(request: Request, exc: MaxwellError) -> JSONResponse:
        # LoD boundary: the handler reads from ``exc`` only via its public
        # serialiser, and from ``request`` only via the headers mapping.
        body = exc.to_problem_detail()
        status = type(exc).http_status

        # Log at WARNING for 4xx (caller fault) and ERROR for 5xx (our fault).
        # ``exc_info`` is included for 5xx so the stack reaches the operator.
        log_level = "error" if status >= 500 else "warning"
        getattr(_log, log_level)(
            "maxwell_error",
            problem_type=body["type"],
            status=status,
            path=str(request.url.path),
            method=request.method,
            exc_info=status >= 500,
        )

        return JSONResponse(
            status_code=status,
            content=body,
            media_type=PROBLEM_JSON_MEDIA_TYPE,
        )

    setattr(app, _INSTALL_SENTINEL, True)
