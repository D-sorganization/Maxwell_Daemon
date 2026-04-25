"""Tests for JWT authentication and RBAC."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

pytest.importorskip("jwt")

from maxwell_daemon.auth import JWTConfig, Role


@pytest.fixture
def cfg() -> JWTConfig:
    return JWTConfig.generate(expiry_seconds=3600)


def _make_request(path: str = "/test") -> MagicMock:
    """Return a minimal mock FastAPI Request with a url.path attribute."""
    req = MagicMock()
    req.url.path = path
    return req


class TestRole:
    def test_admin_can_all_roles(self) -> None:
        assert Role.admin.can(Role.admin)
        assert Role.admin.can(Role.operator)
        assert Role.admin.can(Role.viewer)
        assert Role.admin.can(Role.developer)

    def test_viewer_cannot_admin(self) -> None:
        assert not Role.viewer.can(Role.admin)
        assert not Role.viewer.can(Role.operator)

    def test_developer_cannot_viewer(self) -> None:
        assert not Role.developer.can(Role.viewer)

    def test_operator_can_viewer(self) -> None:
        assert Role.operator.can(Role.viewer)
        assert Role.operator.can(Role.operator)
        assert not Role.operator.can(Role.admin)


class TestJWTConfig:
    def test_generate_creates_random_secret(self) -> None:
        cfg1 = JWTConfig.generate()
        cfg2 = JWTConfig.generate()
        assert cfg1.secret != cfg2.secret

    def test_empty_secret_raises(self) -> None:
        with pytest.raises(ValueError, match="secret"):
            JWTConfig("")

    def test_create_and_decode_roundtrip(self, cfg: JWTConfig) -> None:
        token = cfg.create_token("alice", Role.operator)
        claims = cfg.decode_token(token)
        assert claims.sub == "alice"
        assert claims.role == Role.operator
        assert claims.jti

    def test_create_token_assigns_unique_jti(self, cfg: JWTConfig) -> None:
        first = cfg.decode_token(cfg.create_token("alice", Role.viewer))
        second = cfg.decode_token(cfg.create_token("alice", Role.viewer))
        assert first.jti != second.jti

    def test_wrong_secret_raises(self, cfg: JWTConfig) -> None:
        import jwt

        token = cfg.create_token("alice", Role.admin)
        other = JWTConfig("differentSecret123456789012345678")
        with pytest.raises(jwt.InvalidTokenError):
            other.decode_token(token)

    def test_expired_token_raises(self) -> None:
        import jwt

        cfg = JWTConfig.generate(expiry_seconds=1)
        # expired 60s ago — outside the 30s leeway window
        token = cfg.create_token("alice", Role.viewer, expiry_seconds=-60)
        with pytest.raises(jwt.InvalidTokenError):
            cfg.decode_token(token)

    def test_custom_expiry(self, cfg: JWTConfig) -> None:
        token = cfg.create_token("bob", Role.developer, expiry_seconds=60)
        claims = cfg.decode_token(token)
        assert claims.sub == "bob"

    def test_extra_claims_preserved(self, cfg: JWTConfig) -> None:
        import jwt as _jwt

        token = cfg.create_token("alice", Role.admin, extra_claims={"org": "D-sorg"})
        raw = _jwt.decode(token, cfg.secret, algorithms=[cfg.algorithm])
        assert raw["org"] == "D-sorg"

    def test_reserved_extra_claims_rejected(self, cfg: JWTConfig) -> None:
        with pytest.raises(ValueError, match="reserved claims"):
            cfg.create_token("alice", Role.admin, extra_claims={"exp": 0})

    def test_unknown_role_in_payload_raises(self, cfg: JWTConfig) -> None:
        import jwt

        payload = {
            "sub": "alice",
            "role": "superadmin",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "jti": "test-jti",
        }
        token = jwt.encode(payload, cfg.secret, algorithm=cfg.algorithm)
        with pytest.raises(jwt.InvalidTokenError):
            cfg.decode_token(token)

    # --- Fix 1: Algorithm whitelist ---

    def test_auth_algorithm_whitelist_rejects_rs256(self) -> None:
        """Constructing JWTConfig with a non-HMAC algorithm must raise ValueError."""
        with pytest.raises(ValueError, match="not in the allowed set"):
            JWTConfig("somesecret", algorithm="RS256")

    def test_auth_algorithm_whitelist_rejects_none(self) -> None:
        """The 'none' algorithm must be rejected at config construction time."""
        with pytest.raises(ValueError, match="not in the allowed set"):
            JWTConfig("somesecret", algorithm="none")

    def test_auth_algorithm_whitelist_accepts_hs256(self) -> None:
        cfg = JWTConfig("somesecret", algorithm="HS256")
        assert cfg.algorithm == "HS256"

    def test_auth_algorithm_whitelist_accepts_hs384(self) -> None:
        cfg = JWTConfig("somesecret", algorithm="HS384")
        assert cfg.algorithm == "HS384"

    def test_auth_algorithm_whitelist_accepts_hs512(self) -> None:
        cfg = JWTConfig("somesecret", algorithm="HS512")
        assert cfg.algorithm == "HS512"

    # --- Fix 3: Generic error messages ---

    def test_auth_generic_error_message(self, cfg: JWTConfig) -> None:
        """A PyJWTError must surface as a generic 401 — no internal detail leaked."""
        import asyncio
        from unittest.mock import patch

        import jwt
        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)

        with (
            patch.object(cfg, "decode_token", side_effect=jwt.PyJWTError("Signature has expired")),
            pytest.raises(HTTPException) as exc_info,
        ):
            asyncio.run(dep(request=_make_request(), authorization="Bearer fake.token.here"))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Authentication failed"
        # The raw exception text must NOT appear in the response detail.
        assert "Signature has expired" not in str(exc_info.value.detail)

    def test_auth_non_pyjwt_error_message(self, cfg: JWTConfig) -> None:
        """A non-PyJWT exception must still surface as a generic 401."""
        import asyncio
        from unittest.mock import patch

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)

        with (
            patch.object(cfg, "decode_token", side_effect=RuntimeError("boom")),
            pytest.raises(HTTPException) as exc_info,
        ):
            asyncio.run(dep(request=_make_request(), authorization="Bearer fake.token.here"))

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Authentication failed"

    # --- Fix 2 & Fix 3: Clock drift leeway ---

    def test_auth_clock_skew_leeway_accepts_recently_expired(self) -> None:
        """A token expired 15s ago should be accepted (within the 30s leeway)."""

        cfg_leeway = JWTConfig.generate()
        # Expired 15s ago — inside the 30s leeway window, must be accepted.
        token = cfg_leeway.create_token("alice", Role.viewer, expiry_seconds=-15)
        claims = cfg_leeway.decode_token(token)
        assert claims.sub == "alice"

    def test_auth_clock_skew_leeway_rejects_long_expired(self) -> None:
        """A token expired 60s ago should be rejected (outside the 30s leeway)."""
        import jwt

        cfg_leeway = JWTConfig.generate()
        # Expired 60s ago — outside the 30s leeway window, must be rejected.
        token = cfg_leeway.create_token("alice", Role.viewer, expiry_seconds=-60)
        with pytest.raises(jwt.InvalidTokenError):
            cfg_leeway.decode_token(token)

    # --- Fix 4: require exp/iat/sub claims ---

    def test_decode_rejects_token_missing_exp(self, cfg: JWTConfig) -> None:
        """Tokens without an 'exp' claim must be rejected."""
        import jwt

        payload = {
            "sub": "alice",
            "role": "viewer",
            "iat": int(time.time()),
            "jti": "test-jti",
        }
        token = jwt.encode(payload, cfg.secret, algorithm=cfg.algorithm)
        with pytest.raises(jwt.InvalidTokenError):
            cfg.decode_token(token)

    def test_decode_rejects_token_missing_sub(self, cfg: JWTConfig) -> None:
        """Tokens without a 'sub' claim must be rejected."""
        import jwt

        payload = {
            "role": "viewer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
            "jti": "test-jti",
        }
        token = jwt.encode(payload, cfg.secret, algorithm=cfg.algorithm)
        with pytest.raises(jwt.InvalidTokenError):
            cfg.decode_token(token)

    def test_decode_rejects_token_missing_jti(self, cfg: JWTConfig) -> None:
        """Tokens without a 'jti' claim must be rejected."""
        import jwt

        payload = {
            "sub": "alice",
            "role": "viewer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 3600,
        }
        token = jwt.encode(payload, cfg.secret, algorithm=cfg.algorithm)
        with pytest.raises(jwt.InvalidTokenError):
            cfg.decode_token(token)


class TestTokenClaims:
    def test_has_role_delegates_to_role(self, cfg: JWTConfig) -> None:
        token = cfg.create_token("alice", Role.operator)
        claims = cfg.decode_token(token)
        assert claims.has_role(Role.operator)
        assert claims.has_role(Role.viewer)
        assert not claims.has_role(Role.admin)


class TestRequireRole:
    def test_valid_token_passes(self, cfg: JWTConfig) -> None:
        import asyncio

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        token = cfg.create_token("alice", Role.operator)
        claims = asyncio.run(dep(request=_make_request(), authorization=f"Bearer {token}"))
        assert claims.sub == "alice"

    def test_insufficient_role_raises_403(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.admin, cfg)
        token = cfg.create_token("alice", Role.viewer)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(request=_make_request(), authorization=f"Bearer {token}"))
        assert exc_info.value.status_code == 403

    def test_missing_token_raises_401(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(request=_make_request(), authorization=None))
        assert exc_info.value.status_code == 401

    def test_invalid_jwt_raises_401(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(request=_make_request(), authorization="Bearer not.a.valid.jwt"))
        assert exc_info.value.status_code == 401

    def test_invalid_jwt_detail_is_generic(self, cfg: JWTConfig) -> None:
        """Error detail must not expose internal JWT library messages."""
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(request=_make_request(), authorization="Bearer not.a.valid.jwt"))
        assert exc_info.value.detail == "Authentication failed"
