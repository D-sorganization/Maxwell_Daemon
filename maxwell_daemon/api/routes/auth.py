"""JWT authentication endpoints (phase 2 of #793).

Extracted from ``maxwell_daemon/api/server.py`` to give the auth surface its
own focused module.  These endpoints manage JWT issuance, refresh, revocation,
and identity inspection.

Wire-up — call ``register`` from ``create_app`` in ``server.py``::

    from maxwell_daemon.api.routes import auth as _auth_routes
    _auth_routes.register(app, daemon, jwt_config, auth_token, _require_admin, _require_operator)
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from maxwell_daemon.auth import JWTConfig, Role
from maxwell_daemon.daemon import Daemon
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "TokenRefreshRequest",
    "TokenRequest",
    "TokenResponse",
    "TokenRevokeRequest",
    "register",
]


class TokenRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=128)
    role: str = Field(default="viewer", pattern=r"^(admin|operator|viewer|developer)$")
    expiry_seconds: int | None = Field(default=None, ge=1, le=86400 * 30)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    refresh_token: str | None = None


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class TokenRevokeRequest(BaseModel):
    token: str | None = None
    all_for_subject: str | None = None


def register(  # noqa: C901
    app: FastAPI,
    daemon: Daemon,
    jwt_config: JWTConfig | None,
    auth_token: str | None,
    require_admin: Any,
    require_operator: Any,
) -> None:
    """Attach ``/api/v1/auth/*`` endpoints to ``app``.

    Parameters
    ----------
    app:
        The FastAPI application instance.
    daemon:
        The running daemon (provides access to the auth session store).
    jwt_config:
        JWT configuration, or ``None`` when JWT is disabled.
    auth_token:
        Static bearer token (used by the ``/me`` endpoint fallback).
    require_admin:
        Resolved FastAPI dependency that enforces admin role.
    require_operator:
        Resolved FastAPI dependency that enforces operator role.
    """

    @app.post("/api/v1/auth/token", dependencies=[Depends(require_admin)])
    async def issue_token(payload: Annotated[TokenRequest, Body()]) -> TokenResponse:
        """Issue a JWT with the requested role.

        Requires an admin credential.  The resulting JWT can then be used in
        place of the static token for role-scoped access.
        """
        if jwt_config is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "JWT not configured — set api.jwt_secret in config",
            )
        ttl = payload.expiry_seconds or jwt_config.expiry_seconds
        role = Role(payload.role)
        token = jwt_config.create_token(
            payload.subject, role, expiry_seconds=ttl, extra_claims={"typ": "access"}
        )

        refresh_ttl = 30 * 24 * 3600
        refresh_token = jwt_config.create_token(
            payload.subject,
            role,
            expiry_seconds=refresh_ttl,
            extra_claims={"typ": "refresh"},
        )

        claims = jwt_config.decode_token(token)
        refresh_claims = jwt_config.decode_token(refresh_token)

        auth_store = getattr(daemon, "_auth_store", None)
        if auth_store is not None:
            auth_store.record_session(claims.jti, payload.subject, claims.iat)
            auth_store.record_session(refresh_claims.jti, payload.subject, refresh_claims.iat)

        return TokenResponse(
            access_token=token,
            expires_in=ttl,
            role=role.value,
            refresh_token=refresh_token,
        )

    @app.post("/api/v1/auth/refresh")
    async def refresh_access_token(
        payload: Annotated[TokenRefreshRequest, Body()],
    ) -> TokenResponse:
        """Exchange a valid refresh token for a new access token."""
        if jwt_config is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "JWT not configured",
            )

        try:
            claims = jwt_config.decode_token(payload.refresh_token)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token") from exc

        if getattr(claims, "typ", "access") != "refresh":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token is not a refresh token")

        auth_store = getattr(daemon, "_auth_store", None)
        if auth_store is not None and auth_store.is_revoked(claims.jti):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token is revoked")

        ttl = jwt_config.expiry_seconds
        new_token = jwt_config.create_token(
            claims.sub, claims.role, expiry_seconds=ttl, extra_claims={"typ": "access"}
        )
        new_claims = jwt_config.decode_token(new_token)

        if auth_store is not None:
            auth_store.record_session(new_claims.jti, claims.sub, new_claims.iat)

        return TokenResponse(
            access_token=new_token,
            expires_in=ttl,
            role=claims.role.value,
            refresh_token=payload.refresh_token,
        )

    @app.post("/api/v1/auth/revoke", dependencies=[Depends(require_operator)])
    async def revoke_token(
        payload: Annotated[TokenRevokeRequest, Body()],
    ) -> dict[str, str]:
        """Revoke a specific token or all tokens for a subject."""
        auth_store = getattr(daemon, "_auth_store", None)
        if auth_store is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Auth session store not configured",
            )

        if payload.all_for_subject:
            auth_store.revoke_all_for_subject(payload.all_for_subject)
            return {"status": f"Revoked all tokens for {payload.all_for_subject}"}

        if not payload.token:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Must provide token or all_for_subject"
            )

        try:
            # We skip validation here to allow revoking expired tokens if needed
            import jwt

            decoded = jwt.decode(payload.token, options={"verify_signature": False})
            jti = decoded.get("jti")
            if jti:
                auth_store.revoke(jti)
            return {"status": "Revoked"}
        except Exception as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid token format") from exc

    @app.get("/api/v1/auth/me")
    async def whoami(
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Decode and return the caller's JWT claims (or static-token identity)."""
        if jwt_config is not None and authorization and authorization.startswith("Bearer "):
            raw = authorization.removeprefix("Bearer ").strip()
            try:
                claims = jwt_config.decode_token(raw)
                return {
                    "sub": claims.sub,
                    "role": claims.role.value,
                    "exp": claims.exp.isoformat(),
                }
            except Exception:  # noqa: BLE001
                # invalid/expired JWT, fall through to static token check
                pass  # nosec B110
        if auth_token is not None and authorization:
            raw = authorization.removeprefix("Bearer ").strip()
            if hmac.compare_digest(raw.encode(), auth_token.encode()):
                return {"sub": "static-token", "role": "admin", "exp": None}
        return {"sub": "anonymous", "role": None, "exp": None}
