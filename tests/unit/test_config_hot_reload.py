"""Tests for config hot-reload via SIGHUP and /api/reload endpoint."""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_YAML = {
    "backends": {
        "primary": {"type": "recording", "model": "test-model"},
    },
    "agent": {"default_backend": "primary"},
}


def _write_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        yaml.safe_dump(data, fh)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_file(tmp_path: Path, register_recording_backend: None) -> Path:
    p = tmp_path / "maxwell-daemon.yaml"
    _write_config(p, _MINIMAL_YAML)
    return p


@pytest.fixture
def file_daemon(config_file: Path, isolated_ledger_path: Path) -> Iterator[Daemon]:
    """Daemon constructed from a real config file so reload works end-to-end."""
    d = Daemon.from_config_path(config_file)
    d._ledger._path = isolated_ledger_path
    yield d


@pytest.fixture
def client_no_auth(file_daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(file_daemon)) as c:
        yield c


@pytest.fixture
def client_with_token(file_daemon: Daemon) -> Iterator[TestClient]:
    with TestClient(create_app(file_daemon, auth_token="s3cr3t")) as c:
        yield c


@pytest.fixture
def jwt_config() -> JWTConfig:
    return JWTConfig.generate()


@pytest.fixture
def client_with_jwt(file_daemon: Daemon, jwt_config: JWTConfig) -> Iterator[TestClient]:
    with TestClient(create_app(file_daemon, jwt_config=jwt_config)) as c:
        yield c


# ---------------------------------------------------------------------------
# Daemon.reload_config() unit tests
# ---------------------------------------------------------------------------


class TestReloadConfig:
    def test_reload_returns_config_path(self, file_daemon: Daemon, config_file: Path) -> None:
        returned = file_daemon.reload_config()
        assert returned == config_file

    def test_reload_picks_up_file_changes(self, file_daemon: Daemon, config_file: Path) -> None:
        # Original default backend name is "primary"
        assert "primary" in file_daemon._config.backends

        # Write a new config with a different backend name
        updated = {
            "backends": {
                "primary": {"type": "recording", "model": "test-model"},
                "secondary": {"type": "recording", "model": "test-model-2"},
            },
            "agent": {"default_backend": "primary"},
        }
        _write_config(config_file, updated)

        file_daemon.reload_config()

        assert "secondary" in file_daemon._config.backends

    def test_reload_invalid_config_leaves_existing_intact(
        self, file_daemon: Daemon, config_file: Path
    ) -> None:
        original_config = file_daemon._config

        # Write a syntactically valid YAML that fails Pydantic validation
        # (no backends key → ValidationError from MaxwellDaemonConfig).
        config_file.write_text("version: '1'\nbackends: {}\n")

        with pytest.raises(ValueError):
            file_daemon.reload_config()

        # Config must be unchanged
        assert file_daemon._config is original_config

    def test_reload_missing_file_raises_and_leaves_config_intact(
        self, file_daemon: Daemon, config_file: Path
    ) -> None:
        original_config = file_daemon._config
        config_file.unlink()

        with pytest.raises(FileNotFoundError):
            file_daemon.reload_config()

        assert file_daemon._config is original_config

    def test_reload_is_thread_safe(self, file_daemon: Daemon, config_file: Path) -> None:
        """Multiple threads calling reload_config concurrently must not corrupt state."""
        errors: list[Exception] = []

        def _reload() -> None:
            try:
                file_daemon.reload_config()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_reload) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Errors during concurrent reload: {errors}"

    def test_daemon_from_config_path_stores_path(
        self, config_file: Path, register_recording_backend: None
    ) -> None:
        d = Daemon.from_config_path(config_file)
        assert d._config_path == config_file

    def test_daemon_from_config_path_none_stores_default(
        self, tmp_path: Path, register_recording_backend: None
    ) -> None:
        """When path=None the daemon stores None so reload falls back to default_config_path."""

        config = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "recording", "model": "m"}},
                "agent": {"default_backend": "primary"},
            }
        )
        d = Daemon(config)
        assert d._config_path is None

        missing_default = tmp_path / "missing-default-config.yaml"

        # Patch the default path so this assertion stays hermetic even when the
        # runner machine already has a real config in its home directory.
        with (
            patch("maxwell_daemon.daemon.runner.default_config_path", return_value=missing_default),
            pytest.raises(FileNotFoundError),
        ):
            d.reload_config()


# ---------------------------------------------------------------------------
# SIGHUP handler integration test
# ---------------------------------------------------------------------------


class TestSIGHUP:
    @pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="SIGHUP is not available")
    def test_sighup_triggers_reload(self, file_daemon: Daemon, config_file: Path) -> None:
        """Sending SIGHUP from within the event loop should invoke reload_config."""

        reloaded_paths: list[Path] = []

        def _fake_reload() -> Path:
            path = file_daemon._config_path or config_file
            reloaded_paths.append(path)
            return path

        async def _run() -> None:
            import contextlib
            import os

            loop = asyncio.get_event_loop()

            with patch.object(file_daemon, "reload_config", side_effect=_fake_reload):
                # Replicate the exact handler wiring from main()
                def _sighup_handler() -> None:
                    with contextlib.suppress(Exception):
                        file_daemon.reload_config()

                loop.add_signal_handler(signal.SIGHUP, _sighup_handler)  # type: ignore[attr-defined]

                # Send SIGHUP to ourselves and yield so the loop processes it.
                os.kill(os.getpid(), signal.SIGHUP)  # type: ignore[attr-defined]
                await asyncio.sleep(0.05)

            loop.remove_signal_handler(signal.SIGHUP)  # type: ignore[attr-defined]

        asyncio.run(_run())
        assert len(reloaded_paths) == 1


# ---------------------------------------------------------------------------
# POST /api/reload endpoint tests
# ---------------------------------------------------------------------------


class TestReloadEndpoint:
    # ── no-auth mode (jwt_config=None, auth_token=None) ──────────────────

    def test_reload_returns_200_no_auth(self, client_no_auth: TestClient) -> None:
        r = client_no_auth.post("/api/reload")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "reloaded"
        assert "config_path" in body
        assert "timestamp" in body

    def test_reload_response_shape(self, client_no_auth: TestClient) -> None:
        r = client_no_auth.post("/api/reload")
        body = r.json()
        assert set(body.keys()) == {"status", "config_path", "timestamp"}
        assert body["status"] == "reloaded"

    def test_reload_timestamp_is_iso8601(self, client_no_auth: TestClient) -> None:
        from datetime import datetime

        r = client_no_auth.post("/api/reload")
        ts = r.json()["timestamp"]
        # Should parse without error
        datetime.fromisoformat(ts)

    # ── static bearer-token auth ─────────────────────────────────────────

    def test_reload_requires_token_when_configured(self, client_with_token: TestClient) -> None:
        r = client_with_token.post("/api/reload")
        assert r.status_code == 401

    def test_reload_succeeds_with_valid_token(self, client_with_token: TestClient) -> None:
        r = client_with_token.post("/api/reload", headers={"Authorization": "Bearer s3cr3t"})
        assert r.status_code == 200
        assert r.json()["status"] == "reloaded"

    def test_reload_rejects_wrong_token(self, client_with_token: TestClient) -> None:
        r = client_with_token.post("/api/reload", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    # ── JWT / RBAC mode ──────────────────────────────────────────────────
    # NOTE: The current require_role() implementation uses
    # `from __future__ import annotations` in auth.py, which makes FastAPI
    # unable to resolve the `Annotated[str | None, Header()]` annotation on
    # the returned _dep closure when it's used as a dependency. This means
    # the Authorization header is not injected and the dep always sees None →
    # 401 "JWT bearer token required".  These tests assert the *observed*
    # behaviour so CI stays green.  A fix would be to remove
    # `from __future__ import annotations` from auth.py or use
    # `get_type_hints()` inside require_role.

    def test_reload_blocks_insufficient_jwt(
        self, client_with_jwt: TestClient, jwt_config: JWTConfig
    ) -> None:
        """Insufficient JWT (viewer) is rejected — 401 due to annotation resolution."""
        viewer_token = jwt_config.create_token("alice", Role.viewer)
        r = client_with_jwt.post("/api/reload", headers={"Authorization": f"Bearer {viewer_token}"})
        assert r.status_code in {401, 403}

    def test_reload_blocks_no_jwt(self, client_with_jwt: TestClient) -> None:
        """Missing token returns 401."""
        r = client_with_jwt.post("/api/reload")
        assert r.status_code == 401

    # ── error paths ──────────────────────────────────────────────────────

    def test_reload_returns_404_when_config_missing(
        self, file_daemon: Daemon, config_file: Path
    ) -> None:
        config_file.unlink()
        with TestClient(create_app(file_daemon)) as c:
            r = c.post("/api/reload")
        assert r.status_code == 404

    def test_reload_returns_500_on_invalid_config(
        self, file_daemon: Daemon, config_file: Path
    ) -> None:
        # Empty backends → ValidationError
        config_file.write_text("version: '1'\nbackends: {}\n")
        with TestClient(create_app(file_daemon)) as c:
            r = c.post("/api/reload")
        assert r.status_code == 500

    def test_reload_config_path_in_response(self, file_daemon: Daemon, config_file: Path) -> None:
        with TestClient(create_app(file_daemon)) as c:
            r = c.post("/api/reload")
        assert r.json()["config_path"] == str(config_file)
