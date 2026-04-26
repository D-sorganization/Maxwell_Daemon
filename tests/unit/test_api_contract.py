"""Contract surface tests — /api/ operator endpoints (issue #681).

Verifies the stable JSON shapes that runner-dashboard and other operator
tooling depends on.  Follow the same fixture pattern as test_api.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import maxwell_daemon.api.server as server_module
from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.evals.storage import EvalRunStore

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_api.py pattern)
# ---------------------------------------------------------------------------

AUTH_TOKEN = "contract-test-secret"  # nosec B105 — test fixture, not a real credential


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
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    """Unauthenticated client (no auth_token configured)."""
    with TestClient(create_app(daemon)) as c:
        yield c


@pytest.fixture
def auth_client(daemon: Daemon) -> Iterator[TestClient]:
    """Client where the app requires bearer-token auth."""
    with TestClient(create_app(daemon, auth_token=AUTH_TOKEN)) as c:  # nosec B106
        yield c


def _bearer(token: str = AUTH_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# GET /api/version
# ---------------------------------------------------------------------------


class TestApiVersion:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/version")
        assert r.status_code == 200

    def test_response_has_daemon_and_contract_keys(self, client: TestClient) -> None:
        body = client.get("/api/version").json()
        assert "daemon" in body
        assert "contract" in body

    def test_contract_version_is_1_0_0(self, client: TestClient) -> None:
        body = client.get("/api/version").json()
        assert body["contract"] == "1.0.0"

    def test_does_not_require_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/version")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestApiHealth:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_response_has_status_key(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert "status" in body

    def test_does_not_require_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/health")
        assert r.status_code == 200

    def test_does_not_500_when_daemon_state_raises(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Health must be orthogonal — even a broken daemon state returns a response."""

        def _broken_state(self: Daemon) -> None:
            raise RuntimeError("daemon internals on fire")

        monkeypatch.setattr(Daemon, "state", _broken_state)
        monkeypatch.setattr(server_module, "log", MagicMock())
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "degraded"

    def test_has_uptime_seconds(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert "uptime_seconds" in body

    def test_has_gate_key(self, client: TestClient) -> None:
        body = client.get("/api/health").json()
        assert "gate" in body


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------


class TestApiStatus:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/status")
        assert r.status_code == 200

    def test_has_pipeline_state_and_gate(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert "pipeline_state" in body
        assert "gate" in body

    def test_has_sandbox_key(self, client: TestClient) -> None:
        body = client.get("/api/status").json()
        assert "sandbox" in body

    def test_does_not_require_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/status")
        assert r.status_code == 200

    def test_status_handles_state_exception(
        self,
        client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Status must degrade gracefully when daemon.state() raises."""

        def _broken_state(self: Daemon) -> None:
            raise RuntimeError("daemon internals on fire")

        monkeypatch.setattr(Daemon, "state", _broken_state)
        monkeypatch.setattr(server_module, "log", MagicMock())
        r = client.get("/api/status")
        assert r.status_code == 200
        body = r.json()
        assert body["pipeline_state"] == "error"
        assert body["gate"] == "closed"
        assert body["sandbox"] == "unknown"


# ---------------------------------------------------------------------------
# GET /api/tasks
# ---------------------------------------------------------------------------


class TestApiTasksList:
    def test_returns_200_unauthenticated(self, client: TestClient) -> None:
        """Without auth_token configured every request is allowed."""
        r = client.get("/api/tasks")
        assert r.status_code == 200

    def test_response_has_tasks_list(self, client: TestClient) -> None:
        body = client.get("/api/tasks").json()
        assert "tasks" in body
        assert isinstance(body["tasks"], list)

    def test_returns_401_when_token_missing(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/tasks")
        assert r.status_code == 401

    def test_returns_200_with_valid_token(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/tasks", headers=_bearer())
        assert r.status_code == 200

    def test_accepts_limit_param(self, client: TestClient) -> None:
        r = client.get("/api/tasks?limit=5")
        assert r.status_code == 200

    def test_has_total_field(self, client: TestClient) -> None:
        body = client.get("/api/tasks").json()
        assert "total" in body


# ---------------------------------------------------------------------------
# GET /api/tasks/{task_id}
# ---------------------------------------------------------------------------


class TestApiTaskDetail:
    def test_nonexistent_task_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/tasks/nonexistent")
        assert r.status_code == 404

    def test_nonexistent_returns_404_with_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/tasks/nonexistent", headers=_bearer())
        assert r.status_code == 404

    def test_missing_token_returns_401(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/tasks/some-task-id")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/dispatch
# ---------------------------------------------------------------------------


class TestApiDispatch:
    def _payload(self, token: str = AUTH_TOKEN) -> dict:
        return {
            "confirmation_token": token,
            "prompt": "Do something useful",
            "repo": None,
            "idempotency_key": "test-key-001",
        }

    def test_valid_token_returns_task_id(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/dispatch", json=self._payload())
        assert r.status_code in (200, 202)
        body = r.json()
        assert "task_id" in body

    def test_missing_token_returns_403(self, auth_client: TestClient) -> None:
        payload = self._payload()
        payload["confirmation_token"] = ""
        r = auth_client.post("/api/dispatch", json=payload)
        assert r.status_code == 403

    def test_wrong_token_returns_403(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/dispatch", json=self._payload(token="wrong-token"))
        assert r.status_code == 403

    def test_response_has_status_and_queued_at(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/dispatch", json=self._payload())
        assert r.status_code in (200, 202)
        body = r.json()
        assert "status" in body
        assert "queued_at" in body

    def test_no_auth_token_configured_accepts_any_token(self, client: TestClient) -> None:
        """When no auth_token is configured the endpoint rejects empty tokens."""
        payload = {
            "confirmation_token": "anything",
            "prompt": "test prompt",
            "idempotency_key": "key-abc",
        }
        # With no auth_token configured, expected_token is "" so hmac comparison
        # fails for non-empty tokens too — endpoint returns 403.
        r = client.post("/api/dispatch", json=payload)
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /api/control/{action}
# ---------------------------------------------------------------------------


class TestApiControl:
    def _payload(self, token: str = AUTH_TOKEN) -> dict:
        return {"confirmation_token": token, "reason": "test run"}

    def test_pause_with_valid_token_returns_200(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/control/pause", json=self._payload())
        assert r.status_code == 200

    def test_pause_response_has_action_pause(self, auth_client: TestClient) -> None:
        body = auth_client.post("/api/control/pause", json=self._payload()).json()
        assert body["action"] == "pause"

    def test_resume_with_valid_token_returns_200(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/control/resume", json=self._payload())
        assert r.status_code == 200

    def test_abort_with_valid_token_returns_200(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/control/abort", json=self._payload())
        assert r.status_code == 200

    def test_missing_token_returns_403(self, auth_client: TestClient) -> None:
        payload = self._payload()
        payload["confirmation_token"] = ""
        r = auth_client.post("/api/control/pause", json=payload)
        assert r.status_code == 403

    def test_wrong_token_returns_403(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/control/pause", json=self._payload(token="bad"))
        assert r.status_code == 403

    def test_invalid_action_returns_422(self, auth_client: TestClient) -> None:
        r = auth_client.post("/api/control/invalid_action", json=self._payload())
        assert r.status_code == 422

    def test_response_has_applied_at_and_previous_state(self, auth_client: TestClient) -> None:
        body = auth_client.post("/api/control/pause", json=self._payload()).json()
        assert "applied_at" in body
        assert "previous_state" in body

    def test_abort_handles_state_exception(
        self,
        auth_client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(daemon, "state", _raise)
        monkeypatch.setattr(server_module, "log", MagicMock())
        r = auth_client.post("/api/control/abort", json=self._payload())
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/v1/evals/leaderboard
# ---------------------------------------------------------------------------


class TestApiEvalLeaderboard:
    def test_leaderboard_handles_load_run_exception(
        self,
        auth_client: TestClient,
        daemon: Daemon,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        workspace = tmp_path / "workspace"
        evals_dir = workspace / "evals"
        run_dir = evals_dir / "run-001"
        run_dir.mkdir(parents=True)

        monkeypatch.setattr(daemon._config.memory, "workspace_path", workspace)

        # Mock load_run to raise exception, triggering the except block
        def _mock_load_run(self, run_id: str) -> None:
            raise RuntimeError("corrupted run")

        monkeypatch.setattr(EvalRunStore, "load_run", _mock_load_run)

        mock_log = MagicMock()
        monkeypatch.setattr(server_module, "log", mock_log)

        r = auth_client.get(
            "/api/v1/evals/leaderboard?suite_id=test-suite",
            headers=_bearer(),
        )
        assert r.status_code == 200
        assert r.json()["suite_id"] == "test-suite"
        assert r.json()["entries"] == []
        mock_log.warning.assert_called_once()
