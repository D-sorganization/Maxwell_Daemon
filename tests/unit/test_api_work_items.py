"""Work item REST API."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def daemon(minimal_config: MaxwellDaemonConfig, isolated_ledger_path, tmp_path) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        work_item_store_path=tmp_path / "work_items.db",
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
    with TestClient(create_app(daemon)) as c:
        yield c


def test_create_list_get_and_transition_work_item(client: TestClient) -> None:
    created = client.post(
        "/api/v1/work-items",
        json={
            "id": "wi-api",
            "title": "Add contract",
            "repo": "D-sorganization/Maxwell-Daemon",
            "acceptance_criteria": [{"id": "AC1", "text": "covered"}],
            "priority": 10,
        },
    )
    assert created.status_code == 201
    assert created.json()["status"] == "draft"

    listed = client.get("/api/v1/work-items", params={"repo": "D-sorganization/Maxwell-Daemon"})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == ["wi-api"]

    needs_refinement = client.post(
        "/api/v1/work-items/wi-api/transition",
        json={"status": "needs_refinement"},
    )
    assert needs_refinement.status_code == 200

    refined = client.post(
        "/api/v1/work-items/wi-api/transition",
        json={"status": "refined"},
    )
    assert refined.status_code == 200
    assert refined.json()["status"] == "refined"

    fetched = client.get("/api/v1/work-items/wi-api")
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Add contract"


def test_invalid_transition_returns_conflict(client: TestClient) -> None:
    client.post("/api/v1/work-items", json={"id": "wi-bad", "title": "Bad transition"})

    response = client.post("/api/v1/work-items/wi-bad/transition", json={"status": "done"})

    assert response.status_code == 409


def test_patch_cannot_break_refined_contract(client: TestClient) -> None:
    client.post(
        "/api/v1/work-items",
        json={
            "id": "wi-contract",
            "title": "Contract",
            "acceptance_criteria": [{"id": "AC1", "text": "covered"}],
        },
    )
    client.post("/api/v1/work-items/wi-contract/transition", json={"status": "needs_refinement"})
    client.post("/api/v1/work-items/wi-contract/transition", json={"status": "refined"})

    response = client.patch(
        "/api/v1/work-items/wi-contract",
        json={"acceptance_criteria": []},
    )

    assert response.status_code == 422
