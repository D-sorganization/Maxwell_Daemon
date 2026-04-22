from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from maxwell_daemon.api import create_app
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def jwt_config() -> JWTConfig:
    return JWTConfig("test-websocket-secret", expiry_seconds=3600)


@pytest.fixture
def jwt_system(
    minimal_config: MaxwellDaemonConfig,
    isolated_ledger_path: Path,
    jwt_config: JWTConfig,
) -> Iterator[tuple[Daemon, TestClient]]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = Daemon(minimal_config, ledger_path=isolated_ledger_path)
    loop.run_until_complete(daemon.start(worker_count=1))
    try:
        with TestClient(create_app(daemon, jwt_config=jwt_config)) as client:
            yield daemon, client
    finally:
        loop.run_until_complete(daemon.stop())
        loop.close()
        asyncio.set_event_loop(None)


def _query_token(jwt_config: JWTConfig, role: Role) -> str:
    return jwt_config.create_token(f"{role.value}-subject", role)


class TestEventsWebSocketJwtAuth:
    def test_jwt_only_events_reject_missing_token(
        self, jwt_system: tuple[Daemon, TestClient]
    ) -> None:
        _daemon, client = jwt_system

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/events") as ws,
        ):
            ws.receive_text()

    def test_jwt_only_events_accept_viewer_token(
        self, jwt_system: tuple[Daemon, TestClient], jwt_config: JWTConfig
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, Role.viewer)

        with client.websocket_connect(f"/api/v1/events?token={token}") as ws:
            assert ws is not None

    @pytest.mark.parametrize("role", [Role.operator, Role.admin])
    def test_jwt_only_events_accept_higher_privilege_tokens(
        self,
        jwt_system: tuple[Daemon, TestClient],
        jwt_config: JWTConfig,
        role: Role,
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, role)

        with client.websocket_connect(f"/api/v1/events?token={token}") as ws:
            assert ws is not None

    def test_jwt_only_events_reject_developer_token(
        self, jwt_system: tuple[Daemon, TestClient], jwt_config: JWTConfig
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, Role.developer)

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(f"/api/v1/events?token={token}") as ws,
        ):
            ws.receive_text()

    def test_jwt_only_events_reject_invalid_token(
        self, jwt_system: tuple[Daemon, TestClient]
    ) -> None:
        _daemon, client = jwt_system

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/events?token=invalid") as ws,
        ):
            ws.receive_text()


class TestSshShellWebSocketJwtAuth:
    def test_jwt_only_ssh_shell_rejects_missing_token_before_validation(
        self, jwt_system: tuple[Daemon, TestClient]
    ) -> None:
        _daemon, client = jwt_system

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/v1/ssh/shell?host=example.test&user=maxwell&port=bad"
            ) as ws,
        ):
            ws.receive_text()

    def test_jwt_only_ssh_shell_rejects_viewer_token(
        self, jwt_system: tuple[Daemon, TestClient], jwt_config: JWTConfig
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, Role.viewer)

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/v1/ssh/shell?host=example.test&user=maxwell&port=bad&token={token}"
            ) as ws,
        ):
            ws.receive_text()

    def test_jwt_only_ssh_shell_rejects_operator_token(
        self, jwt_system: tuple[Daemon, TestClient], jwt_config: JWTConfig
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, Role.operator)

        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                f"/api/v1/ssh/shell?host=example.test&user=maxwell&port=bad&token={token}"
            ) as ws,
        ):
            ws.receive_text()

    def test_jwt_only_ssh_shell_accepts_admin_token_then_validates_request(
        self, jwt_system: tuple[Daemon, TestClient], jwt_config: JWTConfig
    ) -> None:
        _daemon, client = jwt_system
        token = _query_token(jwt_config, Role.admin)

        with client.websocket_connect(
            f"/api/v1/ssh/shell?host=example.test&user=maxwell&port=bad&token={token}"
        ) as ws:
            message = ws.receive_json()

        assert message["error"] in {"SSH not installed", "invalid port"}


class TestWebSocketStaticTokenCompatibility:
    def test_events_accept_static_query_token(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        daemon = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        loop.run_until_complete(daemon.start(worker_count=1))
        try:
            with (
                TestClient(create_app(daemon, auth_token="s3cret")) as client,
                client.websocket_connect("/api/v1/events?token=s3cret") as ws,
            ):
                assert ws is not None
        finally:
            loop.run_until_complete(daemon.stop())
            loop.close()
            asyncio.set_event_loop(None)

    def test_ssh_shell_accepts_static_query_token_then_validates_request(
        self,
        minimal_config: MaxwellDaemonConfig,
        isolated_ledger_path: Path,
    ) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        daemon = Daemon(minimal_config, ledger_path=isolated_ledger_path)
        loop.run_until_complete(daemon.start(worker_count=1))
        try:
            with (
                TestClient(create_app(daemon, auth_token="s3cret")) as client,
                client.websocket_connect(
                    "/api/v1/ssh/shell?host=example.test&user=maxwell&port=bad&token=s3cret"
                ) as ws,
            ):
                message = ws.receive_json()
        finally:
            loop.run_until_complete(daemon.stop())
            loop.close()
            asyncio.set_event_loop(None)

        assert message["error"] in {"SSH not installed", "invalid port"}
