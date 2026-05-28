"""Dispatch and control endpoints -- stable operator contract surface.

These are the endpoints documented in ``CLAUDE.md`` and consumed by
``runner-dashboard``.  The API shapes are **append-only**: new fields may
appear but existing shapes MUST NOT change without bumping
``CONTRACT_VERSION``.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

import contextlib
import hmac
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, status

from maxwell_daemon.api.contract import (
    ControlRequest,
    ControlResponse,
    DispatchRequest,
    DispatchResponse,
)
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.task_models import DuplicateTaskIdError
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = ["register"]


def register(
    app: FastAPI,
    daemon: Daemon,
    auth_token: str | None,
    dispatch_rate_limit_dep: Any,
) -> None:
    """Attach ``POST /api/dispatch`` and ``POST /api/control/{action}`` to ``app``."""

    @app.post(
        "/api/dispatch",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(dispatch_rate_limit_dep)],
    )
    async def api_dispatch(payload: DispatchRequest) -> DispatchResponse:
        expected_token = auth_token or ""
        if not expected_token or not hmac.compare_digest(
            payload.confirmation_token, expected_token
        ):
            raise HTTPException(status_code=403, detail="invalid confirmation_token")

        log.info(
            "audit: api_dispatch idempotency_key=%s repo=%s",
            payload.idempotency_key,
            payload.repo,
        )
        try:
            task = daemon.submit(
                payload.prompt,
                repo=payload.repo,
                task_id=payload.idempotency_key,
            )
        except DuplicateTaskIdError as exc:
            # 409 Conflict: idempotency_key already exists.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        return DispatchResponse(
            task_id=task.id,
            status=task.status.value,
            queued_at=task.created_at.isoformat(),
        )

    @app.post("/api/control/{action}")
    async def api_control(action: str, payload: ControlRequest) -> ControlResponse:
        valid_actions = {"pause", "resume", "abort"}
        if action not in valid_actions:
            raise HTTPException(
                status_code=422,
                detail=f"action must be one of {sorted(valid_actions)}",
            )

        expected_token = auth_token or ""
        if not expected_token or not hmac.compare_digest(
            payload.confirmation_token, expected_token
        ):
            raise HTTPException(status_code=403, detail="invalid confirmation_token")

        log.info(
            "audit: api_control action=%s reason=%s",
            action,
            payload.reason,
        )

        try:
            state = daemon.state()
            running_tasks = [t for t in state.tasks.values() if t.status.value == "running"]
            previous_state = "running" if running_tasks else "idle"
        except Exception:  # noqa: BLE001
            previous_state = "unknown"

        if action == "abort":
            try:
                state = daemon.state()
                for task_obj in list(state.tasks.values()):
                    if task_obj.status.value in ("running", "queued"):
                        with contextlib.suppress(Exception):
                            daemon.cancel_task(task_obj.id)
            except Exception:  # noqa: BLE001
                log.warning("Error during abort: cancel tasks failed", exc_info=True)

        return ControlResponse(
            action=action,
            applied_at=datetime.now(timezone.utc).isoformat(),
            previous_state=previous_state,
        )
