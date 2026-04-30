"""Integration-level contract tests for the operator-facing /api surface.

Phase 1 of issue #800. These tests pin the documented JSON shapes that
``runner-dashboard`` consumes from a live FastAPI app wrapped in
``TestClient``.  They reuse the fixture pattern from
``tests/unit/test_api_contract.py`` (which itself mirrors
``tests/unit/test_api.py``) so future contract checks have a single,
familiar shape.

Source of truth for the response models: ``maxwell_daemon/api/contract.py``.
Per ``CLAUDE.md`` the HTTP contract is append-only — these tests will fail
loudly if a field is renamed or removed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.api.contract import (
    CONTRACT_VERSION,
    HealthResponse,
    StatusResponse,
    VersionResponse,
)
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

# ---------------------------------------------------------------------------
# Fixtures (mirrors tests/unit/test_api.py and tests/unit/test_api_contract.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path: Path,
    tmp_path: Path,
) -> Iterator[Daemon]:
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
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon)) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestApiHealthContract:
    def test_returns_documented_schema(self, client: TestClient) -> None:
        """Body must validate against ``HealthResponse`` and report ok status."""
        response = client.get("/api/health")
        assert response.status_code == 200

        body = response.json()
        # Pydantic validation guarantees the shape stays in lockstep with contract.py.
        parsed = HealthResponse.model_validate(body)
        assert parsed.status in {"ok", "degraded"}
        assert parsed.uptime_seconds >= 0.0
        assert parsed.gate in {"open", "closed"}

    def test_response_keys_match_contract_model(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        # Model fields the dashboard relies on; extra fields are OK (append-only),
        # but documented fields must always be present.
        for required in ("status", "uptime_seconds", "gate"):
            assert required in body, f"missing documented field {required!r}"


# ---------------------------------------------------------------------------
# GET /api/version
# ---------------------------------------------------------------------------


class TestApiVersionContract:
    def test_returns_semver_and_contract_version(self, client: TestClient) -> None:
        response = client.get("/api/version")
        assert response.status_code == 200

        body = response.json()
        parsed = VersionResponse.model_validate(body)

        # contract version is pinned by CONTRACT_VERSION in contract.py
        assert parsed.contract == CONTRACT_VERSION

        # Daemon version should look like a semver-ish string ("X.Y.Z" or
        # "X.Y.Z<suffix>" — we don't enforce strict PEP 440 here, but it must
        # contain at least two dots so the dashboard can parse it.
        assert parsed.daemon, "daemon version must be a non-empty string"
        assert parsed.daemon.count(".") >= 2, (
            f"daemon version {parsed.daemon!r} doesn't look like semver"
        )


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


class TestApiStatusContract:
    def test_returns_documented_fields_only(self, client: TestClient) -> None:
        """Status payload exposes exactly the contract.py StatusResponse shape.

        The dashboard relies on the closed set ``{pipeline_state, active_task_id,
        gate, sandbox}``.  This test fails loudly if a field is added without a
        contract bump or removed without a major-version bump.
        """
        response = client.get("/api/status")
        assert response.status_code == 200

        body = response.json()
        parsed = StatusResponse.model_validate(body)

        assert set(body) == {"pipeline_state", "active_task_id", "gate", "sandbox"}
        assert parsed.pipeline_state in {"idle", "running", "paused", "error"}
        assert parsed.gate in {"open", "closed"}
        assert parsed.sandbox in {"enabled", "disabled", "unknown"}


# ---------------------------------------------------------------------------
# Error response shape (404 on a nonexistent task)
# ---------------------------------------------------------------------------


class TestApiErrorContract:
    def test_404_for_nonexistent_task_has_consistent_shape(self, client: TestClient) -> None:
        """FastAPI default error envelope is ``{"detail": "<message>"}``.

        The dashboard surfaces ``detail`` to operators; if we ever switch to a
        custom error model this test will catch it before the dashboard does.
        """
        response = client.get("/api/v1/tasks/nonexistent-task-id-9d3f")

        assert response.status_code == 404
        body = response.json()
        assert "detail" in body
        assert isinstance(body["detail"], str)
        assert body["detail"], "error detail must not be empty"
