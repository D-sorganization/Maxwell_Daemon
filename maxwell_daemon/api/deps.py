"""Shared FastAPI dependency factories for auth and WebSocket gating.

Extracted from ``maxwell_daemon/api/server.py`` as part of epic #896
Phase 1.1 to keep ``server.py`` <=600 lines.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Header, HTTPException, Request, WebSocket, status

from maxwell_daemon.auth import JWTConfig, Role, is_jwt_auth_failure
from maxwell_daemon.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "make_auth_dep",
    "make_rbac_dep",
    "websocket_auth_or_close",
]


def make_auth_dep(token: str | None) -> Any:
    """Return a FastAPI dependency that validates a static bearer token.

    When ``token`` is ``None`` the dependency is a no-op (open/dev mode).
    """

    async def _check(authorization: Annotated[str | None, Header()] = None) -> None:
        if token is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
        presented = authorization.removeprefix("Bearer ").strip()
        # Constant-time comparison -- prevents leaking token via response timing.
        if not hmac.compare_digest(presented.encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    return _check


def make_rbac_dep(  # noqa: C901
    minimum: Role,
    static_token: str | None,
    jwt_config: JWTConfig | None,
    auth_store: Any | None = None,
    audit: Any | None = None,
) -> Any:
    """Return a FastAPI dependency that enforces *minimum* role.

    Accepts EITHER a valid static admin bearer token (treated as
    ``Role.admin``) OR a valid JWT bearer token whose role is >=
    *minimum*.  When neither JWT config nor static token is configured
    all requests pass (open/dev mode).
    """

    async def _dep(  # noqa: C901
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # Open mode -- nothing to enforce.
        if static_token is None and jwt_config is None:
            return

        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "bearer token required")

        raw = authorization.removeprefix("Bearer ").strip()

        # Fast path: static admin token -- always grants admin-level access.
        if static_token is not None and hmac.compare_digest(raw.encode(), static_token.encode()):
            if audit:
                audit.log_auth_decision(
                    subject="static",
                    role="admin",
                    endpoint=request.url.path,
                    outcome="pass",
                )
            return  # admitted as admin

        # JWT path.
        if jwt_config is None:
            if audit:
                audit.log_auth_decision(
                    subject="unknown",
                    role="none",
                    endpoint=request.url.path,
                    outcome="fail_no_jwt",
                )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

        try:
            claims = jwt_config.decode_token(raw)
        except Exception as exc:
            if audit:
                audit.log_auth_decision(
                    subject="unknown",
                    role="none",
                    endpoint=request.url.path,
                    outcome="fail_invalid_token",
                )
            if is_jwt_auth_failure(exc):
                log.warning("Auth failure: %s", exc, exc_info=False)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication failed",
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            ) from exc

        if (
            auth_store is not None
            and getattr(claims, "jti", None)
            and auth_store.is_revoked(claims.jti)
        ):
            if audit:
                audit.log_auth_decision(
                    subject=claims.sub,
                    role=claims.role.value,
                    endpoint=request.url.path,
                    outcome="fail_revoked",
                )
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token has been revoked")

        if getattr(claims, "typ", "access") != "access":
            if audit:
                audit.log_auth_decision(
                    subject=claims.sub,
                    role=claims.role.value,
                    endpoint=request.url.path,
                    outcome="fail_wrong_type",
                )
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Refresh tokens cannot be used as access tokens",
            )

        if not claims.has_role(minimum):
            if audit:
                audit.log_auth_decision(
                    subject=claims.sub,
                    role=claims.role.value,
                    endpoint=request.url.path,
                    outcome="fail_role_insufficient",
                )
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
            )

        if audit:
            audit.log_auth_decision(
                subject=claims.sub,
                role=claims.role.value,
                endpoint=request.url.path,
                outcome="pass",
            )

    return _dep


async def websocket_auth_or_close(
    ws: WebSocket,
    minimum: Role,
    static_token: str | None,
    jwt_config: JWTConfig | None,
    auth_store: Any | None = None,
    audit: Any | None = None,
) -> bool:
    """Authenticate a WebSocket query token against the static/JWT auth policy.

    Returns ``True`` when the client is admitted, ``False`` (and closes the
    socket) when authentication fails.
    """
    if static_token is None and jwt_config is None:
        return True

    presented = ws.query_params.get("token") or ""
    if not presented:
        await ws.close(code=1008)
        return False

    if static_token is not None and hmac.compare_digest(presented.encode(), static_token.encode()):
        return True

    if jwt_config is None:
        await ws.close(code=1008)
        return False

    try:
        claims = jwt_config.decode_token(presented)
    except Exception:  # nosec B110  # noqa: BLE001
        if audit:
            audit.log_auth_decision(
                subject="unknown",
                role="none",
                endpoint=ws.url.path,
                outcome="fail_invalid_token",
            )
        await ws.close(code=1008)
        return False

    if (
        auth_store is not None
        and getattr(claims, "jti", None)
        and auth_store.is_revoked(claims.jti)
    ):
        if audit:
            audit.log_auth_decision(
                subject=claims.sub,
                role=claims.role.value,
                endpoint=ws.url.path,
                outcome="fail_revoked",
            )
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="Token revoked")
        return False

    if getattr(claims, "typ", "access") != "access":
        if audit:
            audit.log_auth_decision(
                subject=claims.sub,
                role=claims.role.value,
                endpoint=ws.url.path,
                outcome="fail_wrong_type",
            )
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="Refresh token not allowed")
        return False

    if not claims.has_role(minimum):
        if audit:
            audit.log_auth_decision(
                subject=claims.sub,
                role=claims.role.value,
                endpoint=ws.url.path,
                outcome="fail_role_insufficient",
            )
        await ws.close(code=1008)
        return False

    if audit:
        audit.log_auth_decision(
            subject=claims.sub,
            role=claims.role.value,
            endpoint=ws.url.path,
            outcome="pass",
        )
    return True
