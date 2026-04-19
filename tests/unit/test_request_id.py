"""Request-ID middleware — every response carries an X-Request-ID header and
all structured logs inside the handler carry the same id."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conductor.api import create_app
from conductor.config import ConductorConfig
from conductor.daemon import Daemon


@pytest.fixture
def client(minimal_config: ConductorConfig, isolated_ledger_path: Path) -> Iterator[TestClient]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        with TestClient(create_app(d)) as c:
            yield c
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


class TestRequestId:
    def test_response_has_request_id_header(self, client: TestClient) -> None:
        r = client.get("/health")
        assert "x-request-id" in r.headers
        # uuid-ish shape (8-4-4-4-12 hex)
        assert re.match(
            r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
            r.headers["x-request-id"],
        )

    def test_client_supplied_id_is_preserved(self, client: TestClient) -> None:
        r = client.get("/health", headers={"x-request-id": "00000000-0000-0000-0000-000000000001"})
        assert r.headers["x-request-id"] == "00000000-0000-0000-0000-000000000001"

    def test_malformed_client_id_is_replaced(self, client: TestClient) -> None:
        r = client.get("/health", headers={"x-request-id": "not-a-uuid"})
        assert r.headers["x-request-id"] != "not-a-uuid"

    def test_every_request_gets_unique_id(self, client: TestClient) -> None:
        a = client.get("/health").headers["x-request-id"]
        b = client.get("/health").headers["x-request-id"]
        assert a != b
