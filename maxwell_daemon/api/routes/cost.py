"""Cost endpoints (phase 2 of #793).

Extracted from ``maxwell_daemon/api/server.py`` so the billing / cost
aggregation endpoints live in their own focused module.  These endpoints are
part of the stable, append-only contract consumed by ``runner-dashboard``
(see ``AGENTS.md`` and ``maxwell_daemon/api/contract.py``).

The shape returned here MUST match ``CostSummary`` exactly.  Any breaking
change requires bumping ``CONTRACT_VERSION``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI

from maxwell_daemon.api.contract import CostSummary
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)


def register(app: FastAPI, daemon: Daemon, auth_dep: Any) -> None:
    """Attach ``GET /api/v1/cost`` to ``app``."""

    @app.get("/api/v1/cost", dependencies=[Depends(auth_dep)])
    async def cost_summary() -> CostSummary:
        now = datetime.now(timezone.utc)
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return CostSummary(
            month_to_date_usd=daemon._ledger.month_to_date(),
            by_backend=daemon._ledger.by_backend(start),
        )
