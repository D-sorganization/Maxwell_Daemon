"""Rate-limiting primitives for the REST API.

Two independent flavours live in this module:

1. **Legacy token-bucket middleware** — used by ``install_rate_limiter`` to put a
   per-IP / per-bearer-token cap on every request. Keyed by client IP (or
   bearer token when present) and path group. Single-process and in-memory.

2. **Phase-1 sliding-window dependency** (``RateLimitStore`` protocol +
   ``InMemoryRateLimitStore`` + ``rate_limit_dependency``) — a per-endpoint,
   opt-in FastAPI ``Depends`` that emits the standard ``RateLimit-*`` headers
   and returns ``HTTP 429`` with ``Retry-After`` when a caller exceeds the
   policy. Currently wired only to ``POST /api/dispatch``; enable via
   ``APIConfig.dispatch_rate_limit`` (disabled by default).

For multi-instance deployments either terminate rate limiting at the reverse
proxy or swap ``InMemoryRateLimitStore`` for a Redis-backed implementation
(follow-up — see issue #796).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

__all__ = [
    "InMemoryRateLimitStore",
    "RateLimitGroup",
    "RateLimitPolicy",
    "RateLimitResult",
    "RateLimitStore",
    "TokenBucket",
    "TokenBucketLimiter",
    "build_rate_limit_dependency",
    "extract_client_id",
    "install_rate_limit_headers_middleware",
    "install_rate_limiter",
]


@dataclass(slots=True)
class TokenBucket:
    capacity: int
    refill_per_second: float
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()

    def try_consume(self, amount: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    def has_capacity(self, amount: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            return self._tokens >= amount

    def consume(self, amount: float = 1.0) -> None:
        with self._lock:
            self._refill()
            self._tokens = max(0.0, self._tokens - amount)

    def refund(self, amount: float = 1.0) -> None:
        with self._lock:
            self._refill()
            self._tokens = min(float(self.capacity), self._tokens + amount)

    def retry_after_seconds(self) -> float:
        """Seconds until at least 1 token is available."""
        with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                return 0.0
            missing = 1.0 - self._tokens
            return missing / self.refill_per_second if self.refill_per_second > 0 else 1.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._updated = now


@dataclass(slots=True, frozen=True)
class RateLimitGroup:
    rate: float
    burst: int


class TokenBucketLimiter:
    """Per-(key, group) token bucket registry."""

    def __init__(
        self,
        *,
        default_rate: float,
        default_burst: int,
        groups: Mapping[str, Mapping[str, float]] | None = None,
    ) -> None:
        self._default = RateLimitGroup(rate=default_rate, burst=default_burst)
        self._groups: dict[str, RateLimitGroup] = {
            name: RateLimitGroup(rate=float(cfg["rate"]), burst=int(cfg["burst"]))
            for name, cfg in (groups or {}).items()
        }
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()

    def _group(self, name: str) -> RateLimitGroup:
        return self._groups.get(name, self._default)

    def _bucket(self, key: str, group: str) -> TokenBucket:
        g = self._group(group)
        cache_key = (key, group)
        with self._lock:
            bucket = self._buckets.get(cache_key)
            if bucket is None:
                bucket = TokenBucket(capacity=g.burst, refill_per_second=g.rate)
                self._buckets[cache_key] = bucket
            return bucket

    def check(self, key: str, *, group: str = "default") -> bool:
        return self._bucket(key, group).try_consume()

    def has_capacity(self, key: str, *, group: str = "default", amount: float = 1.0) -> bool:
        return self._bucket(key, group).has_capacity(amount)

    def consume(self, key: str, *, group: str = "default", amount: float = 1.0) -> None:
        self._bucket(key, group).consume(amount)

    def refund(self, key: str, *, group: str = "default", amount: float = 1.0) -> None:
        self._bucket(key, group).refund(amount)

    def retry_after(self, key: str, *, group: str = "default") -> float:
        return self._bucket(key, group).retry_after_seconds()


def _classify(method: str, _path: str) -> str:
    """Map a request to a rate-limit group. Writes get their own budget so
    that a burst of GETs can't starve actual work submissions."""
    if method in {"POST", "PUT", "DELETE", "PATCH"}:
        return "writes"
    return "default"


def _client_key(request: Request) -> str:
    """Prefer the bearer token (authenticated caller) over the IP, so a
    well-behaved client isn't punished by a misbehaving NAT peer."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        # Hash prefix to keep the key space bounded without logging tokens.
        import hashlib

        return "tok:" + hashlib.sha256(auth.encode()).hexdigest()[:16]
    return "ip:" + (request.client.host if request.client else "unknown")


def install_rate_limiter(
    app: FastAPI,
    *,
    default_rate: float,
    default_burst: int,
    groups: Mapping[str, Mapping[str, float]],
    exempt_paths: Iterable[str] = ("/health", "/metrics"),
) -> TokenBucketLimiter:
    """Attach a rate-limit middleware to the given FastAPI app.

    Returns the underlying limiter so tests / metrics integrations can inspect
    bucket state.
    """
    if "auth_failures" not in groups:
        # Default policy: permit 5 rapid auth failures, then 1 per 10s.
        groups_dict = dict(groups)
        groups_dict["auth_failures"] = {"rate": 0.1, "burst": 5}
        groups = groups_dict

    limiter = TokenBucketLimiter(
        default_rate=default_rate,
        default_burst=default_burst,
        groups=groups,
    )
    exempt = frozenset(exempt_paths)

    @app.middleware("http")
    async def _rate_limit_middleware(request: Request, call_next) -> object:  # type: ignore[no-untyped-def]
        if request.url.path in exempt:
            return await call_next(request)
        key = _client_key(request)

        if not limiter.check(key, group="auth_failures"):
            retry = max(1, round(limiter.retry_after(key, group="auth_failures")))
            return JSONResponse(
                {"detail": "too many authentication failures"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        group = _classify(request.method, request.url.path)
        if not limiter.check(key, group=group):
            limiter.refund(key, group="auth_failures", amount=1.0)
            retry = max(1, round(limiter.retry_after(key, group=group)))
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )

        response = await call_next(request)
        if response.status_code != 401:
            limiter.refund(key, group="auth_failures", amount=1.0)

        return response

    return limiter


# ─────────────────────────────────────────────────────────────────────────────
# Phase-1: per-endpoint sliding-window dependency.
#
# Designed as a FastAPI ``Depends`` rather than middleware so each route can
# have its own policy without scanning all requests. The store is pluggable so
# we can swap in Redis later without touching the call sites.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class RateLimitPolicy:
    """A per-endpoint rate-limit rule.

    ``limit`` requests are allowed within any rolling ``window_seconds`` window.
    """

    limit: int
    window_seconds: float

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("RateLimitPolicy.limit must be >= 1")
        if self.window_seconds <= 0:
            raise ValueError("RateLimitPolicy.window_seconds must be > 0")


@dataclass(slots=True, frozen=True)
class RateLimitResult:
    """Outcome of a single ``RateLimitStore.hit`` call.

    Attributes
    ----------
    allowed:
        True if the request fits inside the policy.
    limit:
        The policy's request limit.
    remaining:
        Requests remaining in the current window after this call. Floor at 0.
    reset_seconds:
        Seconds until the window's oldest hit expires (i.e. when 1 token
        becomes available again). 0 when ``allowed`` is True and there is
        spare headroom.
    retry_after_seconds:
        Suggested ``Retry-After`` value when ``allowed`` is False. Always >= 1
        so we never advertise a sub-second retry.
    """

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int
    retry_after_seconds: int


class RateLimitStore(Protocol):
    """Pluggable backend for sliding-window rate limiting.

    Implementations must be safe to call from multiple coroutines. The
    in-memory implementation uses an ``asyncio.Lock``; a Redis-backed
    implementation would use ``INCR`` + ``EXPIRE`` or a Lua script.
    """

    async def hit(self, key: str, policy: RateLimitPolicy) -> RateLimitResult:
        """Record one request and return whether it's permitted under ``policy``."""


class InMemoryRateLimitStore:
    """Single-process sliding-window store.

    Tracks the timestamps of the last ``policy.limit`` requests for each key.
    Memory cost is O(keys * limit). Suitable for a single daemon instance —
    swap for Redis when running >1 daemon behind a load balancer.
    """

    def __init__(self, *, monotonic: object = None) -> None:
        # ``monotonic`` is a hook for tests so we don't have to ``time.sleep``.
        self._now: object = monotonic or time.monotonic
        self._buckets: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    def _clock(self) -> float:
        clock = self._now
        # ``Callable`` would force callers to import typing; this stays light.
        return float(clock())  # type: ignore[operator]

    async def hit(self, key: str, policy: RateLimitPolicy) -> RateLimitResult:
        async with self._lock:
            now = self._clock()
            cutoff = now - policy.window_seconds
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            # Drop hits that have aged out of the window.
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= policy.limit:
                # Over limit: compute reset / retry-after from the oldest hit
                # without recording the new attempt.
                oldest = bucket[0]
                reset = max(0.0, (oldest + policy.window_seconds) - now)
                reset_ceiled = max(1, int(reset) + (0 if reset.is_integer() else 1))
                return RateLimitResult(
                    allowed=False,
                    limit=policy.limit,
                    remaining=0,
                    reset_seconds=reset_ceiled,
                    retry_after_seconds=reset_ceiled,
                )

            bucket.append(now)
            remaining = policy.limit - len(bucket)
            if bucket:
                oldest = bucket[0]
                reset = max(0.0, (oldest + policy.window_seconds) - now)
                reset_ceiled = max(0, int(reset) + (0 if reset.is_integer() else 1))
            else:
                reset_ceiled = 0
            return RateLimitResult(
                allowed=True,
                limit=policy.limit,
                remaining=remaining,
                reset_seconds=reset_ceiled,
                retry_after_seconds=0,
            )

    async def reset(self, key: str | None = None) -> None:
        """Clear all buckets, or a single key. Test helper."""
        async with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


def extract_client_id(request: Request) -> str:
    """Best-effort identity for rate-limit bucketing.

    Only **verified** identities — those attached to ``request.state`` by an
    upstream auth middleware after validating the credential — may key a
    bucket. Unverified credentials (e.g. an arbitrary ``Authorization: Bearer``
    header on an endpoint that authorizes via a body field instead) MUST NOT
    influence the key, otherwise an attacker can rotate fake tokens to mint a
    fresh bucket per request and bypass throttling.

    Order of precedence:
    1. ``request.state.jwt_sub`` — verified JWT subject claim.
    2. ``request.state.auth_token_id`` — verified static-token identifier.
    3. ``X-Forwarded-For`` left-most entry, when the daemon trusts a proxy.
    4. ``request.client.host`` (direct connection).
    5. The literal string ``"ip:unknown"`` so we never raise on weird clients.
    """
    state = getattr(request, "state", None)

    # 1. Authenticated JWT subject, attached by the JWT auth middleware.
    sub = getattr(state, "jwt_sub", None)
    if isinstance(sub, str) and sub:
        return f"user:{sub}"

    # 2. Verified static-token identifier, attached by static-token auth.
    token_id = getattr(state, "auth_token_id", None)
    if isinstance(token_id, str) and token_id:
        return f"token:{token_id}"

    # 3. Trust the proxy chain only if the daemon has been told to (via the
    #    standard ``X-Forwarded-For`` header). We take the left-most entry.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        first = fwd.split(",", 1)[0].strip()
        if first:
            return f"ip:{first}"

    # 4. Direct connection IP.
    if request.client is not None and request.client.host:
        return f"ip:{request.client.host}"

    return "ip:unknown"


def _record_exceeded(endpoint: str) -> None:
    """Bump the Prometheus counter, swallowing import errors so tests that
    don't exercise metrics aren't forced to import prometheus_client."""
    try:
        from maxwell_daemon.metrics import RATE_LIMIT_EXCEEDED_TOTAL

        RATE_LIMIT_EXCEEDED_TOTAL.labels(endpoint=endpoint).inc()
    except Exception:  # noqa: BLE001 — metrics must never break the request path
        # Swallow any registry / import / labelling error so the 429 still fires.
        pass


def build_rate_limit_dependency(
    *,
    endpoint: str,
    policy: RateLimitPolicy,
    store: RateLimitStore,
) -> object:
    """Return a FastAPI dependency that enforces ``policy`` on ``endpoint``.

    The dependency:

    * Identifies the caller via :func:`extract_client_id`.
    * Calls ``store.hit`` with a key namespaced by ``endpoint``.
    * Sets ``RateLimit-Limit``, ``RateLimit-Remaining``, and ``RateLimit-Reset``
      response headers on the way out.
    * Raises ``HTTPException(429)`` with ``Retry-After`` when the policy is
      exceeded, after bumping the ``rate_limit_exceeded_total`` counter.
    """

    async def _dep(request: Request) -> None:
        client_id = extract_client_id(request)
        key = f"{endpoint}:{client_id}"
        result = await store.hit(key, policy)

        # Stash the result so we can attach headers in a response middleware.
        request.state.rate_limit_result = result
        request.state.rate_limit_endpoint = endpoint

        if not result.allowed:
            _record_exceeded(endpoint)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit exceeded for {endpoint}. "
                    f"Retry after {result.retry_after_seconds} seconds."
                ),
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "RateLimit-Limit": str(result.limit),
                    "RateLimit-Remaining": "0",
                    "RateLimit-Reset": str(result.reset_seconds),
                },
            )

    return _dep


def install_rate_limit_headers_middleware(app: FastAPI) -> None:
    """Attach the ``RateLimit-*`` headers stashed by ``build_rate_limit_dependency``.

    Idempotent: calling twice is a no-op beyond the (negligible) cost of a
    second middleware on the stack. The middleware looks for
    ``request.state.rate_limit_result`` and, if present, copies the headers
    onto the outgoing response.
    """

    @app.middleware("http")
    async def _rate_limit_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        result = getattr(request.state, "rate_limit_result", None)
        if isinstance(result, RateLimitResult):
            response.headers.setdefault("RateLimit-Limit", str(result.limit))
            response.headers.setdefault("RateLimit-Remaining", str(result.remaining))
            response.headers.setdefault("RateLimit-Reset", str(result.reset_seconds))
        return response
