"""FastAPI app — Maxwell-Daemon HTTP API.

This module is responsible only for:
  - Creating the FastAPI application instance
  - Installing middleware (CORS, security headers, correlation IDs,
    rate limiting, request-ID, metrics)
  - Wiring all route modules (see ``maxwell_daemon/api/routes/``)
  - Serving the static UI

All route handlers live in ``api/routes/`` submodules.  Route modules
extracted via epic #896 Phase 1.1:

  - maxwell_daemon.api.routes.auth          (/api/v1/auth/*)
  - maxwell_daemon.api.routes.control_plane (/api/v1/control-plane/gauntlet)
  - maxwell_daemon.api.routes.health        (/api/version, /api/health, /health*)
  - maxwell_daemon.api.routes.status        (/api/status)
  - maxwell_daemon.api.routes.cost          (/api/cost)
  - maxwell_daemon.api.routes.tasks         (/api/v1/tasks, /api/tasks legacy)
  - maxwell_daemon.api.routes.dispatch      (/api/dispatch, /api/control/{action})
  - maxwell_daemon.api.routes.backends      (/api/v1/backends/*)
  - maxwell_daemon.api.routes.actions       (/api/v1/actions/*, /api/v1/artifacts/*)
  - maxwell_daemon.api.routes.work_items    (/api/v1/work-items/*, /api/v1/task-graphs/*)
  - maxwell_daemon.api.routes.issues        (/api/v1/issues/*, /api/v1/templates/*, /api/v1/memory/*)
  - maxwell_daemon.api.routes.fleet         (/api/v1/fleet/*, /api/v1/workers/*, /api/v1/heartbeat)
  - maxwell_daemon.api.routes.audit         (/api/v1/audit/*, /api/reload, /api/v1/admin/prune)
  - maxwell_daemon.api.routes.webhooks      (/api/v1/webhooks/*, /api/webhooks/trigger, /api/v1/evals/*)
  - maxwell_daemon.api.routes.ssh           (/api/v1/ssh/*)
  - maxwell_daemon.api.routes.events        (/api/v1/events WebSocket)

Auth helpers (``make_auth_dep``, ``make_rbac_dep``, ``websocket_auth_or_close``)
live in ``maxwell_daemon.api.deps``.
"""

from __future__ import annotations

import uuid
from pathlib import Path as _Path
from typing import Any

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from maxwell_daemon import __version__
from maxwell_daemon.api.contract import CONTRACT_VERSION
from maxwell_daemon.api.deps import make_auth_dep, make_rbac_dep, websocket_auth_or_close
from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import bind_context, get_logger
from maxwell_daemon.metrics import http_metrics_middleware, mount_metrics_endpoint

_UI_DIR = _Path(__file__).parent / "ui"
log = get_logger(__name__)


def _mount_web_ui(app: FastAPI) -> None:
    """Serve the vanilla-JS dashboard at ``/ui/``.

    Uses ``StaticFiles`` so there is no per-request Python overhead and
    content-types are set correctly from the file extension.
    """
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    if not _UI_DIR.is_dir():
        return  # Missing assets — skip mounting rather than fail startup.

    app.mount("/ui/", StaticFiles(directory=_UI_DIR, html=True), name="maxwell-daemon-ui")

    @app.get("/ui", include_in_schema=False)
    async def _ui_no_slash() -> RedirectResponse:
        return RedirectResponse(url="/ui/", status_code=308)


def create_app(  # noqa: C901
    daemon: Daemon,
    *,
    auth_token: str | None = None,
    github_client: Any = None,
    audit_log_path: _Path | None = None,
    jwt_config: JWTConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``github_client`` is an optional injection point for tests — if omitted,
    the handlers construct a fresh ``GitHubClient()`` on demand.
    """
    app = FastAPI(
        title="Maxwell-Daemon API",
        version=__version__,
        summary="Autonomous AI control plane orchestrating agent tasks.",
        description=(
            "Maxwell-Daemon exposes a stable HTTP + WebSocket API for "
            "orchestrating agent tasks, observing pipeline state, and "
            "controlling the daemon lifecycle.\n\n"
            f"**Contract version:** `{CONTRACT_VERSION}` (advertised at "
            "`GET /api/version`).  The contract is **append-only**: new "
            "fields and endpoints may appear, but existing request and "
            "response shapes are stable within a major version.\n\n"
            "## Authentication\n\n"
            "Most endpoints require a bearer token in the `Authorization` "
            "header:\n\n"
            "```\nAuthorization: Bearer <your_token>\n```\n\n"
            "When JWT auth is configured, role-based access control "
            "(viewer / operator / admin) is enforced per endpoint.\n\n"
            "## Interactive docs\n\n"
            "* Swagger UI: [`/docs`](/docs)\n"
            "* ReDoc: [`/redoc`](/redoc)\n"
            "* OpenAPI JSON: [`/openapi.json`](/openapi.json)\n"
        ),
        contact={
            "name": "Maxwell-Daemon",
            "url": "https://github.com/D-sorganization/Maxwell-Daemon",
        },
        license_info={
            "name": "MIT",
            "url": "https://github.com/D-sorganization/Maxwell-Daemon/blob/main/LICENSE",
        },
        openapi_tags=[
            {"name": "health", "description": "Liveness and readiness probes."},
            {"name": "version", "description": "Daemon and contract version metadata."},
            {"name": "tasks", "description": "Submit, list, and inspect agent tasks."},
            {
                "name": "control",
                "description": "Privileged daemon control (pause / resume / abort).",
            },
            {"name": "cost", "description": "Cost ledger queries and aggregates."},
            {"name": "backends", "description": "LLM backend discovery and configuration."},
            {"name": "auth", "description": "Authentication and session management."},
            {"name": "fleet", "description": "Multi-repo fleet manifest and dispatch."},
        ],
    )

    # -- Metrics & static UI
    mount_metrics_endpoint(app)
    http_metrics_middleware(app)
    _mount_web_ui(app)

    # -- RFC 7807 problem-detail handler
    from maxwell_daemon.api.problem import install_problem_handler

    install_problem_handler(app)

    # -- QueueSaturationError -> HTTP 429
    from maxwell_daemon.daemon.runner import QueueSaturationError

    @app.exception_handler(QueueSaturationError)
    async def queue_saturation_exception_handler(
        request: Request,
        exc: QueueSaturationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.backoff_seconds)},
        )

    # -- Audit logger
    _audit: AuditLogger | None = (
        AuditLogger(audit_log_path, retention_days=daemon._config.agent.task_retention_days)
        if audit_log_path is not None
        else None
    )

    # -- Auth dependency factories
    auth = make_auth_dep(None if jwt_config is not None else auth_token)

    def _require_viewer() -> Any:
        if jwt_config is not None:
            return make_rbac_dep(
                Role.viewer,
                auth_token,
                jwt_config,
                getattr(daemon, "_auth_store", None),
                _audit,
            )
        return auth

    def _require_operator() -> Any:
        if jwt_config is not None:
            return make_rbac_dep(
                Role.operator,
                auth_token,
                jwt_config,
                getattr(daemon, "_auth_store", None),
                _audit,
            )
        return auth

    def _require_admin() -> Any:
        if jwt_config is not None:
            return make_rbac_dep(
                Role.admin,
                auth_token,
                jwt_config,
                getattr(daemon, "_auth_store", None),
                _audit,
            )
        return auth

    # -- Middleware
    api_cfg = daemon._config.api

    if api_cfg.cors_allowed_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=api_cfg.cors_allowed_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=[
                "X-Request-ID",
                "X-Correlation-ID",
                "RateLimit-Limit",
                "RateLimit-Remaining",
                "RateLimit-Reset",
            ],
        )

    from maxwell_daemon.api.correlation import install_correlation_middleware
    from maxwell_daemon.api.security_headers import install_security_headers

    install_security_headers(app)
    install_correlation_middleware(app)

    if api_cfg.rate_limit_default is not None:
        from maxwell_daemon.api.rate_limit import install_rate_limiter

        install_rate_limiter(
            app,
            default_rate=api_cfg.rate_limit_default.rate,
            default_burst=api_cfg.rate_limit_default.burst,
            groups={
                name: {"rate": g.rate, "burst": g.burst}
                for name, g in api_cfg.rate_limit_groups.items()
            },
        )

    # Per-endpoint rate limit for POST /api/dispatch (issue #796).
    dispatch_rl_cfg = api_cfg.dispatch_rate_limit
    if dispatch_rl_cfg.enabled:
        from maxwell_daemon.api.rate_limit import (
            InMemoryRateLimitStore,
            RateLimitPolicy,
            build_rate_limit_dependency,
            install_rate_limit_headers_middleware,
        )

        dispatch_rate_limit_dep: Any = build_rate_limit_dependency(
            endpoint="dispatch",
            policy=RateLimitPolicy(
                limit=dispatch_rl_cfg.limit,
                window_seconds=float(dispatch_rl_cfg.window_seconds),
            ),
            store=InMemoryRateLimitStore(),
        )
        install_rate_limit_headers_middleware(app)
    else:

        async def _noop_dispatch_rate_limit() -> None:
            return None

        dispatch_rate_limit_dep = _noop_dispatch_rate_limit

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Response:
        """Attach a UUID request-id to every request + response + log line."""
        incoming = request.headers.get("x-request-id", "")
        try:
            request_id = str(uuid.UUID(incoming)) if incoming else str(uuid.uuid4())
        except ValueError:
            request_id = str(uuid.uuid4())
        with bind_context(request_id=request_id):
            response: Response = await call_next(request)
        response.headers["x-request-id"] = request_id
        if _audit is not None and not request.url.path.startswith("/ui"):
            auth_header = request.headers.get("authorization", "")
            if auth_header.lower().startswith("bearer "):
                user: str | None = "Bearer ***"
            elif auth_header:
                user = f"{auth_header.split(' ', 1)[0]} ***"
            else:
                user = None
            _audit.log_api_call(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                user=user,
                request_id=request_id,
            )
        return response

    # -- GitHub client factory
    def _gh() -> Any:
        if github_client is not None:
            return github_client
        from maxwell_daemon.gh import GitHubClient

        return GitHubClient()

    # -- Route modules (epic #896 Phase 1.1)
    from maxwell_daemon.api.routes import actions as _actions_routes
    from maxwell_daemon.api.routes import audit as _audit_routes
    from maxwell_daemon.api.routes import auth as _auth_routes
    from maxwell_daemon.api.routes import backends as _backends_routes
    from maxwell_daemon.api.routes import control_plane as _control_plane_routes
    from maxwell_daemon.api.routes import cost as _cost_routes
    from maxwell_daemon.api.routes import dispatch as _dispatch_routes
    from maxwell_daemon.api.routes import events as _events_routes
    from maxwell_daemon.api.routes import fleet as _fleet_routes
    from maxwell_daemon.api.routes import health as _health_routes
    from maxwell_daemon.api.routes import issues as _issues_routes
    from maxwell_daemon.api.routes import ssh as _ssh_routes
    from maxwell_daemon.api.routes import status as _status_routes
    from maxwell_daemon.api.routes import tasks as _task_routes
    from maxwell_daemon.api.routes import webhooks as _webhooks_routes
    from maxwell_daemon.api.routes import work_items as _work_items_routes

    # Stable operator contract (CLAUDE.md section 1)
    _auth_routes.register(
        app, daemon, jwt_config, auth_token, _require_admin(), _require_operator()
    )
    _health_routes.register(app, daemon)
    _status_routes.register(app, daemon)
    _cost_routes.register(app, daemon, _require_viewer())
    _task_routes.register(app, daemon, auth, _require_viewer(), _require_operator())
    _control_plane_routes.register(app, daemon, auth, _require_viewer(), _require_operator())

    # Dispatch / control -- stable contract surface (CLAUDE.md section 1)
    _dispatch_routes.register(app, daemon, auth_token, dispatch_rate_limit_dep)

    # Domain routers
    _backends_routes.register(app, daemon, _require_viewer(), _require_admin())
    _actions_routes.register(app, daemon, _audit, _require_viewer(), _require_operator(), auth)
    _work_items_routes.register(app, daemon, _require_viewer(), _require_operator(), auth)
    _issues_routes.register(
        app, daemon, _gh, _require_viewer(), _require_operator(), _require_admin(), auth
    )
    _fleet_routes.register(app, daemon, _require_viewer(), _require_operator(), auth)
    _audit_routes.register(
        app,
        daemon,
        _audit,
        audit_log_path,
        _require_viewer(),
        _require_operator(),
        _require_admin(),
    )
    _webhooks_routes.register(app, daemon, _require_viewer(), _require_operator(), auth)
    _ssh_routes.register(
        app,
        daemon,
        auth_token,
        jwt_config,
        _audit,
        _require_admin(),
        auth,
        websocket_auth_or_close,
    )
    _events_routes.register(
        app,
        daemon,
        auth_token,
        jwt_config,
        _audit,
        api_cfg.websocket_max_connections,
        websocket_auth_or_close,
    )

    return app
