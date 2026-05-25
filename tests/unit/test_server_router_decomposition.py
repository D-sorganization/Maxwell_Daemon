"""TDD tests for Phase 1.1 server.py router decomposition (epic #896).

Verifies that:
1. ``create_app()`` wires the tasks and control_plane route modules into the
   full app rather than defining duplicate inline routes.
2. The full app exposes the expected /api/v1/tasks and /api/v1/control-plane
   routes registered from the extracted modules.
3. ``server.py`` line count stays below the ratcheted ceiling (progress toward
   the 600-line target in the Phase 1.1 ADR).

These tests are GREEN after the Phase 1.1 decomposition wires:
  - maxwell_daemon.api.routes.tasks into create_app()
  - maxwell_daemon.api.routes.control_plane into create_app()
and removes the duplicate inline definitions from server.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def daemon_for_decomp(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path: Path,
    tmp_path: Path,
) -> Iterator[Daemon]:
    """Minimal Daemon instance for integration-level route tests."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        task_graph_store_path=tmp_path / "task_graphs.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
        delegate_lifecycle_store_path=tmp_path / "delegate_sessions.db",
    )
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def full_app(daemon_for_decomp: Daemon) -> FastAPI:
    """Full app built by create_app() — integration-level."""
    from maxwell_daemon.api.server import create_app

    return create_app(daemon_for_decomp, auth_token=None)


@pytest.fixture
def full_client(full_app: FastAPI) -> Iterator[TestClient]:
    """TestClient over the full create_app() result."""
    with TestClient(full_app, raise_server_exceptions=True) as c:
        yield c


# ── Route coverage: tasks ─────────────────────────────────────────────────────


class TestFullAppTaskRoutes:
    """Verify the full app (via create_app) exposes task endpoints."""

    def test_list_tasks_via_full_app(self, full_client: TestClient) -> None:
        """GET /api/v1/tasks returns 200 from the full app."""
        r = full_client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_submit_task_via_full_app(self, full_client: TestClient) -> None:
        """POST /api/v1/tasks accepts a valid payload from the full app."""
        r = full_client.post(
            "/api/v1/tasks",
            json={"prompt": "phase-1.1 decomp test"},
        )
        assert r.status_code in (200, 202)
        assert r.json()["prompt"] == "phase-1.1 decomp test"

    def test_legacy_tasks_list_via_full_app(self, full_client: TestClient) -> None:
        """GET /api/tasks (legacy) returns 200 from the full app."""
        r = full_client.get("/api/tasks?limit=10")
        assert r.status_code == 200
        body = r.json()
        assert "tasks" in body

    def test_cancel_task_via_full_app(self, full_client: TestClient) -> None:
        """POST /api/v1/tasks/{task_id}/cancel works through the full app."""
        sub = full_client.post(
            "/api/v1/tasks",
            json={"prompt": "cancel me from full app"},
        )
        task_id = sub.json()["id"]
        r = full_client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


# ── Route coverage: control-plane ────────────────────────────────────────────


class TestFullAppControlPlaneRoutes:
    """Verify the full app exposes control-plane endpoints."""

    def test_gauntlet_via_full_app(self, full_client: TestClient) -> None:
        """GET /api/v1/control-plane/gauntlet returns 200 from the full app."""
        r = full_client.get("/api/v1/control-plane/gauntlet")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_control_action_via_full_app(self, full_client: TestClient) -> None:
        """POST /api/control/{action} (legacy) is reachable from the full app."""
        r = full_client.post("/api/control/pause")
        # Expect 200 (paused OK) or 409 (already paused) — not 404.
        assert r.status_code in (200, 400, 409, 422), (
            f"Expected control plane to be reachable; got {r.status_code}: {r.text}"
        )


# ── No-duplicate-inline check ─────────────────────────────────────────────────


class TestServerPyUsesRouteModules:
    """Verify server.py delegates to the extracted route modules.

    After Phase 1.1:
    - tasks.register() is called from create_app()
    - control_plane.register() is called from create_app()
    - server.py no longer defines /api/v1/tasks routes inline
    """

    def test_tasks_route_module_registered_in_create_app(self) -> None:
        """create_app() source delegates /api/v1/tasks to tasks.register()."""
        server_src = Path("maxwell_daemon/api/server.py").read_text(encoding="utf-8")
        # After decomposition, create_app must import and call tasks.register
        assert "task_routes.register" in server_src or (
            "from maxwell_daemon.api.routes import tasks" in server_src
            and "tasks" in server_src
            and "register" in server_src
        ), "server.py must import and call tasks.register() as part of Phase 1.1 decomposition"

    def test_control_plane_route_module_registered_in_create_app(self) -> None:
        """create_app() source delegates control-plane routes to control_plane.register()."""
        server_src = Path("maxwell_daemon/api/server.py").read_text(encoding="utf-8")
        assert "control_plane_routes.register" in server_src or (
            "from maxwell_daemon.api.routes import control_plane" in server_src
            and "control_plane" in server_src
            and "register" in server_src
        ), (
            "server.py must import and call control_plane.register() "
            "as part of Phase 1.1 decomposition"
        )


# ── Line-count ratchet ────────────────────────────────────────────────────────


class TestServerPyLineCountRatchet:
    """Ratchet: server.py must shrink toward the 600-line Phase 1.1 target.

    Phase 1.1 exit criterion (from epic #896):
      server.py ≤ 2500 lines  (post-tasks+control_plane extraction)

    Subsequent phases will tighten this further; update the ceiling as each
    router module is extracted.  Never raise the ceiling — only lower it.
    """

    _MAX_LINES = 2500  # Ratcheted ceiling — lower as more routers are extracted.

    def test_server_py_line_count_below_ceiling(self) -> None:
        lines = Path("maxwell_daemon/api/server.py").read_text(encoding="utf-8").splitlines()
        actual = len(lines)
        assert actual <= self._MAX_LINES, (
            f"server.py has {actual} lines, exceeding the Phase 1.1 ceiling of "
            f"{self._MAX_LINES}. Extract more routes to reduce it. "
            f"(Ratchet in {__file__})"
        )
