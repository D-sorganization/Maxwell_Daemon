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
from typing import Annotated, Any
from uuid import uuid4

from maxwell_daemon.logging import get_logger

__all__ = [
    "is_jwt_auth_failure",
    "JWTConfig",
    "require_role",
    "Role",
    "TokenClaims",
]

log = get_logger(__name__)

# Only HMAC-based symmetric algorithms are permitted.  Asymmetric or "none"
# algorithms open the door to algorithm-confusion attacks.
_ALLOWED_JWT_ALGORITHMS: frozenset[str] = frozenset({"HS256", "HS384", "HS512"})

# Clock drift leeway applied to all jwt.decode calls (seconds).
_LEEWAY_SECONDS: int = 30
_REQUIRED_JWT_CLAIMS = ("exp", "iat", "sub", "role", "jti")
_RESERVED_CLAIMS = frozenset(_REQUIRED_JWT_CLAIMS)


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

    def __init__(
        self, sub: str, role: Role, exp: datetime, *, iat: datetime, jti: str, typ: str = "access"
    ) -> None:
        self.sub = sub
        self.role = role
        self.exp = exp
        self.iat = iat
        self.jti = jti
        self.typ = typ

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
        JWT signing algorithm.  Must be one of HS256, HS384, or HS512.
        HS256 is the default and is suitable for single-server deployments
        where the daemon both issues and validates tokens.
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
        if algorithm not in _ALLOWED_JWT_ALGORITHMS:
            raise ValueError(
                f"JWT algorithm {algorithm!r} is not in the allowed set "
                f"{_ALLOWED_JWT_ALGORITHMS}. Use HS256, HS384, or HS512."
            )
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
        try:
            import jwt  # PyJWT
        except ImportError as exc:
            raise ImportError(
                "PyJWT is required to create tokens. Install maxwell-daemon[auth]."
            ) from exc

        if extra_claims is not None:
            reserved = _RESERVED_CLAIMS.intersection(extra_claims)
            if reserved:
                raise ValueError(
                    f"extra_claims may not override reserved claims: {sorted(reserved)!r}"
                )
        ttl = expiry_seconds if expiry_seconds is not None else self.expiry_seconds
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": subject,
            "role": role.value,
            "iat": now,
            "exp": now + timedelta(seconds=ttl),
            "jti": uuid4().hex,
            **(extra_claims or {}),
        }
        result: str = jwt.encode(payload, self.secret, algorithm=self.algorithm)
        return result

    def decode_token(self, token: str) -> TokenClaims:
        """Validate *token* and return its claims.

        Raises ``jwt.InvalidTokenError`` (or a subclass) on any failure:
        expired, bad signature, missing claims, unknown role, etc.
        """
        try:
            import jwt  # PyJWT
        except ImportError as exc:
            raise ImportError(
                "PyJWT is required to decode tokens. Install maxwell-daemon[auth]."
            ) from exc

        payload = jwt.decode(
            token,
            self.secret,
            algorithms=[self.algorithm],
            leeway=timedelta(seconds=_LEEWAY_SECONDS),
            options={"require": list(_REQUIRED_JWT_CLAIMS)},
        )
        sub: str = payload.get("sub", "")
        raw_role: str = payload.get("role", "")
        jti: str = payload.get("jti", "")
        try:
            role = Role(raw_role)
        except ValueError as exc:
            raise jwt.InvalidTokenError(f"unknown role {raw_role!r}") from exc
        iat_ts: int | float = payload.get("iat", 0)
        exp_ts: int | float = payload.get("exp", 0)
        iat = datetime.fromtimestamp(iat_ts, tz=timezone.utc)
        exp = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        typ: str = payload.get("typ", "access")
        return TokenClaims(sub=sub, role=role, exp=exp, iat=iat, jti=jti, typ=typ)


def is_jwt_auth_failure(exc: BaseException) -> bool:
    """Return True when *exc* came from PyJWT or a missing PyJWT dependency."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        module = type(current).__module__
        if module == "jwt" or module.startswith("jwt."):
            return True
        if isinstance(current, ModuleNotFoundError) and current.name == "jwt":
            return True
        if isinstance(current, ImportError) and "PyJWT" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def require_role(minimum: Role, jwt_config: JWTConfig) -> Any:
    """FastAPI dependency factory that enforces a minimum role.

    Usage::

        @app.post("/api/v1/tasks", dependencies=[Depends(require_role(Role.operator, cfg))])

    The request must carry ``Authorization: Bearer <JWT>``.  Returns the
    decoded ``TokenClaims`` (so callers can inspect ``claims.sub`` etc.).
    """
    from fastapi import Header, HTTPException, Request, status

    async def _dep(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> TokenClaims:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "JWT bearer token required")
        raw = authorization.removeprefix("Bearer ").strip()
        try:
            claims = jwt_config.decode_token(raw)
        except Exception as exc:
            if is_jwt_auth_failure(exc):
                log.warning(
                    "Auth failure for endpoint %s: %s",
                    request.url.path,
                    exc,
                    exc_info=False,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication failed",  # generic — don't leak exc details
                ) from exc
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication failed",
            ) from exc
        if not claims.has_role(minimum):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"role {claims.role.value!r} lacks {minimum.value!r} privileges",
            )
        return claims

    # Ensure FastAPI can resolve the Header annotation even with PEP 563 (from __future__ import
    # annotations). Without this, `get_type_hints(_dep)` raises NameError because `Header` is
    # local to require_role and not in auth.py's module globals.
    _dep.__annotations__ = {
        "request": Request,
        "authorization": Annotated[str | None, Header()],
        "return": TokenClaims,
    }

    return _dep
