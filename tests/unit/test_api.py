"""FastAPI REST server — endpoint contracts, auth, and error paths."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def daemon(minimal_config: MaxwellDaemonConfig, isolated_ledger_path) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        yield d
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon)) as c:
        yield c


@pytest.fixture
def auth_client(daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(daemon, auth_token="secret-abc")) as c:
        yield c


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "uptime_seconds" in body
        assert "version" in body

    def test_health_does_not_require_auth(self, auth_client: TestClient) -> None:
        r = auth_client.get("/health")
        assert r.status_code == 200


class TestBackends:
    def test_list_returns_configured_backends(self, client: TestClient) -> None:
        r = client.get("/api/v1/backends")
        assert r.status_code == 200
        assert "primary" in r.json()["backends"]


class TestTaskSubmission:
    def test_submit_returns_202_and_queued_task(self, client: TestClient) -> None:
        r = client.post("/api/v1/tasks", json={"prompt": "hi"})
        assert r.status_code == 202
        body = r.json()
        assert body["prompt"] == "hi"
        assert body["status"] in {"queued", "running", "completed"}
        assert body["id"]

    def test_submit_rejects_empty_prompt(self, client: TestClient) -> None:
        r = client.post("/api/v1/tasks", json={"prompt": ""})
        assert r.status_code == 422

    def test_list_returns_all_tasks(self, client: TestClient) -> None:
        for i in range(3):
            client.post("/api/v1/tasks", json={"prompt": f"t{i}"})
        r = client.get("/api/v1/tasks")
        assert r.status_code == 200
        assert len(r.json()) >= 3

    def test_get_task_by_id(self, client: TestClient) -> None:
        submitted = client.post("/api/v1/tasks", json={"prompt": "x"}).json()
        r = client.get(f"/api/v1/tasks/{submitted['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == submitted["id"]

    def test_get_nonexistent_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/v1/tasks/nonexistent-id")
        assert r.status_code == 404


class TestCostEndpoint:
    def test_cost_summary_structure(self, client: TestClient) -> None:
        r = client.get("/api/v1/cost")
        assert r.status_code == 200
        body = r.json()
        assert "month_to_date_usd" in body
        assert "by_backend" in body
        assert body["month_to_date_usd"] >= 0.0


class TestAuth:
    def test_protected_endpoint_rejects_missing_token(self, auth_client: TestClient) -> None:
        r = auth_client.get("/api/v1/backends")
        assert r.status_code == 401

    def test_rejects_malformed_bearer(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "NotBearer secret-abc"},
        )
        assert r.status_code == 401

    def test_rejects_wrong_token(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401

    def test_accepts_correct_token(self, auth_client: TestClient) -> None:
        r = auth_client.get(
            "/api/v1/backends",
            headers={"Authorization": "Bearer secret-abc"},
        )
        assert r.status_code == 200
