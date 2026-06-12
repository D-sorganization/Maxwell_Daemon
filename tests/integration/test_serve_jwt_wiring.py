"""End-to-end JWT/RBAC enforcement through the ``serve`` entrypoint (issue #964).

These tests prove that ``maxwell-daemon serve`` actually *wires* the JWT
config onto the FastAPI app.  The regression they guard against is the
``serve`` command building the app with ``create_app(daemon,
auth_token=...)`` and silently dropping ``jwt_config`` — which left the
entire RBAC subsystem (roles, revocation, audit) as dead code on the real
serving path, so the daemon ran fully open whenever only ``api.jwt_secret``
(and no static ``auth_token``) was configured.

The tests exercise the exact wiring ``serve`` uses: ``_build_jwt_config`` to
derive the ``JWTConfig`` from config, then ``create_app(daemon,
auth_token=..., jwt_config=...)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("jwt")

from maxwell_daemon.api import create_app
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.cli.main import _build_jwt_config
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

# A protected admin route registered by ``server.py`` — used as the canonical
# probe for RBAC enforcement.
_ADMIN_ROUTE = "/api/v1/admin/prune"

_JWT_SECRET = "a" * 64  # 32 bytes hex — non-empty, valid HS256 secret  # nosec B105


def _config_with_jwt(*, auth_token: str | None = None) -> MaxwellDaemonConfig:
    api: dict[str, object] = {"jwt_secret": _JWT_SECRET}
    if auth_token is not None:
        api["auth_token"] = auth_token
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "test-model"}},
            "agent": {"default_backend": "primary"},
            "api": api,
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


@pytest.fixture
def jwt_serve_client(
    register_recording_backend: None,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, JWTConfig]]:
    """Build the app exactly as ``serve`` does, with ``api.jwt_secret`` set.

    Yields the test client together with the ``JWTConfig`` so tests can mint
    tokens that the running app will accept.
    """
    cfg = _config_with_jwt()
    daemon, loop = _make_daemon(cfg, tmp_path)

    # This is the wiring under test: serve() derives jwt_config from config and
    # passes it into create_app alongside the (here unset) static token.
    jwt_config = _build_jwt_config(cfg)
    assert jwt_config is not None, "JWT must be reachable when api.jwt_secret is set"

    app = create_app(daemon, auth_token=cfg.api.auth_token, jwt_config=jwt_config)
    try:
        with TestClient(app) as client:
            yield client, jwt_config
    finally:
        loop.run_until_complete(daemon.stop())
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(None)


class TestBuildJwtConfig:
    """Unit-level coverage of the ``serve`` JWT wiring helper."""

    def test_returns_none_when_secret_unset(self, register_recording_backend: None) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "recording", "model": "test-model"}},
                "agent": {"default_backend": "primary"},
            }
        )
        assert _build_jwt_config(cfg) is None

    def test_builds_config_from_secret_and_expiry(self, register_recording_backend: None) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "recording", "model": "test-model"}},
                "agent": {"default_backend": "primary"},
                "api": {"jwt_secret": _JWT_SECRET, "jwt_expiry_seconds": 1234},
            }
        )
        jwt_config = _build_jwt_config(cfg)
        assert isinstance(jwt_config, JWTConfig)
        assert jwt_config.secret == _JWT_SECRET
        assert jwt_config.expiry_seconds == 1234


class TestServeJwtEnforcement:
    """RBAC is enforced end-to-end on the app ``serve`` actually builds."""

    def test_unauthenticated_admin_route_returns_401(
        self, jwt_serve_client: tuple[TestClient, JWTConfig]
    ) -> None:
        client, _ = jwt_serve_client
        resp = client.get(_ADMIN_ROUTE)
        assert resp.status_code == 401

    def test_non_admin_jwt_is_forbidden_403(
        self, jwt_serve_client: tuple[TestClient, JWTConfig]
    ) -> None:
        client, jwt_config = jwt_serve_client
        token = jwt_config.create_token("viewer-user", Role.viewer)
        resp = client.get(_ADMIN_ROUTE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

    def test_admin_jwt_is_admitted(self, jwt_serve_client: tuple[TestClient, JWTConfig]) -> None:
        client, jwt_config = jwt_serve_client
        token = jwt_config.create_token("admin-user", Role.admin)
        resp = client.get(_ADMIN_ROUTE, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200

    def test_garbage_token_returns_401(
        self, jwt_serve_client: tuple[TestClient, JWTConfig]
    ) -> None:
        client, _ = jwt_serve_client
        resp = client.get(_ADMIN_ROUTE, headers={"Authorization": "Bearer not-a-real-jwt"})
        assert resp.status_code == 401
