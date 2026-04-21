"""JWT authentication and Role-Based Access Control (RBAC).

Tokens carry a *role* claim that gates access to API endpoints:

  ``admin``     — full fleet control (all endpoints)
  ``operator``  — start/stop agents, view logs (most write endpoints)
  ``viewer``    — read-only dashboard access (GET only)
  ``developer`` — can only see tasks they submitted (no fleet write ops)

Usage::

    cfg = JWTConfig(secret="...", expiry_seconds=3600)
    token = cfg.create_token("alice", Role.operator)

    # In FastAPI — use ``require_role`` as a dependency:
    @app.post("/api/v1/tasks", dependencies=[Depends(require_role(Role.operator, cfg))])
    async def submit_task(...): ...

The module is intentionally decoupled from FastAPI: ``JWTConfig`` and
``decode_token`` are pure functions with no framework dependency.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

__all__ = [
    "JWTConfig",
    "Role",
    "TokenClaims",
    "require_role",
    "require_role_or_token",
]


class Role(str, Enum):
    """RBAC roles in descending privilege order."""

    admin = "admin"
    operator = "operator"
    viewer = "viewer"
    developer = "developer"

    def can(self, minimum: Role) -> bool:
        """Return True if this role has at least the privileges of *minimum*."""
        order = [Role.admin, Role.operator, Role.viewer, Role.developer]
        return order.index(self) <= order.index(minimum)


class TokenClaims:
    """Decoded, validated JWT claims."""

    def __init__(self, sub: str, role: Role, exp: datetime) -> None:
        self.sub = sub
        self.role = role
        self.exp = exp

    def has_role(self, minimum: Role) -> bool:
        return self.role.can(minimum)


class JWTConfig:
    """Configuration for JWT token issuance and validation.

    Parameters
    ----------
    secret:
        HMAC-SHA256 signing secret.  Generate with
        ``secrets.token_hex(32)`` and store in your config file.
    algorithm:
        JWT signing algorithm.  HS256 is the default and is suitable
        for single-server deployments where the daemon both issues and
        validates tokens.
    expiry_seconds:
        Default token lifetime.  Callers may override per-token.
    """

    def __init__(
        self,
        secret: str,
        *,
        algorithm: str = "HS256",
        expiry_seconds: int = 3600,
    ) -> None:
        if not secret:
            raise ValueError("JWT secret must be non-empty")
        self.secret = secret
        self.algorithm = algorithm
        self.expiry_seconds = expiry_seconds

    @classmethod
    def generate(cls, *, expiry_seconds: int = 3600) -> JWTConfig:
        """Create a config with a randomly generated secret (for testing)."""
        return cls(secrets.token_hex(32), expiry_seconds=expiry_seconds)

    def create_token(
        self,
        subject: str,
        role: Role,
        *,
        expiry_seconds: int | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Issue a signed JWT for *subject* with *role*."""
        import jwt  # PyJWT

        ttl = expiry_seconds if expiry_seconds is not None else self.expiry_seconds
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": subject,
            "role": role.value,
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
            **(extra_claims or {}),
        }
        result: str = jwt.encode(payload, self.secret, algorithm=self.algorithm)
        return result

    def decode_token(self, token: str) -> TokenClaims:
        """Validate *token* and return its claims.

        Raises ``jwt.InvalidTokenError`` (or a subclass) on any failure:
        expired, bad signature, missing claims, unknown role, etc.
        """
        import jwt  # PyJWT

        payload = jwt.decode(token, self.secret, algorithms=[self.algorithm])
        sub: str = payload.get("sub", "")
        raw_role: str = payload.get("role", "")
        try:
            role = Role(raw_role)
        except ValueError as exc:
            raise jwt.InvalidTokenError(f"unknown role {raw_role!r}") from exc
        exp_ts: int | float = payload.get("exp", 0)
        exp = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        return TokenClaims(sub=sub, role=role, exp=exp)


def require_role(minimum: Role, jwt_config: JWTConfig) -> Any:
    """FastAPI dependency factory that enforces a minimum role.

    Usage::

        @app.post("/api/v1/tasks", dependencies=[Depends(require_role(Role.operator, cfg))])

    The request must carry ``Authorization: Bearer <JWT>``.  Returns the
    decoded ``TokenClaims`` (so callers can inspect ``claims.sub`` etc.).
    """
    from typing import Annotated

    from fastapi import Header, HTTPException, status

    async def _dep(authorization: Annotated[str | None, Header()] = None) -> TokenClaims:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "JWT bearer token required")
        raw = authorization.removeprefix("Bearer ").strip()
        try:
            claims = jwt_config.decode_token(raw)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}") from exc
        if not claims.has_role(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
            )
        return claims

    return _dep


def require_role_or_token(
    minimum: Role,
    jwt_config: JWTConfig | None,
    static_token: str | None,
) -> Any:
    """FastAPI dependency factory supporting both JWT RBAC and static bearer token.

    This is the backward-compatible variant:

    * If a valid JWT is presented and ``jwt_config`` is set, the JWT role is
      checked against ``minimum``.
    * If the static bearer token is presented (and matches ``static_token``),
      the caller is treated as ``Role.admin`` — full access.
    * If neither JWT nor static token is configured, the dependency is a no-op
      (open access, for local/dev deployments).

    Returns the decoded ``TokenClaims`` when a JWT is used, or a synthetic
    ``TokenClaims`` with ``sub="static-token"`` / ``role=Role.admin`` when
    the static token is used.
    """
    import hmac as _hmac
    from datetime import datetime, timezone
    from typing import Annotated

    from fastapi import Header, HTTPException, status

    async def _dep(authorization: Annotated[str | None, Header()] = None) -> TokenClaims | None:
        # No auth configured at all — open access.
        if jwt_config is None and static_token is None:
            return None

        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

        raw = authorization.removeprefix("Bearer ").strip()

        # Try JWT first when configured.
        if jwt_config is not None:
            try:
                claims = jwt_config.decode_token(raw)
                if not claims.has_role(minimum):
                    raise HTTPException(
                        status.HTTP_403_FORBIDDEN,
                        f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
                    )
                return claims
            except HTTPException:
                raise
            except Exception:
                # JWT decode failed — fall through to static token check.
                pass

        # Static token fallback (treated as admin).
        if static_token is not None:
            if _hmac.compare_digest(raw.encode(), static_token.encode()):
                exp = datetime(9999, 12, 31, tzinfo=timezone.utc)
                return TokenClaims(sub="static-token", role=Role.admin, exp=exp)

        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")

    return _dep
