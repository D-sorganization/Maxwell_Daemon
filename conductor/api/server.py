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
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field

from conductor import __version__
from conductor.daemon import Daemon
from conductor.daemon.runner import Task
from conductor.logging import bind_context
from conductor.metrics import mount_metrics_endpoint


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


def create_app(
    daemon: Daemon,
    *,
    auth_token: str | None = None,
    github_client: Any = None,
) -> FastAPI:
    """Build the FastAPI app.

    `github_client` is an optional injection point for tests — if omitted, the
    handlers construct a fresh ``GitHubClient()`` on demand.
    """
    app = FastAPI(
        title="Conductor API",
        version=__version__,
        description="Remote control plane for Conductor daemons.",
    )
    mount_metrics_endpoint(app)
    auth = _auth_dep(auth_token)

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
        return response

    def _gh() -> Any:
        if github_client is not None:
            return github_client
        from conductor.gh import GitHubClient

        return GitHubClient()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        state = daemon.state()
        return {
            "status": "ok",
            "version": state.version,
            "uptime_seconds": (datetime.now(timezone.utc) - state.started_at).total_seconds(),
        }

    @app.get("/api/v1/backends", dependencies=[Depends(auth)])
    async def list_backends() -> dict[str, Any]:
        return {"backends": daemon.state().backends_available}

    @app.post(
        "/api/v1/tasks",
        dependencies=[Depends(auth)],
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

    @app.get("/api/v1/tasks", dependencies=[Depends(auth)])
    async def list_tasks() -> list[TaskView]:
        return [TaskView.from_task(t) for t in daemon.state().tasks.values()]

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(auth)])
    async def get_task(task_id: str) -> TaskView:
        t = daemon.get_task(task_id)
        if t is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
        return TaskView.from_task(t)

    @app.post(
        "/api/v1/issues",
        dependencies=[Depends(auth)],
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

            match = re.search(r"/issues/(\d+)", url)
            if match:
                number = int(match.group(1))
                task = daemon.submit_issue(
                    repo=payload.repo, issue_number=number, mode=payload.mode
                )
                result["task_id"] = task.id
        return result

    @app.post(
        "/api/v1/issues/dispatch",
        dependencies=[Depends(auth)],
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

    @app.get("/api/v1/issues/{owner}/{name}", dependencies=[Depends(auth)])
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

    @app.get("/api/v1/cost", dependencies=[Depends(auth)])
    async def cost_summary() -> CostSummary:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return CostSummary(
            month_to_date_usd=daemon._ledger.month_to_date(),
            by_backend=daemon._ledger.by_backend(start),
        )

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
