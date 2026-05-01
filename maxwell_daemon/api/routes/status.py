"""Status endpoints (phase 2 of #793).

Extracted from ``maxwell_daemon/api/server.py`` so the pipeline-state
endpoints live in their own focused module.  These endpoints are part of
the stable, append-only contract consumed by ``runner-dashboard`` (see
``AGENTS.md`` and ``maxwell_daemon/api/contract.py``).

The shapes returned here MUST match ``StatusResponse`` and
``StatusV2Response`` exactly.  Any breaking change requires bumping
``CONTRACT_VERSION``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

from fastapi import FastAPI

from maxwell_daemon.api.contract import (
    StatusResponse,
    StatusV2Response,
    StatusV2RunningTask,
    StatusV2Tokens,
    StatusV2Totals,
)
from maxwell_daemon.backends.base import TokenUsage
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.daemon.runner import Task, TaskStatus
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


def _status_v2_tokens(usage: TokenUsage | None) -> StatusV2Tokens:
    if usage is None:
        return StatusV2Tokens()
    return StatusV2Tokens(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
    )


def _status_v2_counts(tasks: Sequence[Task]) -> dict[str, int]:
    counts: dict[str, int] = {
        "running": 0,
        "retrying": 0,
        "queued": 0,
        "dispatched": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
    }
    for task in tasks:
        counts[task.status.value] = counts.get(task.status.value, 0) + 1
    return counts


def register(app: FastAPI, daemon: Daemon) -> None:
    """Attach ``GET /api/status`` and ``GET /api/v2/status`` to ``app``."""

    @app.get("/api/status")
    async def api_status() -> StatusResponse:
        try:
            state = daemon.state()
        except Exception:  # noqa: BLE001
            return StatusResponse(
                pipeline_state="error",
                gate="closed",
                sandbox="unknown",
            )
        # Derive pipeline_state from running tasks.
        running_tasks = [t for t in state.tasks.values() if t.status.value == "running"]
        queued_tasks = [t for t in state.tasks.values() if t.status.value == "queued"]
        if running_tasks:
            pipeline_state = "running"
            active_task_id: str | None = running_tasks[0].id
        elif queued_tasks:
            pipeline_state = "running"
            active_task_id = None
        else:
            pipeline_state = "idle"
            active_task_id = None

        gate = "open" if state.backends_available else "closed"
        sandbox_cfg = getattr(daemon._config, "sandbox", None)
        sandbox_enabled = getattr(sandbox_cfg, "enabled", True) if sandbox_cfg else True
        return StatusResponse(
            pipeline_state=pipeline_state,
            active_task_id=active_task_id,
            gate=gate,
            sandbox="enabled" if sandbox_enabled else "disabled",
        )

    @app.get("/api/v2/status")
    async def api_v2_status() -> StatusV2Response:
        generated_at = datetime.now(timezone.utc)
        try:
            state = daemon.state()
        except Exception:  # noqa: BLE001
            return StatusV2Response(
                generated_at=generated_at.isoformat(),
                counts=_status_v2_counts([]),
                running=[],
                retrying=[],
                codex_totals=StatusV2Totals(),
                rate_limits=None,
            )

        tasks = list(state.tasks.values())
        running_tasks = [t for t in tasks if t.status is TaskStatus.RUNNING]
        token_totals_by_task = daemon._ledger.token_totals_by_agent({t.id for t in running_tasks})
        running_since = [t.started_at for t in running_tasks if t.started_at is not None]
        earliest_started_at = min(running_since) if running_since else None
        seconds_running = (
            (generated_at - earliest_started_at).total_seconds()
            if earliest_started_at is not None
            else 0.0
        )

        running = [
            StatusV2RunningTask(
                task_id=t.id,
                session_id=t.turn_session_id,
                run_count=t.turn_count,
                last_event=t.status.value,
                started_at=t.started_at.isoformat() if t.started_at is not None else None,
                dispatched_to=t.dispatched_to,
                tokens=_status_v2_tokens(token_totals_by_task.get(t.id)),
            )
            for t in running_tasks
        ]

        totals = _status_v2_tokens(daemon._ledger.token_totals())
        return StatusV2Response(
            generated_at=generated_at.isoformat(),
            counts=_status_v2_counts(tasks),
            running=running,
            retrying=[],
            codex_totals=StatusV2Totals(
                input_tokens=totals.input_tokens,
                output_tokens=totals.output_tokens,
                total_tokens=totals.total_tokens,
                seconds_running=seconds_running,
            ),
            rate_limits=None,
        )
