"""Unit tests for GitHubAuth."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

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

    @pytest.mark.asyncio
    async def test_async_token_returns_token_for_pat(self) -> None:
        auth = GitHubAuth.from_token("ghp_async_test")
        result = await auth.async_token()
        assert result == "ghp_async_test"

    @pytest.mark.asyncio
    async def test_async_headers_for_pat(self) -> None:
        auth = GitHubAuth.from_token("ghp_hdr_test")
        hdrs = await auth.async_headers()
        assert hdrs["Authorization"] == "Bearer ghp_hdr_test"
        assert "Accept" in hdrs
        assert "X-GitHub-Api-Version" in hdrs


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

    def test_sync_token_raises_for_app_mode(self) -> None:
        """Synchronous .token must not block the event loop in app mode."""
        auth = self._make_auth()
        with pytest.raises(RuntimeError, match=r"app.*mode"):
            _ = auth.token

    @pytest.mark.asyncio
    async def test_async_token_uses_cache_when_fresh(self) -> None:
        auth = self._make_auth()
        auth._cache = _AppTokenCache(
            token="cached_token",
            expires_at=time.monotonic() + 3600,
        )
        result = await auth.async_token()
        assert result == "cached_token"

    @pytest.mark.asyncio
    async def test_async_token_refreshes_when_cache_expired(self) -> None:
        auth = self._make_auth()
        auth._cache = _AppTokenCache(
            token="old_token",
            expires_at=time.monotonic() - 1,  # already expired
        )

        fresh_token = "new_installation_token"
        fresh_expires = time.monotonic() + 3600

        with patch.object(
            auth,
            "_fetch_installation_token",
            new=AsyncMock(return_value=(fresh_token, fresh_expires)),
        ):
            result = await auth.async_token()

        assert result == fresh_token
        assert auth._cache is not None
        assert auth._cache.token == fresh_token

    @pytest.mark.asyncio
    async def test_async_token_refreshes_when_near_expiry(self) -> None:
        auth = self._make_auth()
        # Only 2 minutes left — below the 5-minute threshold
        auth._cache = _AppTokenCache(
            token="near_expired",
            expires_at=time.monotonic() + 120,
        )

        with patch.object(
            auth,
            "_fetch_installation_token",
            new=AsyncMock(return_value=("refreshed", time.monotonic() + 3600)),
        ):
            result = await auth.async_token()

        assert result == "refreshed"

    @pytest.mark.asyncio
    async def test_no_jwt_import_raises_helpful_error(self) -> None:
        auth = self._make_auth()

        with patch.dict("sys.modules", {"jwt": None}), pytest.raises(ImportError, match="PyJWT"):
            await auth._fetch_installation_token()
