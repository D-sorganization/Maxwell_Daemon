"""SSH admin endpoints fail *closed* when no auth is configured (issue #965).

SSH routes expose remote command execution against any host that has a stored
or agent SSH key. Before this fix they inherited the fully-open dev mode that
``make_rbac_dep`` falls back to when neither a static ``auth_token`` nor a
``jwt_config`` is configured — so an unauthenticated local process (or a
browser via DNS-rebinding on 127.0.0.1) could run arbitrary remote commands.
This contrasted with ``POST /api/dispatch``, which is safe-closed (refuses
when no token is set).

These tests prove every SSH route now rejects with ``503`` and never executes
when no auth is configured, and that the safe-closed guard steps out of the way
once auth *is* configured (the request then reaches the normal auth layer,
which returns ``401`` for a missing token rather than ``503``).
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

# Every SSH route, as (method, path, json-body-or-None). The guard must fire
# *before* any handler side effect, so a bogus body is fine.
_SSH_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/api/v1/ssh/sessions", None),
    ("GET", "/api/v1/ssh/keys", None),
    ("GET", "/api/v1/ssh/keys/somehost", None),
    ("DELETE", "/api/v1/ssh/keys/somehost", None),
    ("GET", "/api/v1/ssh/files?host=h&user=u", None),
    (
        "POST",
        "/api/v1/ssh/connect",
        {"host": "h", "port": 22, "user": "u"},
    ),
    (
        "POST",
        "/api/v1/ssh/run",
        {"host": "h", "port": 22, "user": "u", "command": "id"},
    ),
]

_STATIC_TOKEN = "s" * 32  # nosec B105


def _make_config(**api: object) -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "test-model"}},
            "agent": {"default_backend": "primary"},
            "api": dict(api),
        }
    )


def _make_daemon(
    cfg: MaxwellDaemonConfig, tmp_path: Path
) -> tuple[Daemon, asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = Daemon(
        cfg,
        ledger_path=tmp_path / "ledger.db",
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        task_graph_store_path=tmp_path / "task_graphs.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
        delegate_lifecycle_store_path=tmp_path / "delegate_sessions.db",
    )
    loop.run_until_complete(daemon.start(worker_count=1))
    return daemon, loop


def _client(cfg: MaxwellDaemonConfig, tmp_path: Path) -> Iterator[TestClient]:
    daemon, loop = _make_daemon(cfg, tmp_path)
    # These tests cover the open-mode default and the static-token path only;
    # JWT wiring is covered by tests/integration/test_serve_jwt_wiring.py.
    app = create_app(daemon, auth_token=cfg.api.auth_token, jwt_config=None)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        loop.run_until_complete(daemon.stop())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def open_mode_client(register_recording_backend: None, tmp_path: Path) -> Iterator[TestClient]:
    """App built with NO auth configured (the dangerous default)."""
    yield from _client(_make_config(), tmp_path)


@pytest.fixture
def static_token_client(register_recording_backend: None, tmp_path: Path) -> Iterator[TestClient]:
    """App built with a static admin token configured."""
    yield from _client(_make_config(auth_token=_STATIC_TOKEN), tmp_path)


def _call(client: TestClient, method: str, path: str, body: dict[str, object] | None):  # type: ignore[no-untyped-def]
    if method == "GET":
        return client.get(path)
    if method == "DELETE":
        return client.delete(path)
    return client.post(path, json=body)


class TestSshSafeClosedWhenUnconfigured:
    """With no auth configured, every SSH route returns 503 and never runs."""

    @pytest.mark.parametrize(("method", "path", "body"), _SSH_ROUTES)
    def test_route_returns_503(
        self,
        open_mode_client: TestClient,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        resp = _call(open_mode_client, method, path, body)
        assert resp.status_code == 503, (method, path, resp.status_code, resp.text)
        assert "auth" in resp.json()["detail"].lower()

    def test_websocket_shell_closed_when_unconfigured(self, open_mode_client: TestClient) -> None:
        from starlette.websockets import WebSocketDisconnect

        with (
            pytest.raises(WebSocketDisconnect) as excinfo,
            open_mode_client.websocket_connect("/api/v1/ssh/shell?host=h&user=u") as ws,
        ):
            ws.receive_text()
        assert excinfo.value.code == 1008


class TestSshConnectPasswordRedaction:
    """Issue #966 — the plaintext password never appears in model repr."""

    def test_password_excluded_from_repr(self) -> None:
        from maxwell_daemon.api.routes.ssh import SSHConnectRequest

        req = SSHConnectRequest(host="h", user="u", password="s3cr3t-pw")
        assert "s3cr3t-pw" not in repr(req)
        # The value is still usable by the handler / SSH layer.
        assert req.password == "s3cr3t-pw"


class TestSshReachableWhenConfigured:
    """Once auth is configured, the safe-closed guard steps aside.

    The request then reaches the normal auth layer: a missing/invalid token is
    a ``401`` (auth failure), distinctly *not* the ``503`` that means
    "auth not configured". This proves the guard only suppresses the dangerous
    open-mode default and does not block legitimately-secured deployments.
    """

    @pytest.mark.parametrize(("method", "path", "body"), _SSH_ROUTES)
    def test_route_not_503_when_token_configured(
        self,
        static_token_client: TestClient,
        method: str,
        path: str,
        body: dict[str, object] | None,
    ) -> None:
        resp = _call(static_token_client, method, path, body)
        assert resp.status_code != 503, (method, path, resp.text)
        assert resp.status_code == 401, (method, path, resp.status_code, resp.text)
