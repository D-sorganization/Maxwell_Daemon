"""End-to-end flow — config → daemon → API → cost ledger round-trip.

These tests stay hermetic (no external network) by using a fake backend; the
point is to exercise the wiring between components, not the LLM providers
themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig, save_config
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def e2e_config_path(tmp_path: Path, register_recording_backend: None) -> Path:
    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "m-primary"},
                "local": {"type": "recording", "model": "m-local"},
            },
            "agent": {"default_backend": "primary"},
            "repos": [
                {
                    "name": "user/cheap-repo",
                    "path": str(tmp_path / "cheap"),
                    "backend": "local",
                },
            ],
            "budget": {"monthly_limit_usd": 100.0, "hard_stop": False},
        }
    )
    path = tmp_path / "maxwell-daemon.yaml"
    save_config(cfg, path)
    return path


@pytest.fixture
def live_system(
    e2e_config_path: Path, tmp_path: Path
) -> Iterator[tuple[Daemon, TestClient, asyncio.AbstractEventLoop]]:
    from maxwell_daemon.config import load_config

    cfg = load_config(e2e_config_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    daemon = Daemon(
        cfg,
        ledger_path=tmp_path / "ledger.db",
        task_store_path=tmp_path / "tasks.db",
    )
    loop.run_until_complete(daemon.start(worker_count=2))
    loop.run_until_complete(asyncio.sleep(0))

    with TestClient(create_app(daemon)) as client:
        try:
            yield daemon, client, loop
        finally:
            loop.run_until_complete(daemon.stop())
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()
            asyncio.set_event_loop(None)


def _wait_for_completion(
    client: TestClient,
    loop: asyncio.AbstractEventLoop,
    task_id: str,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Poll the API while yielding to the shared event loop so workers can run."""
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        t = client.get(f"/api/v1/tasks/{task_id}").json()
        if t["status"] in {"completed", "failed"}:
            return t  # type: ignore[no-any-return]
        # Yield: run the loop long enough for a worker to pick up the task.
        loop.run_until_complete(asyncio.sleep(0.25))
    raise AssertionError(f"task did not complete: {t}")


class TestEndToEnd:
    def test_submit_task_via_api_records_cost(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "hello world"})
        assert r.status_code == 202, r.json()
        final = _wait_for_completion(client, loop, r.json()["id"])
        assert final["status"] == "completed"

        cost = client.get("/api/v1/cost").json()
        assert cost["month_to_date_usd"] > 0
        assert "primary" in cost["by_backend"]

    def test_repo_override_routes_to_different_backend(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "hello there", "repo": "user/cheap-repo"})
        assert r.status_code == 202, r.json()
        final = _wait_for_completion(client, loop, r.json()["id"])
        print(f"DEBUG: {final}")

        cost = client.get("/api/v1/cost").json()
        assert "local" in cost["by_backend"]

    def test_metrics_endpoint_reflects_activity(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "metrics test"})
        _wait_for_completion(client, loop, r.json()["id"])

        metrics = client.get("/metrics").text
        assert "maxwell_daemon_requests_total" in metrics
        assert 'status="success"' in metrics

    def test_healthz_endpoint_returns_200_when_backends_available(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, _ = live_system
        r = client.get("/healthz")
        assert r.status_code == 200, r.json()
        assert r.json()["status"] == "ready"

    def test_metrics_endpoint_records_http_requests(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, _ = live_system

        # Trigger an HTTP request
        r = client.get("/healthz")
        assert r.status_code == 200

        metrics = client.get("/metrics").text
        assert "maxwell_daemon_http_requests_total" in metrics
        assert 'method="GET"' in metrics
        assert 'endpoint="/healthz"' in metrics
        assert "maxwell_daemon_http_request_duration_seconds" in metrics
