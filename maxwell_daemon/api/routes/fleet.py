"""Fleet, workers, delegate sessions, heartbeat, and push endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status

from maxwell_daemon.api.contract import WorkersStatusResponse
from maxwell_daemon.core.delegate_lifecycle import DelegateSessionSnapshot, DelegateSessionStatus
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = ["register"]


def _parse_delegate_status(value: str | None) -> DelegateSessionStatus | None:
    if value is None:
        return None
    try:
        return DelegateSessionStatus(value)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"invalid status {value!r}; choices: {[s.value for s in DelegateSessionStatus]}",
        ) from exc


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    require_viewer: Any,
    require_operator: Any,
    auth: Any,
) -> None:
    """Attach fleet, workers, delegate sessions, and heartbeat endpoints to ``app``."""

    @app.post("/api/v1/push/subscribe", dependencies=[Depends(require_viewer)])
    async def push_subscribe(request: Request) -> dict[str, str]:
        """Register a Web Push subscription.

        Currently a stub. Real implementation requires storing the subscription
        and generating a VAPID keypair.
        """
        body = await request.json()
        if not body or "endpoint" not in body:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid subscription")
        return {
            "status": "subscribed",
            "message": "Push notification subscription recorded.",
        }

    @app.post("/api/v1/heartbeat", dependencies=[Depends(auth)])
    async def worker_heartbeat(request: Request) -> dict[str, Any]:
        """Workers POST here every heartbeat_seconds to stay registered as alive."""
        body = await request.json()
        machine_name = str(body.get("machine_name") or "")
        if not machine_name:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "machine_name required")
        daemon.record_worker_heartbeat(machine_name)
        return {
            "machine_name": machine_name,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/fleet", dependencies=[Depends(require_viewer)])
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
                    with p.open(encoding="utf-8") as fh:
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
            if t.status.value in ("queued", "running", "dispatched"):
                active_by_repo[repo_name] = active_by_repo.get(repo_name, 0) + 1
            cost_by_repo[repo_name] = cost_by_repo.get(repo_name, 0.0) + t.cost_usd

        machines_summary: list[dict[str, Any]] = []
        if daemon._config.role == "coordinator" and daemon._config.fleet_machines:
            from maxwell_daemon.fleet.client import RemoteDaemonClient
            from maxwell_daemon.fleet.dispatcher import MachineState

            initial = tuple(
                MachineState(
                    name=m.name,
                    host=m.host,
                    port=m.port,
                    capacity=m.capacity,
                    tags=tuple(m.tags),
                )
                for m in daemon._config.fleet_machines
            )
            fleet_client = RemoteDaemonClient(auth_token=daemon._config.api_auth_token)
            try:
                probed = await fleet_client.refresh_all(initial)
            except Exception:  # noqa: BLE001
                probed = initial

            dispatched_per_machine: dict[str, int] = {}
            for t in tasks:
                from maxwell_daemon.daemon.runner import TaskStatus

                if t.status is TaskStatus.DISPATCHED and t.dispatched_to:
                    dispatched_per_machine[t.dispatched_to] = (
                        dispatched_per_machine.get(t.dispatched_to, 0) + 1
                    )

            for m in probed:
                last_seen = daemon._worker_last_seen.get(m.name)
                machines_summary.append(
                    {
                        "name": m.name,
                        "host": m.host,
                        "port": m.port,
                        "capacity": m.capacity,
                        "healthy": m.healthy,
                        "dispatched_tasks": dispatched_per_machine.get(m.name, 0),
                        "last_seen": last_seen.isoformat() if last_seen else None,
                    }
                )

        repos: list[dict[str, Any]] = []
        for r in repos_raw:
            name: str = r.get("name", "")
            org: str = r.get("org", "")
            repos.append(
                {
                    "name": name,
                    "org": org,
                    "github_url": (f"https://github.com/{org}/{name}" if org and name else None),
                    "slots": r.get("slots", default_slots),
                    "budget_per_story": r.get("budget_per_story", default_budget),
                    "pr_target_branch": r.get("pr_target_branch", default_branch),
                    "watch_labels": r.get("watch_labels", default_labels),
                    "active_tasks": active_by_repo.get(name, 0),
                    "total_cost_usd": round(cost_by_repo.get(name, 0.0), 6),
                }
            )

        result: dict[str, Any] = {
            "role": daemon._config.role,
            "fleet": {
                "name": fleet_section.get("name", ""),
                "auto_promote_staging": fleet_section.get("auto_promote_staging", False),
                "discovery_interval_seconds": fleet_section.get("discovery_interval_seconds", 300),
            },
            "repos": repos,
        }
        if machines_summary:
            result["machines"] = machines_summary
        return result

    @app.get(
        "/api/v1/fleet/capabilities",
        dependencies=[Depends(require_viewer)],
    )
    @app.get("/api/v1/fleet/nodes", dependencies=[Depends(require_viewer)])
    async def fleet_capabilities(
        repo: str = Query(..., min_length=1),
        tool: str = Query(..., min_length=1),
        required_capability: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        """Return a redacted capability registry snapshot for dispatch decisions."""
        status_view = daemon.fleet_registry.describe(
            repo=repo,
            tool=tool,
            required_capabilities=tuple(required_capability or ()),
        )
        return status_view.to_dict()

    @app.get(
        "/api/v1/workers",
        dependencies=[Depends(require_viewer)],
        response_model=WorkersStatusResponse,
    )
    async def workers_status() -> WorkersStatusResponse:
        """Return current worker count and queue depth."""
        state = daemon.state()
        return WorkersStatusResponse(worker_count=state.worker_count, queue_depth=state.queue_depth)

    @app.put("/api/v1/workers", dependencies=[Depends(require_operator)])
    async def set_workers(count: int) -> dict[str, Any]:
        """Rescale the worker pool to *count* workers."""
        try:
            await daemon.set_worker_count(count)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
        return {"worker_count": count}

    @app.get(
        "/api/v1/delegate-sessions",
        dependencies=[Depends(require_viewer)],
        response_model=list[DelegateSessionSnapshot],
    )
    async def list_delegate_sessions(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        delegate_id: Annotated[str | None, Query()] = None,
        work_item_id: Annotated[str | None, Query()] = None,
        task_id: Annotated[str | None, Query()] = None,
        status: Annotated[str | None, Query()] = None,
    ) -> list[DelegateSessionSnapshot]:
        """List durable delegate sessions and their latest recovery evidence."""
        parsed_status = _parse_delegate_status(status)
        return daemon.delegate_lifecycle.list_sessions(
            limit=limit,
            delegate_id=delegate_id,
            work_item_id=work_item_id,
            task_id=task_id,
            status=parsed_status,
        )

    @app.get(
        "/api/v1/delegate-sessions/{session_id}",
        dependencies=[Depends(require_viewer)],
        response_model=DelegateSessionSnapshot,
    )
    async def get_delegate_session(session_id: str) -> DelegateSessionSnapshot:
        """Return one durable delegate session snapshot."""
        snapshot = daemon.delegate_lifecycle.snapshot(session_id)
        return snapshot
