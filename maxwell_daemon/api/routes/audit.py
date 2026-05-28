"""Audit log, config-reload, and admin endpoints.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896 Phase 1.1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path as _Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status

from maxwell_daemon.audit import AuditLogger
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = ["register"]


def register(
    app: FastAPI,
    daemon: Daemon,
    audit: AuditLogger | None,
    audit_log_path: _Path | None,
    require_viewer: Any,
    require_operator: Any,
    require_admin: Any,
) -> None:
    """Attach audit, reload, and admin endpoints to ``app``."""

    @app.get("/api/v1/audit", dependencies=[Depends(require_viewer)])
    async def audit_log(
        limit: int = Query(default=200, ge=1, le=10_000),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        """Return paginated audit log entries (oldest first)."""
        if audit is None:
            return {"entries": [], "audit_enabled": False}
        return {"entries": audit.entries(limit=limit, offset=offset), "audit_enabled": True}

    @app.get("/api/v1/audit/verify", dependencies=[Depends(require_viewer)])
    async def audit_verify() -> dict[str, Any]:
        """Verify the audit log hash chain.  Returns violations (empty = clean)."""
        from maxwell_daemon.audit import verify_chain

        if audit is None or audit_log_path is None:
            return {"clean": True, "violations": [], "audit_enabled": False}
        violations = verify_chain(audit_log_path)
        return {
            "clean": len(violations) == 0,
            "violations": violations,
            "audit_enabled": True,
        }

    @app.post("/api/reload", dependencies=[Depends(require_operator)])
    async def reload_config() -> dict[str, Any]:
        """Reload daemon config from disk without restarting."""
        try:
            path = daemon.reload_config()
        except FileNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"config reload failed: {exc}",
            ) from exc
        return {
            "status": "reloaded",
            "config_path": str(path),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v1/admin/prune", dependencies=[Depends(require_admin)])
    async def prune_history(
        older_than_days: Annotated[int | None, Query(ge=0)] = None,
    ) -> dict[str, Any]:
        """Run retention pruning on demand."""
        days = (
            daemon._config.agent.task_retention_days if older_than_days is None else older_than_days
        )
        result = daemon.prune_retained_history(days)
        audit_removed = audit.rotate() if audit is not None else 0
        return {
            "older_than_days": days,
            "tasks_pruned": result["tasks"],
            "ledger_records_pruned": result["ledger_records"],
            "audit_entries_pruned": audit_removed,
        }
