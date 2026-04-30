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

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from maxwell_daemon.logging import get_logger
from maxwell_daemon.metrics import record_ratelimit_rejection

__all__ = [
    "DEFAULT_EXEMPT_PATHS",
    "ENV_DEFAULT_PER_MIN",
    "ENV_TRUST_PROXY",
    "ENV_WRITE_PER_MIN",
    "RateLimitGroup",
    "TokenBucket",
    "TokenBucketLimiter",
    "install_env_rate_limiter",
    "install_rate_limiter",
]

_log = get_logger(__name__)

# Env-var knobs. Values are integers (requests per minute).
ENV_DEFAULT_PER_MIN = "MAXWELL_RATELIMIT_DEFAULT_PER_MIN"
ENV_WRITE_PER_MIN = "MAXWELL_RATELIMIT_WRITE_PER_MIN"
# When set to a truthy value ("1" / "true" / "yes" / "on", case-insensitive),
# the limiter trusts the left-most ``X-Forwarded-For`` entry as the client IP.
# Off by default so direct callers can't spoof a fresh IP per request to evade
# the bucket and force unbounded memory growth.
ENV_TRUST_PROXY = "MAXWELL_TRUST_PROXY"

# Sane defaults — generous enough for a polling dashboard, strict enough on
# state-mutating endpoints to prevent dispatch / control floods.
_DEFAULT_PER_MIN_FALLBACK = 120
_WRITE_PER_MIN_FALLBACK = 30

# Hard cap on the in-memory bucket dict. The env limiter keys by client IP;
# even with the XFF default tightened, a misbehaving NAT could still surface
# many distinct peers. Once we exceed the cap, evict oldest first (FIFO) so
# memory stays bounded.
_MAX_BUCKETS = 10_000
# Log one warning per N evictions to surface pressure without flooding.
_EVICTION_LOG_INTERVAL = 100
# Truthy strings honored by ``MAXWELL_TRUST_PROXY``.
_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

# Liveness/contract probes must never be throttled — the dashboard polls these
# constantly and an outage of /api/health would make a noisy daemon look dead.
DEFAULT_EXEMPT_PATHS: frozenset[str] = frozenset({"/api/health", "/api/version"})

# Routes whose POSTs get the stricter "write" budget.
_WRITE_PATH_EXACT: frozenset[str] = frozenset({"/api/dispatch"})
_WRITE_PATH_PREFIXES: tuple[str, ...] = ("/api/control/",)


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
    """Per-(key, group) token bucket registry.

    The internal bucket dict is bounded at ``max_buckets`` entries; on overflow
    the oldest-inserted bucket is evicted (FIFO). This protects against memory
    DOS when an attacker spoofs many distinct keys.
    """

    def __init__(
        self,
        *,
        default_rate: float,
        default_burst: int,
        groups: Mapping[str, Mapping[str, float]] | None = None,
        max_buckets: int = _MAX_BUCKETS,
    ) -> None:
        self._default = RateLimitGroup(rate=default_rate, burst=default_burst)
        self._groups: dict[str, RateLimitGroup] = {
            name: RateLimitGroup(rate=float(cfg["rate"]), burst=int(cfg["burst"]))
            for name, cfg in (groups or {}).items()
        }
        # OrderedDict so we can FIFO-evict the oldest inserted bucket cheaply.
        self._buckets: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()
        self._lock = threading.Lock()
        self._max_buckets = max(1, int(max_buckets))
        self._evictions = 0

    def _group(self, name: str) -> RateLimitGroup:
        return self._groups.get(name, self._default)

    def _bucket(self, key: str, group: str) -> TokenBucket:
        g = self._group(group)
        cache_key = (key, group)
        evicted_key: tuple[str, str] | None = None
        evictions_so_far = 0
        with self._lock:
            bucket = self._buckets.get(cache_key)
            if bucket is None:
                bucket = TokenBucket(capacity=g.burst, refill_per_second=g.rate)
                self._buckets[cache_key] = bucket
                if len(self._buckets) > self._max_buckets:
                    evicted_key, _ = self._buckets.popitem(last=False)
                    self._evictions += 1
                    evictions_so_far = self._evictions
            return_bucket = bucket
        if evicted_key is not None and evictions_so_far % _EVICTION_LOG_INTERVAL == 1:
            _log.warning(
                "ratelimit_bucket_evicted",
                evicted_key=evicted_key[0],
                evicted_group=evicted_key[1],
                total_evictions=evictions_so_far,
                max_buckets=self._max_buckets,
            )
        return return_bucket

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
    # Sentinels so a later ``install_env_rate_limiter()`` call can detect the
    # config-driven middleware and skip stacking a second token bucket.
    app.state.rate_limiter = limiter
    app.state.rate_limiter_installed = True
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


# ---------------------------------------------------------------------------
# Env-driven middleware (Phase 1 of #796) — keyed strictly by client IP so the
# limiter still bites for unauthenticated callers. The two budgets here ("default"
# and "write") are tuned for the dashboard polling pattern + dispatch/control
# write traffic; richer per-route classes live in the YAML-driven limiter above.
# ---------------------------------------------------------------------------


def _trust_proxy_enabled() -> bool:
    """Return True iff ``MAXWELL_TRUST_PROXY`` is set to a truthy value.

    Defaults to False so the limiter ignores ``X-Forwarded-For`` unless the
    operator explicitly opts in (i.e. has a trusted reverse proxy in front).
    Without this gate, any direct caller could rotate the XFF header to mint
    a fresh bucket per request and bypass the limiter entirely.
    """
    raw = os.environ.get(ENV_TRUST_PROXY)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY_ENV_VALUES


def _ip_key(request: Request) -> str:
    """Resolve the client IP for rate-limit keying.

    By default we use the direct ASGI peer (``request.client.host``). The
    left-most ``X-Forwarded-For`` value is honored only when
    ``MAXWELL_TRUST_PROXY`` is enabled; this prevents header spoofing from
    evading per-IP rate limits or driving unbounded bucket-dict growth.
    Falls back to ``"unknown"`` when the transport exposes no peer (test
    client, ASGI lifespan, …).
    """
    if _trust_proxy_enabled():
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            first = xff.split(",", 1)[0].strip()
            if first:
                return f"ip:{first}"
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "ip:unknown"


def _route_class(method: str, path: str) -> str:
    """Map (method, path) to a rate-limit class.

    Only ``POST`` to dispatch / control endpoints is treated as a "write"
    today — every other request shares the lenient default budget. Keeping
    the rule explicit (rather than method-only) avoids accidentally
    throttling future read-only POST endpoints.
    """
    if method == "POST":
        if path in _WRITE_PATH_EXACT:
            return "write"
        if any(path.startswith(prefix) for prefix in _WRITE_PATH_PREFIXES):
            return "write"
    return "default"


def _read_per_min(env_name: str, fallback: int) -> int:
    """Read a positive int from the environment, falling back on bad input."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = int(raw)
    except ValueError:
        _log.warning(
            "ratelimit_invalid_env",
            env=env_name,
            value=raw,
            fallback=fallback,
        )
        return fallback
    if value <= 0:
        _log.warning(
            "ratelimit_non_positive_env",
            env=env_name,
            value=value,
            fallback=fallback,
        )
        return fallback
    return value


def install_env_rate_limiter(
    app: FastAPI,
    *,
    exempt_paths: Iterable[str] = DEFAULT_EXEMPT_PATHS,
    default_per_min: int | None = None,
    write_per_min: int | None = None,
) -> TokenBucketLimiter | None:
    """Install the per-IP rate-limit middleware described in #796 (Phase 1).

    The limiter is intentionally separate from :func:`install_rate_limiter` so
    that operators can opt in via env vars without disturbing config-driven
    deployments. Reads ``MAXWELL_RATELIMIT_DEFAULT_PER_MIN`` (default 120) and
    ``MAXWELL_RATELIMIT_WRITE_PER_MIN`` (default 30) when the corresponding
    keyword argument is ``None`` — explicit kwargs win, which keeps tests
    hermetic.

    Honors ``MAXWELL_TRUST_PROXY`` (off by default) for the keying decision —
    see :func:`_ip_key`.

    No-op when :func:`install_rate_limiter` has already attached its own
    middleware (detected via ``app.state.rate_limiter_installed``); this
    prevents double-stacking buckets so the env limiter's stricter defaults
    can't 429 traffic the operator-configured limiter would have allowed.

    Returns the underlying limiter, or ``None`` when the call was skipped.
    """
    if getattr(app.state, "rate_limiter_installed", False):
        _log.info(
            "env_rate_limiter_skipped",
            reason="config_driven_limiter_present",
        )
        return None

    default_rpm = (
        default_per_min
        if default_per_min is not None
        else _read_per_min(ENV_DEFAULT_PER_MIN, _DEFAULT_PER_MIN_FALLBACK)
    )
    write_rpm = (
        write_per_min
        if write_per_min is not None
        else _read_per_min(ENV_WRITE_PER_MIN, _WRITE_PER_MIN_FALLBACK)
    )

    # Token-bucket math: rate is per-second, burst tracks the per-minute budget
    # so a polling client can briefly catch up after a stall without 429-ing.
    limiter = TokenBucketLimiter(
        default_rate=default_rpm / 60.0,
        default_burst=max(1, default_rpm),
        groups={
            "write": {"rate": write_rpm / 60.0, "burst": max(1, write_rpm)},
        },
    )
    # Set the sentinel so a subsequent call (or a config-driven install layered
    # on top) can detect that a limiter is already in place.
    app.state.rate_limiter = limiter
    app.state.rate_limiter_installed = True
    exempt = frozenset(exempt_paths)

    @app.middleware("http")
    async def _env_rate_limit_middleware(  # type: ignore[no-untyped-def]
        request: Request,
        call_next,
    ) -> object:
        path = request.url.path
        if path in exempt:
            return await call_next(request)

        route_class = _route_class(request.method, path)
        key = _ip_key(request)

        if limiter.check(key, group=route_class):
            return await call_next(request)

        retry = max(1, round(limiter.retry_after(key, group=route_class)))
        record_ratelimit_rejection(route_class)
        _log.warning(
            "ratelimit_rejected",
            client=key,
            method=request.method,
            path=path,
            route_class=route_class,
            retry_after=retry,
        )
        return JSONResponse(
            {"detail": "rate limit exceeded", "retry_after": retry},
            status_code=429,
            headers={"Retry-After": str(retry)},
        )

    return limiter
