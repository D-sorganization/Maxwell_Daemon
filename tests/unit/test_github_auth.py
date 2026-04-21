"""Unit tests for GitHubAuth."""

from __future__ import annotations

import datetime as dt
import sys
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from maxwell_daemon.github_auth import GitHubAuth, _AppTokenCache


class TestTokenAuth:
    def test_from_token_returns_token(self) -> None:
        auth = GitHubAuth.from_token("ghp_test123")
        assert auth.token == "ghp_test123"

    def test_headers_contain_bearer(self) -> None:
        auth = GitHubAuth.from_token("ghp_abc")
        assert auth.headers["Authorization"] == "Bearer ghp_abc"
        assert "Accept" in auth.headers
        assert "X-GitHub-Api-Version" in auth.headers

    def test_from_config_token_mode(self) -> None:
        cfg = MagicMock()
        cfg.github.auth_method = "token"
        cfg.github.token = "ghp_from_config"
        auth = GitHubAuth.from_config(cfg)
        assert auth.token == "ghp_from_config"

    def test_from_config_no_github_section_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GH_TOKEN", "ghp_from_env")
        cfg = MagicMock()
        cfg.github = None
        auth = GitHubAuth.from_config(cfg)
        assert auth.token == "ghp_from_env"

    def test_from_config_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg = MagicMock()
        cfg.github = None
        with pytest.raises(ValueError, match="GitHub token not configured"):
            GitHubAuth.from_config(cfg)

    def test_from_config_reads_github_token_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_github_env")
        cfg = MagicMock()
        cfg.github = None
        auth = GitHubAuth.from_config(cfg)
        assert auth.token == "ghp_from_github_env"


class TestAppAuth:
    def _make_auth(self) -> GitHubAuth:
        return GitHubAuth.from_app(
            app_id=12345,
            installation_id=99999,
            private_key_pem="fake-pem",
        )

    def test_from_app_sets_mode(self) -> None:
        auth = self._make_auth()
        assert auth._mode == "app"
        assert auth._app_id == 12345
        assert auth._installation_id == 99999

    def test_token_uses_cache_when_fresh(self) -> None:
        auth = self._make_auth()
        auth._cache = _AppTokenCache(
            token="cached_token",
            expires_at=time.monotonic() + 3600,
        )
        assert auth.token == "cached_token"

    def test_token_refreshes_when_cache_expired(self) -> None:
        auth = self._make_auth()
        auth._cache = _AppTokenCache(
            token="old_token",
            expires_at=time.monotonic() - 1,  # already expired
        )

        fresh_token = "new_installation_token"
        fresh_expires = time.monotonic() + 3600

        with patch.object(
            auth, "_fetch_installation_token", return_value=(fresh_token, fresh_expires)
        ):
            result = auth.token

        assert result == fresh_token
        assert auth._cache is not None
        assert auth._cache.token == fresh_token

    def test_token_refreshes_when_near_expiry(self) -> None:
        auth = self._make_auth()
        # Only 2 minutes left — below the 5-minute threshold
        auth._cache = _AppTokenCache(
            token="near_expired",
            expires_at=time.monotonic() + 120,
        )

        with patch.object(
            auth, "_fetch_installation_token", return_value=("refreshed", time.monotonic() + 3600)
        ):
            result = auth.token

        assert result == "refreshed"

    def test_no_jwt_import_raises_helpful_error(self) -> None:
        auth = self._make_auth()

        with patch.dict("sys.modules", {"jwt": None}), pytest.raises(ImportError, match="PyJWT"):
            auth._fetch_installation_token()

    def test_no_httpx_import_raises_helpful_error(self) -> None:
        auth = self._make_auth()

        with (
            patch.dict("sys.modules", {"jwt": MagicMock(), "httpx": None}),
            pytest.raises(ImportError, match="httpx is required"),
        ):
            auth._fetch_installation_token()

    def test_from_config_app_reads_private_key(self, tmp_path) -> None:
        key_path = tmp_path / "app.pem"
        key_path.write_text("private-key", encoding="utf-8")
        cfg = SimpleNamespace(
            github=SimpleNamespace(
                auth_method="app",
                app_id="123",
                installation_id="456",
                private_key_path=str(key_path),
            )
        )

        auth = GitHubAuth.from_config(cfg)

        assert auth._mode == "app"
        assert auth._app_id == 123
        assert auth._installation_id == 456
        assert auth._private_key_pem == "private-key"

    def test_from_config_app_missing_private_key_raises(self, tmp_path) -> None:
        cfg = SimpleNamespace(
            github=SimpleNamespace(
                auth_method="app",
                app_id="123",
                installation_id="456",
                private_key_path=str(tmp_path / "missing.pem"),
            )
        )

        with pytest.raises(FileNotFoundError, match="GitHub App private key not found"):
            GitHubAuth.from_config(cfg)

    def test_fetch_installation_token_posts_jwt_and_converts_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        auth = self._make_auth()
        expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)).isoformat()
        captured: dict[str, Any] = {}

        class FakeJwt:
            @staticmethod
            def encode(payload: dict[str, Any], key: str | None, algorithm: str) -> str:
                captured["payload"] = payload
                captured["key"] = key
                captured["algorithm"] = algorithm
                return "encoded-jwt"

        class FakeResponse:
            def raise_for_status(self) -> None:
                captured["raised"] = False

            def json(self) -> dict[str, str]:
                return {"token": "installation-token", "expires_at": expires_at}

        class FakeHttpx:
            @staticmethod
            def post(url: str, **kwargs: Any) -> FakeResponse:
                captured["url"] = url
                captured["kwargs"] = kwargs
                return FakeResponse()

        monkeypatch.setitem(sys.modules, "jwt", FakeJwt)
        monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

        token, expires_mono = auth._fetch_installation_token()

        assert token == "installation-token"
        assert expires_mono > time.monotonic()
        assert captured["payload"]["iss"] == "12345"
        assert captured["key"] == "fake-pem"
        assert captured["algorithm"] == "RS256"
        assert captured["url"].endswith("/app/installations/99999/access_tokens")
        assert captured["kwargs"]["headers"]["Authorization"] == "Bearer encoded-jwt"
