"""Token-bucket rate limiter for the REST API.

Applied as a FastAPI middleware; keyed by client IP (or bearer token when present)
and path group. Single-process and in-memory — deliberately simple. For multi-
instance deployments, terminate the rate limit at a reverse proxy.

Design
------
- One bucket per (key, group) pair.
- Each request tries to consume 1 token; over-limit requests return 429 with
  ``Retry-After`` set to the refill time.
- Unknown groups use the "default" group's rate/burst.
- Exempt paths (health/metrics probes) bypass the limiter entirely.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

__all__ = [
    "RateLimitGroup",
    "TokenBucket",
    "TokenBucketLimiter",
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

    def retry_after(self, key: str, *, group: str = "default") -> float:
        return self._bucket(key, group).retry_after_seconds()


def _classify(method: str, path: str) -> str:
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
        group = _classify(request.method, request.url.path)
        if not limiter.check(key, group=group):
            retry = max(1, round(limiter.retry_after(key, group=group)))
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        return await call_next(request)

    return limiter
