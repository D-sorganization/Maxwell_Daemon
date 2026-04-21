"""FastAPI app exposing the daemon over HTTP.

Endpoints (v1):
    GET  /health
    GET  /api/v1/backends
    POST /api/v1/tasks           — submit a task
    GET  /api/v1/tasks           — list tasks
    GET  /api/v1/tasks/{id}      — fetch a task
    GET  /api/v1/cost            — cost summary (month-to-date + by backend)

Auth: simple bearer token from `api.auth_token` in config. For production,
terminate TLS at the reverse proxy (nginx, caddy) or enable TLS in uvicorn.
"""

from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field, field_validator

from maxwell_daemon import __version__
from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task
from maxwell_daemon.logging import bind_context
from maxwell_daemon.metrics import mount_metrics_endpoint

_UI_DIR = _Path(__file__).parent / "ui"


def _mount_web_ui(app: FastAPI) -> None:
    """Serve the vanilla-JS dashboard at ``/ui/``.

    Uses ``StaticFiles`` so there's no per-request Python overhead and
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


class TaskSubmit(BaseModel):
    prompt: str = Field(..., min_length=1)
    repo: str | None = None
    backend: str | None = None
    model: str | None = None


class IssueCreate(BaseModel):
    repo: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(..., min_length=1)
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    dispatch: bool = False
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")


class IssueDispatch(BaseModel):
    repo: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    number: int = Field(..., ge=1)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")
    backend: str | None = None
    model: str | None = None


class IssueBatchDispatch(BaseModel):
    items: list[IssueDispatch] = Field(..., min_length=1, max_length=100)


class IssueAbDispatch(BaseModel):
    repo: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
    number: int = Field(..., ge=1)
    backends: list[str] = Field(..., min_length=2, max_length=4)
    mode: str = Field(default="plan", pattern=r"^(plan|implement)$")

    @field_validator("backends")
    @classmethod
    def _check_distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("A/B backends must be distinct")
        return v


class TaskView(BaseModel):
    id: str
    prompt: str
    kind: str
    repo: str | None
    backend: str | None
    model: str | None
    issue_repo: str | None = None
    issue_number: int | None = None
    issue_mode: str | None = None
    ab_group: str | None = None
    pr_url: str | None = None
    status: str
    result: str | None
    error: str | None
    cost_usd: float
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_task(cls, t: Task) -> TaskView:
        return cls(
            id=t.id,
            prompt=t.prompt,
            kind=t.kind.value,
            repo=t.repo,
            backend=t.backend,
            model=t.model,
            issue_repo=t.issue_repo,
            issue_number=t.issue_number,
            issue_mode=t.issue_mode,
            ab_group=t.ab_group,
            pr_url=t.pr_url,
            status=t.status.value,
            result=t.result,
            error=t.error,
            cost_usd=t.cost_usd,
            created_at=t.created_at,
            started_at=t.started_at,
            finished_at=t.finished_at,
        )


class CostSummary(BaseModel):
    month_to_date_usd: float
    by_backend: dict[str, float]


def _auth_dep(token: str | None) -> Any:
    async def _check(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        presented = authorization.removeprefix("Bearer ").strip()
        # Constant-time comparison — prevents leaking token via response timing.
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    return _check


def _make_rbac_dep(
    minimum: Role,
    static_token: str | None,
    jwt_config: JWTConfig | None,
) -> Any:
    """Return a FastAPI dependency that enforces *minimum* role.

    Accepts EITHER:
    - A valid static admin bearer token (treated as Role.admin), OR
    - A valid JWT bearer token whose role is >= *minimum*.

    When neither JWT config nor static token is configured, all requests pass
    (open/dev mode — same behaviour as the existing ``_auth_dep(None)``).
    """

    async def _dep(authorization: Annotated[str | None, Header()] = None) -> None:
        # Open mode — nothing to enforce.
        if static_token is None and jwt_config is None:
            return

        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")

        raw = authorization.removeprefix("Bearer ").strip()

        # Fast path: static admin token — always grants admin-level access.
        if static_token is not None and hmac.compare_digest(raw.encode(), static_token.encode()):
            return  # admitted as admin

        # JWT path.
        if jwt_config is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

        try:
            claims = jwt_config.decode_token(raw)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc

        if not claims.has_role(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
            )

    return _dep


def create_app(
    daemon: Daemon,
    *,
    auth_token: str | None = None,
    github_client: Any = None,
    audit_log_path: _Path | None = None,
    jwt_config: JWTConfig | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    `github_client` is an optional injection point for tests — if omitted, the
    handlers construct a fresh ``GitHubClient()`` on demand.
    """
    app = FastAPI(
        title="Maxwell-Daemon API",
        version=__version__,
        description="Remote control plane for Maxwell-Daemon daemons.",
    )
    mount_metrics_endpoint(app)
    _mount_web_ui(app)
    auth = _auth_dep(auth_token)

    # Role-scoped RBAC dependencies: accept static admin token OR JWT with role >= minimum.
    _rbac_viewer = _make_rbac_dep(Role.viewer, auth_token, jwt_config)
    _rbac_operator = _make_rbac_dep(Role.operator, auth_token, jwt_config)
    _rbac_admin = _make_rbac_dep(Role.admin, auth_token, jwt_config)

    _audit: AuditLogger | None = AuditLogger(audit_log_path) if audit_log_path is not None else None

    # Rate-limit middleware — installs only when config declares a default group.
    api_cfg = daemon._config.api
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
            user = auth_header.removeprefix("Bearer ").strip() if auth_header else None
            _audit.log_api_call(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                user=user,
                request_id=request_id,
            )
        return response

    def _gh() -> Any:
        if github_client is not None:
            return github_client
        from maxwell_daemon.gh import GitHubClient

        return GitHubClient()

    # ── JWT auth endpoints ────────────────────────────────────────────────────

    class TokenRequest(BaseModel):
        subject: str = Field(..., min_length=1, max_length=128)
        role: str = Field(default="viewer", pattern=r"^(admin|operator|viewer|developer)$")
        expiry_seconds: int | None = Field(default=None, ge=1, le=86400 * 30)

    class TokenResponse(BaseModel):
        access_token: str
        token_type: str = "bearer"
        expires_in: int
        role: str

    @app.post("/api/v1/auth/token", dependencies=[Depends(auth)])
    async def issue_token(payload: TokenRequest) -> TokenResponse:
        """Issue a JWT with the requested role.

        Requires the static bearer token (admin action).  The resulting JWT
        can then be used in place of the static token for role-scoped access.
        """
        if jwt_config is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "JWT not configured — set api.jwt_secret in config",
            )
        ttl = payload.expiry_seconds or jwt_config.expiry_seconds
        role = Role(payload.role)
        token = jwt_config.create_token(payload.subject, role, expiry_seconds=ttl)
        return TokenResponse(access_token=token, expires_in=ttl, role=role.value)

    @app.get("/api/v1/auth/me")
    async def whoami(authorization: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        """Decode and return the caller's JWT claims (or static-token identity)."""
        if jwt_config is not None and authorization and authorization.startswith("Bearer "):
            raw = authorization.removeprefix("Bearer ").strip()
            try:
                claims = jwt_config.decode_token(raw)
                return {"sub": claims.sub, "role": claims.role.value, "exp": claims.exp.isoformat()}
            except Exception:  # nosec B110 — invalid/expired JWT, fall through to token check
                pass
        if auth_token is not None and authorization:
            raw = authorization.removeprefix("Bearer ").strip()
            if hmac.compare_digest(raw.encode(), auth_token.encode()):
                return {"sub": "static-token", "role": "admin", "exp": None}
        return {"sub": "anonymous", "role": None, "exp": None}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        state = daemon.state()
        return {
            "status": "ok",
            "version": state.version,
            "uptime_seconds": (datetime.now(timezone.utc) - state.started_at).total_seconds(),
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, Any]:
        state = daemon.state()
        if not state.backends_available:
            raise HTTPException(status_code=503, detail="no backends available")
        return {"status": "ready"}

    @app.get("/api/v1/backends", dependencies=[Depends(_rbac_viewer)])
    async def list_backends() -> dict[str, Any]:
        return {"backends": daemon.state().backends_available}

    @app.post(
        "/api/v1/tasks",
        dependencies=[Depends(_rbac_operator)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_task(payload: TaskSubmit) -> TaskView:
        task = daemon.submit(
            payload.prompt,
            repo=payload.repo,
            backend=payload.backend,
            model=payload.model,
        )
        return TaskView.from_task(task)

    @app.get("/api/v1/tasks", dependencies=[Depends(_rbac_viewer)])
    async def list_tasks(
        status: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        repo: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> list[TaskView]:
        tasks = list(daemon.state().tasks.values())
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        if kind:
            tasks = [t for t in tasks if t.kind.value == kind]
        if repo:
            tasks = [t for t in tasks if t.repo == repo or t.issue_repo == repo]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [TaskView.from_task(t) for t in tasks[:limit]]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(_rbac_viewer)])
    async def get_task(task_id: str) -> TaskView:
        t = daemon.get_task(task_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return TaskView.from_task(t)

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(_rbac_operator)])
    async def cancel_task(task_id: str) -> TaskView:
        try:
            cancelled = daemon.cancel_task(task_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found") from None
        except ValueError as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
        return TaskView.from_task(cancelled)

    @app.post(
        "/api/v1/issues",
        dependencies=[Depends(_rbac_admin)],
        status_code=status.HTTP_201_CREATED,
    )
    async def create_issue(payload: IssueCreate) -> dict[str, Any]:
        """Create a GitHub issue. Optionally dispatch the daemon immediately."""
        gh = _gh()
        url = await gh.create_issue(
            payload.repo,
            title=payload.title,
            body=payload.body,
            labels=payload.labels or None,
        )
        result: dict[str, Any] = {"url": url}

        if payload.dispatch:
            import re

            # Anchor to end-of-string so we only match the trailing issue
            # number that ``gh issue create`` actually returns (one URL per
            # line), not an ``/issues/NN`` fragment that happens to appear in
            # the issue body or in a repo slug like ``org/x-issues``.
            match = re.search(r"/issues/(\d+)/?\s*$", url.strip())
            if match:
                number = int(match.group(1))
                task = daemon.submit_issue(
                    repo=payload.repo, issue_number=number, mode=payload.mode
                )
                result["task_id"] = task.id
        return result

    @app.post(
        "/api/v1/issues/dispatch",
        dependencies=[Depends(_rbac_admin)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def dispatch_issue(payload: IssueDispatch) -> TaskView:
        """Queue an existing issue for the daemon to draft a PR for."""
        task = daemon.submit_issue(
            repo=payload.repo,
            issue_number=payload.number,
            mode=payload.mode,
            backend=payload.backend,
            model=payload.model,
        )
        return TaskView.from_task(task)

    @app.post(
        "/api/v1/issues/ab-dispatch",
        dependencies=[Depends(_rbac_admin)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def ab_dispatch_issue(payload: IssueAbDispatch) -> dict[str, Any]:
        """Race multiple backends on the same issue — reviewer picks the winner."""
        available = set(daemon.state().backends_available)
        unknown = [b for b in payload.backends if b not in available]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"unknown backend(s): {', '.join(unknown)}; available: {sorted(available)}",
            )
        try:
            tasks = daemon.submit_issue_ab(
                repo=payload.repo,
                issue_number=payload.number,
                backends=payload.backends,
                mode=payload.mode,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        return {
            "ab_group": tasks[0].ab_group,
            "tasks": [TaskView.from_task(t).model_dump(mode="json") for t in tasks],
        }

    @app.post(
        "/api/v1/issues/batch-dispatch",
        dependencies=[Depends(_rbac_admin)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def batch_dispatch_issues(payload: IssueBatchDispatch) -> dict[str, Any]:
        """Queue up to 100 issues in one call.

        Per-item failures (e.g. submit_issue raising ValueError) are recorded
        but don't abort the batch — callers get back separate dispatched/
        failed counts plus per-item details.
        """
        dispatched: list[TaskView] = []
        failures: list[dict[str, Any]] = []
        for item in payload.items:
            try:
                task = daemon.submit_issue(
                    repo=item.repo,
                    issue_number=item.number,
                    mode=item.mode,
                    backend=item.backend,
                    model=item.model,
                )
                dispatched.append(TaskView.from_task(task))
            except Exception as e:
                failures.append({"repo": item.repo, "number": item.number, "error": str(e)})
        return {
            "dispatched": len(dispatched),
            "failed": len(failures),
            "tasks": [t.model_dump(mode="json") for t in dispatched],
            "failures": failures,
        }

    @app.get("/api/v1/issues/{owner}/{name}", dependencies=[Depends(_rbac_viewer)])
    async def list_repo_issues(
        owner: str, name: str, state: str = "open", limit: int = 25
    ) -> list[dict[str, Any]]:
        gh = _gh()
        issues = await gh.list_issues(f"{owner}/{name}", state=state, limit=limit)
        return [
            {
                "number": i.number,
                "title": i.title,
                "state": i.state,
                "labels": i.labels,
                "url": i.url,
            }
            for i in issues
        ]

    @app.get("/api/v1/fleet", dependencies=[Depends(_rbac_viewer)])
    async def fleet_overview() -> dict[str, Any]:
        """Return fleet manifest data merged with live task counts per repo."""
        import os

        import yaml

        candidates = [
            os.environ.get("MAXWELL_FLEET_CONFIG") or "",
            "./fleet.yaml",
            str(_Path.home() / ".maxwell-daemon" / "fleet.yaml"),
        ]
        raw: dict[str, Any] = {}
        for path in candidates:
            if path:
                p = _Path(path)
                if p.is_file():
                    with p.open() as fh:
                        raw = yaml.safe_load(fh) or {}
                    break

        fleet_section: dict[str, Any] = raw.get("fleet", {})
        repos_raw: list[dict[str, Any]] = raw.get("repos", [])

        default_slots: int = fleet_section.get("default_slots", 2)
        default_budget: float = fleet_section.get("default_budget_per_story", 0.50)
        default_branch: str = fleet_section.get("default_pr_target_branch", "staging")
        default_labels: list[str] = fleet_section.get("default_watch_labels", [])

        tasks = list(daemon.state().tasks.values())
        active_by_repo: dict[str, int] = {}
        cost_by_repo: dict[str, float] = {}
        for t in tasks:
            repo_name = (t.issue_repo or "").split("/")[-1] or t.repo or ""
            if not repo_name:
                continue
            if t.status.value in ("queued", "running"):
                active_by_repo[repo_name] = active_by_repo.get(repo_name, 0) + 1
            cost_by_repo[repo_name] = cost_by_repo.get(repo_name, 0.0) + t.cost_usd

        repos: list[dict[str, Any]] = []
        for r in repos_raw:
            name: str = r.get("name", "")
            org: str = r.get("org", "")
            repos.append(
                {
                    "name": name,
                    "org": org,
                    "github_url": f"https://github.com/{org}/{name}" if org and name else None,
                    "slots": r.get("slots", default_slots),
                    "budget_per_story": r.get("budget_per_story", default_budget),
                    "pr_target_branch": r.get("pr_target_branch", default_branch),
                    "watch_labels": r.get("watch_labels", default_labels),
                    "active_tasks": active_by_repo.get(name, 0),
                    "total_cost_usd": round(cost_by_repo.get(name, 0.0), 6),
                }
            )

        return {
            "fleet": {
                "name": fleet_section.get("name", ""),
                "auto_promote_staging": fleet_section.get("auto_promote_staging", False),
                "discovery_interval_seconds": fleet_section.get("discovery_interval_seconds", 300),
            },
            "repos": repos,
        }

    @app.get("/api/v1/audit", dependencies=[Depends(_rbac_viewer)])
    async def audit_log(
        limit: int = Query(default=200, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """Return paginated audit log entries (oldest first)."""
        if _audit is None:
            return {"entries": [], "audit_enabled": False}
        return {
            "entries": _audit.entries(limit=limit, offset=offset),
            "audit_enabled": True,
        }

    @app.get("/api/v1/audit/verify", dependencies=[Depends(_rbac_viewer)])
    async def audit_verify() -> dict[str, Any]:
        """Verify the audit log hash chain.  Returns violations (empty = clean)."""
        from maxwell_daemon.audit import verify_chain

        if _audit is None or audit_log_path is None:
            return {"clean": True, "violations": [], "audit_enabled": False}
        violations = verify_chain(audit_log_path)
        return {
            "clean": len(violations) == 0,
            "violations": violations,
            "audit_enabled": True,
        }

    @app.get("/api/v1/cost", dependencies=[Depends(_rbac_viewer)])
    async def cost_summary() -> CostSummary:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return CostSummary(
            month_to_date_usd=daemon._ledger.month_to_date(),
            by_backend=daemon._ledger.by_backend(start),
        )

    @app.post("/api/v1/webhooks/github")
    async def github_webhook(request: Request) -> Response:
        """Receive GitHub webhook events.

        Authenticated with HMAC-SHA256 via X-Hub-Signature-256. No bearer-token
        dependency is applied so GitHub's retry delivery system isn't double-gated.
        """
        import json as _json

        from fastapi.responses import JSONResponse

        from maxwell_daemon.gh.webhook import (
            WebhookConfig,
            WebhookRoute,
            WebhookRouter,
            verify_signature,
        )

        body = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")
        event_type = request.headers.get("x-github-event", "")

        config_secret = (
            daemon._config.github.webhook_secret.get_secret_value()
            if daemon._config.github.webhook_secret is not None
            else None
        )
        if config_secret is None:
            return JSONResponse(
                {"detail": "webhooks disabled", "disabled": True},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not verify_signature(config_secret, body, signature):
            return JSONResponse(
                {"detail": "invalid signature"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            payload = _json.loads(body) if body else {}
        except _json.JSONDecodeError:
            return JSONResponse(
                {"detail": "malformed json"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        routes = [
            WebhookRoute(
                event=r.event,
                action=r.action,
                mode=r.mode,  # type: ignore[arg-type]
                label=r.label,
                trigger=r.trigger,
            )
            for r in daemon._config.github.routes
        ]
        router = WebhookRouter(
            WebhookConfig(
                secret=config_secret,
                allowed_repos=daemon._config.github.allowed_repos,
                routes=routes,
            ),
            daemon=daemon,
        )
        dispatches = router.handle(event_type=event_type, payload=payload)
        return JSONResponse(
            {"event": event_type, "dispatched": len(dispatches)},
            status_code=status.HTTP_200_OK,
        )

    # ── SSH endpoints ────────────────────────────────────────────────────────
    # asyncssh is optional (pip install maxwell-daemon[ssh]).  All SSH routes
    # return 503 if it is not installed.

    _ssh_pool_ref: dict[str, Any] = {}  # lazy singleton

    def _ssh_pool() -> Any:
        if "pool" not in _ssh_pool_ref:
            try:
                from maxwell_daemon.ssh.session import SSHSessionPool

                _ssh_pool_ref["pool"] = SSHSessionPool()
            except ImportError:
                return None
        return _ssh_pool_ref.get("pool")

    def _ssh_unavailable() -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"detail": "SSH support not installed — pip install maxwell-daemon[ssh]"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.get("/api/v1/ssh/sessions", dependencies=[Depends(_rbac_admin)])
    async def ssh_sessions() -> Any:
        """List active SSH sessions."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        return {"sessions": pool.sessions()}

    @app.get("/api/v1/ssh/keys", dependencies=[Depends(_rbac_admin)])
    async def ssh_list_keys() -> Any:
        """List machines that have stored SSH keys."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        return {"machines": store.list_machines()}

    @app.get("/api/v1/ssh/keys/{machine}", dependencies=[Depends(_rbac_admin)])
    async def ssh_get_key(machine: str) -> Any:
        """Return the public key for *machine*, generating it if absent."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        store = SSHKeyStore()
        _, pub = store.get_or_generate(machine)
        return {"machine": machine, "public_key": pub}

    @app.delete("/api/v1/ssh/keys/{machine}", dependencies=[Depends(_rbac_admin)])
    async def ssh_delete_key(machine: str) -> Any:
        """Remove stored SSH keys for *machine*."""
        try:
            from maxwell_daemon.ssh.keys import SSHKeyStore
        except ImportError:
            return _ssh_unavailable()
        SSHKeyStore().remove(machine)
        return {"machine": machine, "deleted": True}

    class SSHConnectRequest(BaseModel):
        host: str
        port: int = 22
        user: str
        password: str | None = None

    @app.post("/api/v1/ssh/connect", dependencies=[Depends(_rbac_admin)])
    async def ssh_connect(payload: SSHConnectRequest) -> Any:
        """Open (or reuse) an SSH session and return its summary."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(
            payload.host,
            user=payload.user,
            port=payload.port,
            password=payload.password,
        )
        return {
            "host": payload.host,
            "port": payload.port,
            "user": payload.user,
            "age_seconds": round(session.age_seconds, 1),
        }

    class SSHRunRequest(BaseModel):
        host: str
        port: int = 22
        user: str
        command: str
        timeout_seconds: float = 30.0

    @app.post("/api/v1/ssh/run", dependencies=[Depends(_rbac_admin)])
    async def ssh_run(payload: SSHRunRequest) -> Any:
        """Run a command on a remote machine and return its output."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(payload.host, user=payload.user, port=payload.port)
        result = await session.run(payload.command, timeout=payload.timeout_seconds)
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }

    @app.get("/api/v1/ssh/files", dependencies=[Depends(_rbac_admin)])
    async def ssh_list_files(
        host: str = Query(...),
        user: str = Query(...),
        port: int = Query(default=22),
        path: str = Query(default="/"),
    ) -> Any:
        """List files on a remote machine via SFTP."""
        pool = _ssh_pool()
        if pool is None:
            return _ssh_unavailable()
        session = await pool.get(host, user=user, port=port)
        entries = await session.list_dir(path)
        return {
            "path": path,
            "entries": [
                {
                    "name": e.name,
                    "path": e.path,
                    "size": e.size,
                    "is_dir": e.is_dir,
                    "modified": e.modified,
                }
                for e in entries
            ],
        }

    @app.websocket("/api/v1/ssh/shell")
    async def ssh_shell_ws(ws: WebSocket) -> None:
        """Interactive shell over WebSocket.

        Query params: ``host``, ``user``, ``port`` (default 22), ``token``
        (bearer token for auth), ``command`` (default ``bash``).

        Frames: text frames sent from client are written to stdin.
        Text frames sent to client contain stdout/stderr chunks.
        Session ends when the command exits or the client disconnects.
        Max session duration: 1 hour.
        """
        # WebSocket auth: accept static admin token OR a JWT with admin role.
        if auth_token is not None or jwt_config is not None:
            presented = ws.query_params.get("token") or ""
            authenticated = False
            if auth_token is not None and hmac.compare_digest(
                presented.encode(), auth_token.encode()
            ):
                authenticated = True
            elif jwt_config is not None and presented:
                try:
                    _ws_claims = jwt_config.decode_token(presented)
                    if _ws_claims.has_role(Role.admin):
                        authenticated = True
                except Exception:  # nosec B110 — invalid JWT, fall through
                    pass
            if not authenticated:
                await ws.close(code=1008)
                return

        pool = _ssh_pool()
        if pool is None:
            await ws.accept()
            await ws.send_text('{"error": "SSH not installed"}')
            await ws.close(code=1011)
            return

        host = ws.query_params.get("host") or ""
        user = ws.query_params.get("user") or ""
        port = int(ws.query_params.get("port") or "22")
        command = ws.query_params.get("command") or "bash"

        if not host or not user:
            await ws.accept()
            await ws.send_text('{"error": "host and user are required"}')
            await ws.close(code=1008)
            return

        await ws.accept()
        try:
            session = await pool.get(host, user=user, port=port)
            async for chunk in session.shell_stream(command):
                await ws.send_bytes(chunk)
        except WebSocketDisconnect:
            return
        except Exception as exc:
            import json as _json_mod

            await ws.send_text(_json_mod.dumps({"error": str(exc)}))
            await ws.close(code=1011)

    @app.websocket("/api/v1/events")
    async def events_ws(ws: WebSocket) -> None:
        """Stream daemon events as JSON frames to the client.

        WebSocket auth is intentionally simpler than REST auth here: clients pass
        ``?token=...`` as a query param because browser WebSocket APIs can't set
        headers. Terminate at a proxy for TLS.
        """
        if auth_token is not None:
            presented = ws.query_params.get("token") or ""
            if not hmac.compare_digest(presented.encode(), auth_token.encode()):
                await ws.close(code=1008)
                return
        await ws.accept()
        try:
            async for event in daemon.events.subscribe(queue_size=64):
                await ws.send_text(event.to_json())
        except WebSocketDisconnect:
            return
        except Exception:
            await ws.close(code=1011)

    return app
