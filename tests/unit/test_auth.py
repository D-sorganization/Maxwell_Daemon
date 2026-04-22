"""Tests for JWT authentication and RBAC."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("jwt")

from maxwell_daemon.auth import JWTConfig, Role


@pytest.fixture
def cfg() -> JWTConfig:
    return JWTConfig.generate(expiry_seconds=3600)


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

    def test_wrong_secret_raises(self, cfg: JWTConfig) -> None:
        import jwt

        token = cfg.create_token("alice", Role.admin)
        other = JWTConfig("differentSecret123456789012345678")
        with pytest.raises(jwt.InvalidTokenError):
            other.decode_token(token)

    def test_expired_token_raises(self) -> None:
        import jwt

        cfg = JWTConfig.generate(expiry_seconds=1)
        token = cfg.create_token("alice", Role.viewer, expiry_seconds=-1)
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

    def test_unknown_role_in_payload_raises(self, cfg: JWTConfig) -> None:
        import jwt

        payload = {"sub": "alice", "role": "superadmin", "exp": int(time.time()) + 3600}
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
        claims = asyncio.run(dep(authorization=f"Bearer {token}"))
        assert claims.sub == "alice"

    def test_insufficient_role_raises_403(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.admin, cfg)
        token = cfg.create_token("alice", Role.viewer)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(authorization=f"Bearer {token}"))
        assert exc_info.value.status_code == 403

    def test_missing_token_raises_401(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(authorization=None))
        assert exc_info.value.status_code == 401

    def test_invalid_jwt_raises_401(self, cfg: JWTConfig) -> None:
        import asyncio

        from fastapi import HTTPException

        from maxwell_daemon.auth import require_role

        dep = require_role(Role.viewer, cfg)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(dep(authorization="Bearer not.a.valid.jwt"))
        assert exc_info.value.status_code == 401
