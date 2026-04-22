"""RBAC enforcement tests — require_role wired onto API endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

pytest.importorskip("jwt")

from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.auth import JWTConfig, Role
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
def jwt_cfg() -> JWTConfig:
    return JWTConfig.generate(expiry_seconds=3600)


@pytest.fixture
def jwt_only_client(daemon: Daemon, jwt_cfg: JWTConfig) -> Iterator[TestClient]:
    """App with JWT only — no static token."""
    with TestClient(create_app(daemon, jwt_config=jwt_cfg)) as c:
        yield c


@pytest.fixture
def static_only_client(daemon: Daemon) -> Iterator[TestClient]:
    """App with static token only — no JWT config."""
    with TestClient(create_app(daemon, auth_token="admin-static-secret")) as c:
        yield c


@pytest.fixture
def both_client(daemon: Daemon, jwt_cfg: JWTConfig) -> Iterator[TestClient]:
    """App with both static token and JWT configured."""
    with TestClient(create_app(daemon, auth_token="admin-static-secret", jwt_config=jwt_cfg)) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── helpers to mint tokens ────────────────────────────────────────────────────


def viewer_token(cfg: JWTConfig) -> str:
    return cfg.create_token("alice", Role.viewer)


def operator_token(cfg: JWTConfig) -> str:
    return cfg.create_token("bob", Role.operator)


def admin_token(cfg: JWTConfig) -> str:
    return cfg.create_token("carol", Role.admin)


# ── open mode (no auth configured) ───────────────────────────────────────────


class TestOpenMode:
    """When no static token and no JWT are configured, all requests pass."""

    def test_get_tasks_no_auth_required(self, daemon: Daemon) -> None:
        with TestClient(create_app(daemon)) as c:
            r = c.get("/api/v1/tasks")
            assert r.status_code == 200

    def test_post_tasks_no_auth_required(self, daemon: Daemon) -> None:
        with TestClient(create_app(daemon)) as c:
            r = c.post("/api/v1/tasks", json={"prompt": "hello"})
            assert r.status_code == 202


# ── static token backward compat ─────────────────────────────────────────────


class TestStaticTokenBackwardCompat:
    """Static admin token must still grant access to all endpoints."""

    def test_static_token_get_tasks(self, static_only_client: TestClient) -> None:
        r = static_only_client.get("/api/v1/tasks", headers=_bearer("admin-static-secret"))
        assert r.status_code == 200

    def test_static_token_post_tasks(self, static_only_client: TestClient) -> None:
        r = static_only_client.post(
            "/api/v1/tasks",
            json={"prompt": "hi"},
            headers=_bearer("admin-static-secret"),
        )
        assert r.status_code == 202

    def test_static_token_get_backends(self, static_only_client: TestClient) -> None:
        r = static_only_client.get("/api/v1/backends", headers=_bearer("admin-static-secret"))
        assert r.status_code == 200

    def test_static_token_get_cost(self, static_only_client: TestClient) -> None:
        r = static_only_client.get("/api/v1/cost", headers=_bearer("admin-static-secret"))
        assert r.status_code == 200

    def test_wrong_static_token_rejected(self, static_only_client: TestClient) -> None:
        r = static_only_client.get("/api/v1/tasks", headers=_bearer("wrong-token"))
        assert r.status_code == 401

    def test_missing_token_rejected(self, static_only_client: TestClient) -> None:
        r = static_only_client.get("/api/v1/tasks")
        assert r.status_code == 401

    def test_malformed_bearer_rejected(self, static_only_client: TestClient) -> None:
        r = static_only_client.get(
            "/api/v1/tasks",
            headers={"Authorization": "Token admin-static-secret"},
        )
        assert r.status_code == 401


# ── viewer-role JWT ───────────────────────────────────────────────────────────


class TestViewerJWT:
    """viewer-role JWT can read, but not write."""

    def test_viewer_can_get_tasks(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_viewer_can_get_backends(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/backends", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_viewer_can_get_cost(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/cost", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_viewer_can_get_fleet(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/fleet", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_viewer_cannot_post_tasks(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.post(
            "/api/v1/tasks",
            json={"prompt": "hi"},
            headers=_bearer(viewer_token(jwt_cfg)),
        )
        assert r.status_code == 403

    def test_viewer_cannot_cancel_task(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.post(
            "/api/v1/tasks/nonexistent/cancel",
            headers=_bearer(viewer_token(jwt_cfg)),
        )
        assert r.status_code == 403


# ── operator-role JWT ─────────────────────────────────────────────────────────


class TestOperatorJWT:
    """operator-role JWT can read and write tasks, but not fleet dispatch."""

    def test_operator_can_get_tasks(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer(operator_token(jwt_cfg)))
        assert r.status_code == 200

    def test_operator_can_post_tasks(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.post(
            "/api/v1/tasks",
            json={"prompt": "run this"},
            headers=_bearer(operator_token(jwt_cfg)),
        )
        assert r.status_code == 202

    def test_operator_cannot_dispatch_issue(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.post(
            "/api/v1/issues/dispatch",
            json={"repo": "org/repo", "number": 1},
            headers=_bearer(operator_token(jwt_cfg)),
        )
        assert r.status_code == 403


# ── admin-role JWT ────────────────────────────────────────────────────────────


class TestAdminJWT:
    """admin-role JWT has full access."""

    def test_admin_can_get_tasks(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer(admin_token(jwt_cfg)))
        assert r.status_code == 200

    def test_admin_can_post_tasks(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.post(
            "/api/v1/tasks",
            json={"prompt": "admin work"},
            headers=_bearer(admin_token(jwt_cfg)),
        )
        assert r.status_code == 202

    def test_admin_can_get_fleet(self, jwt_only_client: TestClient, jwt_cfg: JWTConfig) -> None:
        r = jwt_only_client.get("/api/v1/fleet", headers=_bearer(admin_token(jwt_cfg)))
        assert r.status_code == 200


# ── mixed static + JWT ────────────────────────────────────────────────────────


class TestMixedAuth:
    """When both static token and JWT are configured, either works."""

    def test_static_token_still_works_alongside_jwt(self, both_client: TestClient) -> None:
        r = both_client.get("/api/v1/tasks", headers=_bearer("admin-static-secret"))
        assert r.status_code == 200

    def test_viewer_jwt_works_alongside_static_token(
        self, both_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = both_client.get("/api/v1/tasks", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_viewer_jwt_still_blocked_from_operator_endpoints(
        self, both_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = both_client.post(
            "/api/v1/tasks",
            json={"prompt": "x"},
            headers=_bearer(viewer_token(jwt_cfg)),
        )
        assert r.status_code == 403

    def test_invalid_jwt_with_static_config_returns_401(self, both_client: TestClient) -> None:
        r = both_client.get("/api/v1/tasks", headers=_bearer("not.a.valid.jwt"))
        assert r.status_code == 401

    def test_operator_jwt_works_when_static_also_configured(
        self, both_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = both_client.post(
            "/api/v1/tasks",
            json={"prompt": "operator task"},
            headers=_bearer(operator_token(jwt_cfg)),
        )
        assert r.status_code == 202


# ── invalid/missing tokens ────────────────────────────────────────────────────


class TestInvalidTokens:
    """Invalid or missing tokens are rejected with 401."""

    def test_missing_auth_header_rejected(self, jwt_only_client: TestClient) -> None:
        r = jwt_only_client.get("/api/v1/tasks")
        assert r.status_code == 401

    def test_non_bearer_scheme_rejected(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get(
            "/api/v1/tasks",
            headers={"Authorization": f"Token {viewer_token(jwt_cfg)}"},
        )
        assert r.status_code == 401

    def test_invalid_jwt_rejected(self, jwt_only_client: TestClient) -> None:
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer("garbage.jwt.token"))
        assert r.status_code == 401

    def test_jwt_from_different_secret_rejected(self, jwt_only_client: TestClient) -> None:
        other_cfg = JWTConfig.generate()
        token = other_cfg.create_token("eve", Role.admin)
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer(token))
        assert r.status_code == 401


# ── _make_rbac_dep edge cases ─────────────────────────────────────────────────


class TestMakeRbacDep:
    """Direct unit tests for _make_rbac_dep logic."""

    def test_open_mode_passes_all(self, daemon: Daemon) -> None:
        """No static token, no JWT — all requests admitted."""
        with TestClient(create_app(daemon)) as c:
            assert c.get("/api/v1/cost").status_code == 200

    def test_jwt_only_no_static_invalid_jwt_is_401(self, jwt_only_client: TestClient) -> None:
        r = jwt_only_client.get("/api/v1/tasks", headers=_bearer("invalid.jwt.here"))
        assert r.status_code == 401

    def test_static_only_valid_token_admitted_to_admin_endpoint(
        self, static_only_client: TestClient
    ) -> None:
        # SSH sessions is admin-only; static token should admit.
        r = static_only_client.get("/api/v1/ssh/sessions", headers=_bearer("admin-static-secret"))
        # 503 (SSH not installed) means auth passed; 403 would mean RBAC block.
        assert r.status_code in (200, 503)

    def test_viewer_blocked_from_admin_endpoint(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get("/api/v1/ssh/sessions", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 403

    def test_operator_blocked_from_admin_endpoint(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get("/api/v1/ssh/sessions", headers=_bearer(operator_token(jwt_cfg)))
        assert r.status_code == 403

    def test_admin_jwt_admitted_to_admin_endpoint(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get("/api/v1/ssh/sessions", headers=_bearer(admin_token(jwt_cfg)))
        assert r.status_code in (200, 503)

    def test_audit_endpoint_viewer_accessible(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get("/api/v1/audit", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200

    def test_audit_verify_viewer_accessible(
        self, jwt_only_client: TestClient, jwt_cfg: JWTConfig
    ) -> None:
        r = jwt_only_client.get("/api/v1/audit/verify", headers=_bearer(viewer_token(jwt_cfg)))
        assert r.status_code == 200
