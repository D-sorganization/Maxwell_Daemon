"""Webhook receiver and eval endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Request, Response, status
from pydantic import BaseModel, Field

from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "EvalLeaderboardEntry",
    "EvalRunRequest",
    "WebhookTriggerRequest",
    "register",
]


class WebhookTriggerRequest(BaseModel):
    """Body accepted by ``POST /api/webhooks/trigger``."""

    prompt: str = Field(..., min_length=1)
    repo: str | None = None
    backend: str | None = None
    priority: int = Field(default=100, ge=0, le=1000)


class EvalRunRequest(BaseModel):
    suite_id: str
    backends: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)


class EvalLeaderboardEntry(BaseModel):
    backend: str
    model: str
    score: float
    latency_p50: float | None = None
    latency_p95: float | None = None
    cost: float
    pass_rate: float


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    require_viewer: Any,
    require_operator: Any,
    auth: Any,
) -> None:
    """Attach webhook and eval endpoints to ``app``."""

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

        config_secret = daemon._config.github_webhook_secret_value()
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
            for r in daemon._config.github_routes
        ]
        router = WebhookRouter(
            WebhookConfig(
                secret=config_secret,
                allowed_repos=daemon._config.github_allowed_repos,
                routes=routes,
            ),
            daemon=daemon,
        )
        dispatches = router.handle(event_type=event_type, payload=payload)
        return JSONResponse(
            {"event": event_type, "dispatched": len(dispatches)},
            status_code=status.HTTP_200_OK,
        )

    @app.post("/api/v1/evals/run", dependencies=[Depends(auth), Depends(require_operator)])
    async def run_evals(payload: EvalRunRequest) -> dict[str, Any]:
        """Kick off an evaluation run."""
        import asyncio

        from maxwell_daemon.evals.runner import EvalRunner
        from maxwell_daemon.evals.storage import EvalRunStore

        output_root = daemon._config.memory.workspace_path / "evals"
        runner = EvalRunner(output_root)
        store = EvalRunStore(output_root)

        def _do_run() -> None:
            run, results = runner.run(scenario_ids=[payload.suite_id], allow_non_fixture=True)
            store.save(run, results)

        asyncio.get_running_loop().run_in_executor(None, _do_run)
        return {"status": "started", "suite_id": payload.suite_id}

    @app.get("/api/v1/evals/leaderboard", dependencies=[Depends(require_viewer)])
    async def get_eval_leaderboard(suite_id: str) -> dict[str, Any]:
        """Return sortable leaderboard results for a suite."""
        from maxwell_daemon.evals.storage import EvalRunStore

        output_root = daemon._config.memory.workspace_path / "evals"
        store = EvalRunStore(output_root)
        entries: list[EvalLeaderboardEntry] = []

        if output_root.exists():
            for run_dir in output_root.iterdir():
                if not run_dir.is_dir():
                    continue
                backend = "unknown"
                model = "unknown"
                try:
                    run = store.load_run(run_dir.name)
                    if suite_id in run.scenario_ids:
                        results = store.load_results(run_dir.name)
                        for res in results:
                            if res.scenario_id == suite_id:
                                backend = "local"
                                model = "scripted-agent"
                                if run.external_agent_adapter_ids:
                                    backend = run.external_agent_adapter_ids[0]
                                if run.model_profile_ids:
                                    model = run.model_profile_ids[0]
                                entries.append(
                                    EvalLeaderboardEntry(
                                        backend=backend,
                                        model=model,
                                        score=res.score_total,
                                        cost=sum(res.cost_summary.values()),
                                        pass_rate=1.0 if res.status.value == "passed" else 0.0,
                                    )
                                )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "Failed to build leaderboard entry for %s/%s",
                        backend,
                        model,
                        exc_info=True,
                    )

        return {"suite_id": suite_id, "entries": [e.model_dump() for e in entries]}

    @app.post("/api/webhooks/trigger", dependencies=[Depends(require_operator)])
    async def generic_webhook_trigger(
        request: Request,
        body: Annotated[WebhookTriggerRequest, Body()],
    ) -> Response:
        """Trigger a task from any external source via a generic HTTP webhook."""
        from fastapi.responses import JSONResponse

        from maxwell_daemon.triggers.webhook import (
            WebhookTriggerPayload,
            enqueue_webhook_task,
            verify_webhook_signature,
        )

        webhook_secret: str | None = getattr(daemon._config, "webhook_secret", None)
        if webhook_secret:
            raw_body = await request.body()
            sig_header = request.headers.get("x-maxwell-signature", "")
            if not verify_webhook_signature(webhook_secret, raw_body, sig_header):
                return JSONResponse(
                    {"detail": "invalid signature"},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                )

        idempotency_key: str | None = request.headers.get("x-idempotency-key")

        trigger_payload = WebhookTriggerPayload(
            prompt=body.prompt,
            repo=body.repo,
            backend=body.backend,
            priority=body.priority,
            idempotency_key=idempotency_key,
        )
        result = enqueue_webhook_task(trigger_payload, daemon=daemon)

        if result.error:
            return JSONResponse(
                {"detail": result.error},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if result.duplicate:
            return JSONResponse(
                {"duplicate": True, "task_id": None},
                status_code=status.HTTP_200_OK,
            )
        return JSONResponse(
            {"task_id": result.task_id, "duplicate": False},
            status_code=status.HTTP_202_ACCEPTED,
        )
