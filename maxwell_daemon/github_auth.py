"""GitHub authentication helpers.

Supports two auth modes:
- token: a plain PAT or fine-grained PAT (shares the user's rate limit pool)
- app:   a GitHub App installation token (separate 5,000 req/hr quota per installation)

GitHub App tokens expire after 1 hour and are refreshed automatically.

Usage::

    from maxwell_daemon.github_auth import GitHubAuth

    auth = GitHubAuth.from_config(config)
    token = await auth.async_token()   # always fresh; awaitable for app mode
    headers = await auth.async_headers()  # ready for httpx / requests

For plain PAT mode the synchronous ``auth.token`` property still works and
returns immediately.  App mode **must** use the async variants so the event
loop is not blocked by the network call to the GitHub App tokens endpoint.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class _AppTokenCache:
    token: str
    expires_at: float  # monotonic timestamp


@dataclass
class GitHubAuth:
    """Resolves a usable GitHub API token, refreshing App tokens as needed."""

    _mode: str  # "token" | "app"
    _static_token: str | None = field(default=None, repr=False)
    _app_id: int | None = None
    _installation_id: int | None = None
    _private_key_pem: str | None = field(default=None, repr=False)
    _cache: _AppTokenCache | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ #
    # Factory methods
    # ------------------------------------------------------------------ #

    @classmethod
    def from_token(cls, token: str) -> GitHubAuth:
        """Plain PAT / fine-grained PAT auth."""
        return cls(_mode="token", _static_token=token)

    @classmethod
    def from_app(
        cls,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
    ) -> GitHubAuth:
        """GitHub App installation token auth (recommended for fleet operations)."""
        return cls(
            _mode="app",
            _app_id=app_id,
            _installation_id=installation_id,
            _private_key_pem=private_key_pem,
        )

    @classmethod
    def from_config(cls, config: Any) -> GitHubAuth:
        """Build from the daemon's config object."""
        gh_cfg = getattr(config, "github", None)
        if gh_cfg is None or getattr(gh_cfg, "auth_method", "token") == "token":
            token = getattr(gh_cfg, "token", None) or _env_token()
            if not token:
                raise ValueError(
                    "GitHub token not configured. Set github.token in config or GITHUB_TOKEN env var."
                )
            return cls.from_token(token)

        pem_path = Path(getattr(gh_cfg, "private_key_path", "")).expanduser()
        if not pem_path.exists():
            raise FileNotFoundError(f"GitHub App private key not found: {pem_path}")

        return cls.from_app(
            app_id=int(gh_cfg.app_id),
            installation_id=int(gh_cfg.installation_id),
            private_key_pem=pem_path.read_text(),
        )

    # ------------------------------------------------------------------ #
    # Token access
    # ------------------------------------------------------------------ #

    @property
    def token(self) -> str:
        """Return a token synchronously.

        Only valid for ``token`` mode (plain PAT).  Raises ``RuntimeError``
        for ``app`` mode — callers must use :meth:`async_token` instead so
        the event loop is not blocked by the HTTP round-trip to GitHub.
        """
        if self._mode == "token":
            assert self._static_token is not None
            return self._static_token
        raise RuntimeError(
            "GitHubAuth is in 'app' mode — use 'await auth.async_token()' "
            "to avoid blocking the event loop."
        )

    async def async_token(self) -> str:
        """Return a valid token, refreshing asynchronously when needed.

        Works for both ``token`` and ``app`` modes.  Always safe to ``await``
        from async code regardless of auth mode.
        """
        if self._mode == "token":
            assert self._static_token is not None
            return self._static_token
        return await self._installation_token()

    @property
    def headers(self) -> dict[str, str]:
        """Synchronous headers — only valid for plain-token mode."""
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def async_headers(self) -> dict[str, str]:
        """Async headers — works for both token and app modes."""
        return {
            "Authorization": f"Bearer {await self.async_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ------------------------------------------------------------------ #
    # Internal: App token lifecycle
    # ------------------------------------------------------------------ #

    async def _installation_token(self) -> str:
        """Return a valid installation token, refreshing if <5 min remain."""
        if self._cache is not None and self._cache.expires_at - time.monotonic() > 300:
            return self._cache.token

        token, expires_at = await self._fetch_installation_token()
        self._cache = _AppTokenCache(token=token, expires_at=expires_at)
        return token

    async def _fetch_installation_token(self) -> tuple[str, float]:
        """Call GitHub asynchronously to get a fresh installation token.

        Uses ``httpx.AsyncClient`` so the event loop is never blocked while
        waiting for the GitHub API response (fixes issue #141).
        """
        try:
            import jwt as _jwt
        except ImportError as exc:
            raise ImportError(
                "PyJWT is required for GitHub App auth — it is included in maxwell-daemon's default deps."
            ) from exc

        try:
            import httpx as _httpx
        except ImportError as exc:
            raise ImportError("httpx is required for GitHub App auth.") from exc

        now = int(time.time())
        payload = {"iat": now - 60, "exp": now + 600, "iss": str(self._app_id)}
        jwt_token = _jwt.encode(payload, self._private_key_pem, algorithm="RS256")

        async with _httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {jwt_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=15,
            )
        resp.raise_for_status()
        data = resp.json()
        token: str = data["token"]
        # expires_at is ISO-8601; convert to monotonic seconds remaining
        import datetime as _dt

        expires_iso: str = data["expires_at"]
        expires_utc = _dt.datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
        seconds_until = (expires_utc - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
        expires_mono = time.monotonic() + seconds_until
        return token, expires_mono


def _env_token() -> str | None:
    import os

    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
