"""End-to-end flow — config → daemon → API → cost ledger round-trip.

These tests stay hermetic (no external network) by using a fake backend; the
point is to exercise the wiring between components, not the LLM providers
themselves.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conductor.api import create_app
from conductor.config import ConductorConfig, save_config
from conductor.daemon import Daemon


@pytest.fixture
def e2e_config_path(tmp_path: Path, register_recording_backend: None) -> Path:
    cfg = ConductorConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "m-primary"},
                "local": {"type": "recording", "model": "m-local"},
            },
            "agent": {"default_backend": "primary"},
            "repos": [
                {"name": "cheap-repo", "path": str(tmp_path / "cheap"), "backend": "local"},
            ],
            "budget": {"monthly_limit_usd": 100.0, "hard_stop": False},
        }
    )
    path = tmp_path / "conductor.yaml"
    save_config(cfg, path)
    return path


@pytest.fixture
def live_system(
    e2e_config_path: Path, tmp_path: Path
) -> Iterator[tuple[Daemon, TestClient, asyncio.AbstractEventLoop]]:
    from conductor.config import load_config

    cfg = load_config(e2e_config_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    daemon = Daemon(cfg, ledger_path=tmp_path / "ledger.db")
    loop.run_until_complete(daemon.start(worker_count=2))

    with TestClient(create_app(daemon)) as client:
        try:
            yield daemon, client, loop
        finally:
            loop.run_until_complete(daemon.stop())
            loop.close()
            asyncio.set_event_loop(None)


def _wait_for_completion(
    client: TestClient,
    loop: asyncio.AbstractEventLoop,
    task_id: str,
    timeout_s: float = 5.0,
) -> dict:
    """Poll the API while yielding to the shared event loop so workers can run."""
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        t = client.get(f"/api/v1/tasks/{task_id}").json()
        if t["status"] in {"completed", "failed"}:
            return t
        # Yield: run the loop long enough for a worker to pick up the task.
        loop.run_until_complete(asyncio.sleep(0.05))
    raise AssertionError(f"task did not complete: {t}")


class TestEndToEnd:
    def test_submit_task_via_api_records_cost(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "hello world"})
        assert r.status_code == 202
        final = _wait_for_completion(client, loop, r.json()["id"])
        assert final["status"] == "completed"

        cost = client.get("/api/v1/cost").json()
        assert cost["month_to_date_usd"] > 0
        assert "primary" in cost["by_backend"]

    def test_repo_override_routes_to_different_backend(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "hi", "repo": "cheap-repo"})
        assert r.status_code == 202
        _wait_for_completion(client, loop, r.json()["id"])

        cost = client.get("/api/v1/cost").json()
        assert "local" in cost["by_backend"]

    def test_metrics_endpoint_reflects_activity(
        self, live_system: tuple[Daemon, TestClient, asyncio.AbstractEventLoop]
    ) -> None:
        _, client, loop = live_system

        r = client.post("/api/v1/tasks", json={"prompt": "metrics test"})
        _wait_for_completion(client, loop, r.json()["id"])

        metrics = client.get("/metrics").text
        assert "conductor_requests_total" in metrics
        assert 'status="success"' in metrics
