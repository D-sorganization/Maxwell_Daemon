"""Unit tests for the security-headers middleware (#797 Phase 1)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.api.security_headers import (
    DEFAULT_CSP,
    DEFAULT_PERMISSIONS_POLICY,
    DEFAULT_REFERRER_POLICY,
    DEFAULT_STRICT_TRANSPORT_SECURITY,
    SecurityHeadersMiddleware,
    install_security_headers,
)


def _build_app() -> FastAPI:
    """Build a minimal FastAPI app with the security-headers middleware.

    Uses a single arbitrary route (``/ping``) so we can exercise the
    middleware without spinning up the full daemon stack.
    """
    app = FastAPI()
    install_security_headers(app)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    return app


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_x_content_type_options_set() -> None:
    """Verify ``X-Content-Type-Options: nosniff`` is set on responses."""
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"


def test_x_frame_options_set() -> None:
    """Verify ``X-Frame-Options: DENY`` is set on responses."""
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert r.headers["x-frame-options"] == "DENY"


def test_referrer_policy_set() -> None:
    """Verify the conservative Referrer-Policy default is emitted."""
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert r.headers["referrer-policy"] == DEFAULT_REFERRER_POLICY


def test_permissions_policy_set() -> None:
    """Verify Permissions-Policy disables geolocation/microphone/camera."""
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert r.headers["permissions-policy"] == DEFAULT_PERMISSIONS_POLICY


def test_csp_value_matches_expected() -> None:
    """Verify the Content-Security-Policy matches the documented default."""
    with _client(_build_app()) as c:
        r = c.get("/ping")
    csp = r.headers["content-security-policy"]
    assert csp == DEFAULT_CSP
    # And spot-check the CSP fragments — guards against accidental relaxation.
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "img-src 'self' data:" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "*" not in csp  # no wildcard sources


def test_hsts_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify HSTS is *not* emitted when MAXWELL_HSTS_ENABLED is unset."""
    monkeypatch.delenv("MAXWELL_HSTS_ENABLED", raising=False)
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_hsts_enabled_via_env(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify HSTS is emitted when MAXWELL_HSTS_ENABLED is truthy."""
    monkeypatch.setenv("MAXWELL_HSTS_ENABLED", flag)
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert r.headers["strict-transport-security"] == DEFAULT_STRICT_TRANSPORT_SECURITY


@pytest.mark.parametrize("flag", ["0", "false", "no", "off", ""])
def test_hsts_disabled_for_falsy_env(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsy / empty values for the env var must not enable HSTS."""
    monkeypatch.setenv("MAXWELL_HSTS_ENABLED", flag)
    with _client(_build_app()) as c:
        r = c.get("/ping")
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


def test_install_is_idempotent() -> None:
    """Calling ``install_security_headers`` twice should not duplicate the middleware."""
    app = FastAPI()
    install_security_headers(app)
    install_security_headers(app)

    matches = [
        m
        for m in getattr(app, "user_middleware", [])
        if getattr(m, "cls", None) is SecurityHeadersMiddleware
    ]
    assert len(matches) == 1


def test_existing_header_not_overwritten() -> None:
    """Headers explicitly set by a handler must not be clobbered."""
    app = FastAPI()
    install_security_headers(app)

    @app.get("/custom-frame")
    async def custom_frame() -> dict[str, str]:
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": True}, headers={"X-Frame-Options": "SAMEORIGIN"})  # type: ignore[return-value]

    with TestClient(app) as c:
        r = c.get("/custom-frame")
    # The handler's value wins — middleware only sets when absent.
    assert r.headers["x-frame-options"] == "SAMEORIGIN"
    # But the *other* defaults are still applied.
    assert r.headers["x-content-type-options"] == "nosniff"
