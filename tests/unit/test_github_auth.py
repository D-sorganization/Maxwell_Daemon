"""Unit tests for GitHubAuth."""

from __future__ import annotations

from pathlib import Path
import time
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
        fake_jwt = MagicMock()
        fake_jwt.encode.return_value = "jwt"
        with patch.dict("sys.modules", {"jwt": fake_jwt, "httpx": None}):
            with pytest.raises(ImportError, match="httpx"):
                auth._fetch_installation_token()

    def test_fetch_installation_token_success(self) -> None:
        auth = self._make_auth()

        fake_jwt = MagicMock()
        fake_jwt.encode.return_value = "jwt-token"

        response = MagicMock()
        response.json.return_value = {
            "token": "inst_token",
            "expires_at": "2099-01-01T00:00:00Z",
        }
        fake_httpx = MagicMock()
        fake_httpx.post.return_value = response

        with patch.dict("sys.modules", {"jwt": fake_jwt, "httpx": fake_httpx}):
            token, expires_at = auth._fetch_installation_token()

        assert token == "inst_token"
        assert expires_at > time.monotonic()
        response.raise_for_status.assert_called_once()
        fake_httpx.post.assert_called_once()


class TestAppConfig:
    def test_from_config_app_mode_reads_pem(self, tmp_path: Path) -> None:
        pem = tmp_path / "key.pem"
        pem.write_text("PEM", encoding="utf-8")

        cfg = MagicMock()
        cfg.github.auth_method = "app"
        cfg.github.private_key_path = str(pem)
        cfg.github.app_id = "101"
        cfg.github.installation_id = "202"

        auth = GitHubAuth.from_config(cfg)
        assert auth._mode == "app"
        assert auth._app_id == 101
        assert auth._installation_id == 202

    def test_from_config_app_mode_missing_pem(self, tmp_path: Path) -> None:
        cfg = MagicMock()
        cfg.github.auth_method = "app"
        cfg.github.private_key_path = str(tmp_path / "missing.pem")
        cfg.github.app_id = "101"
        cfg.github.installation_id = "202"

        with pytest.raises(FileNotFoundError):
            GitHubAuth.from_config(cfg)
