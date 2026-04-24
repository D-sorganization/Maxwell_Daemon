"""Regression tests for optional PyJWT auth failure handling."""

from __future__ import annotations

import asyncio
import builtins
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.auth import JWTConfig, Role, is_jwt_auth_failure, require_role
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def daemon(
    minimal_config: MaxwellDaemonConfig, isolated_ledger_path: Path, tmp_path: Path
) -> Iterator[Daemon]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    daemon = Daemon(
        minimal_config,
        ledger_path=isolated_ledger_path,
        workspace_root=tmp_path / "workspaces",
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
    )
    loop.run_until_complete(daemon.start(worker_count=1))
    try:
        yield daemon
    finally:
        loop.run_until_complete(daemon.stop())
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def jwt_cfg() -> JWTConfig:
    return JWTConfig.generate(expiry_seconds=3600)


def _make_request(path: str = "/api/v1/tasks") -> MagicMock:
    request = MagicMock()
    request.url.path = path
    return request


@pytest.fixture
def block_pyjwt(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def _guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "jwt":
            error = ModuleNotFoundError("No module named 'jwt'")
            error.name = "jwt"
            raise error
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)


def test_require_role_returns_401_when_pyjwt_is_unavailable(
    jwt_cfg: JWTConfig, block_pyjwt: None
) -> None:
    dep = require_role(Role.viewer, jwt_cfg)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(dep(request=_make_request(), authorization="Bearer fake.jwt.token"))

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Authentication failed"


def test_api_rbac_returns_401_when_pyjwt_is_unavailable(
    daemon: Daemon, jwt_cfg: JWTConfig, block_pyjwt: None
) -> None:
    with TestClient(create_app(daemon, jwt_config=jwt_cfg)) as client:
        response = client.get(
            "/api/v1/tasks",
            headers={"Authorization": "Bearer fake.jwt.token"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication failed"


def test_is_jwt_auth_failure_detects_missing_pyjwt_module() -> None:
    error = ModuleNotFoundError("No module named 'jwt'")
    error.name = "jwt"

    assert is_jwt_auth_failure(error) is True


def test_is_jwt_auth_failure_ignores_unrelated_errors() -> None:
    assert is_jwt_auth_failure(ValueError("boom")) is False
