"""WebSocket /api/v1/events endpoint — auth and connection handshake.

End-to-end streaming (publish → receive) is covered by the EventBus unit tests
and will be re-validated in manual/soak testing. TestClient's WebSocket support
uses a separate anyio portal loop that doesn't interoperate cleanly with a
daemon started in an externally-managed loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def system(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
) -> Iterator[tuple[Daemon, TestClient]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        with TestClient(create_app(d)) as client:
            yield d, client
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def auth_system(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path
) -> Iterator[tuple[Daemon, TestClient]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(d.start(worker_count=1))
    try:
        with TestClient(create_app(d, auth_token="s3cret")) as client:
            yield d, client
    finally:
        loop.run_until_complete(d.stop())
        loop.close()
        asyncio.set_event_loop(None)


class TestEventsWebSocket:
    def test_connection_accepted_without_auth(
        self, system: tuple[Daemon, TestClient]
    ) -> None:
        _, client = system
        with client.websocket_connect("/api/v1/events") as ws:
            assert ws is not None

    def test_rejects_missing_token_when_auth_configured(
        self, auth_system: tuple[Daemon, TestClient]
    ) -> None:
        from starlette.websockets import WebSocketDisconnect

        _, client = auth_system
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/events") as ws,
        ):
            ws.receive_text()

    def test_accepts_correct_token_in_query(
        self, auth_system: tuple[Daemon, TestClient]
    ) -> None:
        _, client = auth_system
        with client.websocket_connect("/api/v1/events?token=s3cret") as ws:
            assert ws is not None

    def test_rejects_wrong_token(self, auth_system: tuple[Daemon, TestClient]) -> None:
        from starlette.websockets import WebSocketDisconnect

        _, client = auth_system
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/events?token=nope") as ws,
        ):
            ws.receive_text()
