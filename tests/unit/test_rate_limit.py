"""Rate limiter — token bucket with per-key isolation.

Also covers the phase-1 sliding-window dependency that protects
``POST /api/dispatch`` (issue #796).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from maxwell_daemon.api.rate_limit import (
    InMemoryRateLimitStore,
    RateLimitPolicy,
    TokenBucket,
    TokenBucketLimiter,
    build_rate_limit_dependency,
    extract_client_id,
    install_rate_limit_headers_middleware,
)


class TestTokenBucket:
    def test_initial_capacity(self) -> None:
        b = TokenBucket(capacity=5, refill_per_second=1.0)
        assert b.try_consume() is True
        # Burst of 5 available.
        for _ in range(4):
            assert b.try_consume() is True
        assert b.try_consume() is False

    def test_refills_over_time(self) -> None:
        b = TokenBucket(capacity=2, refill_per_second=10.0)
        b.try_consume()
        b.try_consume()
        assert b.try_consume() is False
        time.sleep(0.15)  # ~1.5 tokens refilled
        assert b.try_consume() is True

    def test_refill_cap_at_capacity(self) -> None:
        b = TokenBucket(capacity=3, refill_per_second=100.0)
        time.sleep(0.1)  # would refill 10 tokens but capped at 3
        for _ in range(3):
            assert b.try_consume() is True
        assert b.try_consume() is False

    def test_retry_after_reports_wait_time(self) -> None:
        b = TokenBucket(capacity=1, refill_per_second=2.0)
        b.try_consume()
        retry = b.retry_after_seconds()
        assert 0 < retry <= 0.5


class TestLimiter:
    def test_per_key_isolation(self) -> None:
        lim = TokenBucketLimiter(default_rate=1.0, default_burst=1)
        assert lim.check("ip1") is True
        assert lim.check("ip1") is False  # ip1 used its token
        assert lim.check("ip2") is True  # ip2 still has its own

    def test_group_overrides(self) -> None:
        lim = TokenBucketLimiter(
            default_rate=1.0,
            default_burst=1,
            groups={"writes": {"rate": 0.5, "burst": 2}},
        )
        # writes group has burst of 2
        assert lim.check("ip1", group="writes") is True
        assert lim.check("ip1", group="writes") is True
        assert lim.check("ip1", group="writes") is False

    def test_unknown_group_uses_default(self) -> None:
        lim = TokenBucketLimiter(default_rate=1.0, default_burst=1)
        assert lim.check("ip1", group="nonexistent") is True
        assert lim.check("ip1", group="nonexistent") is False

    def test_reports_retry_after(self) -> None:
        lim = TokenBucketLimiter(default_rate=2.0, default_burst=1)
        lim.check("ip1")
        assert lim.check("ip1") is False
        retry = lim.retry_after("ip1")
        assert 0 < retry <= 1.0


class TestMiddleware:
    def test_permits_under_limit(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import install_rate_limiter

        app = FastAPI()
        install_rate_limiter(app, default_rate=100.0, default_burst=10, groups={})

        @app.get("/ping")
        async def _ping() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        for _ in range(5):
            assert client.get("/ping").status_code == 200

    def test_blocks_over_limit_with_429(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import install_rate_limiter

        app = FastAPI()
        install_rate_limiter(app, default_rate=1.0, default_burst=2, groups={})

        @app.get("/p")
        async def _p() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        # Spend the burst budget…
        for _ in range(2):
            assert client.get("/p").status_code == 200
        # …then the next one must be rate-limited.
        r = client.get("/p")
        assert r.status_code == 429
        assert "retry-after" in {k.lower() for k in r.headers}

    def test_exempt_paths_not_limited(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import install_rate_limiter

        app = FastAPI()
        install_rate_limiter(
            app,
            default_rate=0.1,
            default_burst=1,
            groups={},
            exempt_paths={"/health"},
        )

        @app.get("/health")
        async def _h() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        for _ in range(20):
            assert client.get("/health").status_code == 200


class TestRetryAfterFull:
    def test_retry_after_returns_zero_when_tokens_available(self) -> None:
        from maxwell_daemon.api.rate_limit import TokenBucket

        b = TokenBucket(capacity=5, refill_per_second=1.0)
        assert b.retry_after_seconds() == 0.0


class TestClassify:
    def test_writes_classified_for_post(self) -> None:
        from maxwell_daemon.api.rate_limit import _classify

        assert _classify("POST", "/api/tasks") == "writes"
        assert _classify("DELETE", "/api/tasks/1") == "writes"

    def test_gets_classified_as_default(self) -> None:
        from maxwell_daemon.api.rate_limit import _classify

        assert _classify("GET", "/api/tasks") == "default"


class TestClientKey:
    def test_bearer_token_hashed(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import _client_key

        req = MagicMock()
        req.headers.get.return_value = "Bearer mysecrettoken"
        key = _client_key(req)
        assert key.startswith("tok:")
        assert "mysecrettoken" not in key

    def test_no_bearer_uses_ip(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import _client_key

        req = MagicMock()
        req.headers.get.return_value = ""
        req.client.host = "1.2.3.4"
        key = _client_key(req)
        assert key == "ip:1.2.3.4"


# ─────────────────────────────────────────────────────────────────────────────
# Phase-1 sliding-window dependency tests (issue #796).
# ─────────────────────────────────────────────────────────────────────────────


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic window tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestRateLimitPolicy:
    def test_rejects_zero_limit(self) -> None:
        with pytest.raises(ValueError):
            RateLimitPolicy(limit=0, window_seconds=10)

    def test_rejects_zero_window(self) -> None:
        with pytest.raises(ValueError):
            RateLimitPolicy(limit=1, window_seconds=0)


class TestInMemoryRateLimitStore:
    @pytest.mark.asyncio
    async def test_allows_up_to_limit(self) -> None:
        store = InMemoryRateLimitStore()
        policy = RateLimitPolicy(limit=3, window_seconds=60)
        for expected_remaining in (2, 1, 0):
            r = await store.hit("k1", policy)
            assert r.allowed is True
            assert r.limit == 3
            assert r.remaining == expected_remaining
            assert r.retry_after_seconds == 0

    @pytest.mark.asyncio
    async def test_blocks_over_limit(self) -> None:
        store = InMemoryRateLimitStore()
        policy = RateLimitPolicy(limit=2, window_seconds=60)
        await store.hit("k1", policy)
        await store.hit("k1", policy)
        r = await store.hit("k1", policy)
        assert r.allowed is False
        assert r.remaining == 0
        assert r.retry_after_seconds >= 1

    @pytest.mark.asyncio
    async def test_per_key_isolation(self) -> None:
        store = InMemoryRateLimitStore()
        policy = RateLimitPolicy(limit=1, window_seconds=60)
        await store.hit("k1", policy)
        # k1 over its limit, k2 still has its full budget.
        r1 = await store.hit("k1", policy)
        r2 = await store.hit("k2", policy)
        assert r1.allowed is False
        assert r2.allowed is True

    @pytest.mark.asyncio
    async def test_window_resets_after_expiry(self) -> None:
        clock = _FakeClock()
        store = InMemoryRateLimitStore(monotonic=clock)
        policy = RateLimitPolicy(limit=2, window_seconds=10)
        await store.hit("k1", policy)
        await store.hit("k1", policy)
        # Third hit before window rollover is denied.
        denied = await store.hit("k1", policy)
        assert denied.allowed is False
        # Advance past the window — old hits roll off and we get fresh budget.
        clock.advance(11)
        allowed = await store.hit("k1", policy)
        assert allowed.allowed is True
        assert allowed.remaining == policy.limit - 1


class TestExtractClientId:
    def test_prefers_jwt_sub(self) -> None:
        req = MagicMock()
        req.state.jwt_sub = "alice"
        req.headers.get.return_value = ""
        assert extract_client_id(req) == "user:alice"

    def test_unverified_bearer_token_is_ignored(self) -> None:
        """An ``Authorization: Bearer`` header that no auth middleware has
        validated must NOT influence the bucket key — otherwise a caller
        can rotate fake tokens to bypass throttling. Falls through to IP."""
        req = MagicMock()
        # No verified identity attached to state.
        req.state = MagicMock(spec=[])
        req.headers.get.side_effect = lambda name, default="": {
            "authorization": "Bearer s3cret",
        }.get(name, default)
        req.client.host = "10.0.0.5"
        cid = extract_client_id(req)
        assert cid == "ip:10.0.0.5"
        assert "tok:" not in cid
        assert "s3cret" not in cid

    def test_uses_verified_static_token_id(self) -> None:
        req = MagicMock()
        req.state = MagicMock(spec=["auth_token_id"])
        req.state.auth_token_id = "ops-bot"
        req.headers.get.return_value = ""
        assert extract_client_id(req) == "token:ops-bot"

    def test_falls_back_to_forwarded_for(self) -> None:
        req = MagicMock()
        req.state = MagicMock(spec=[])

        def _hget(name: str, default: str = "") -> str:
            return {
                "authorization": "",
                "x-forwarded-for": "203.0.113.5, 10.0.0.1",
            }.get(name, default)

        req.headers.get.side_effect = _hget
        assert extract_client_id(req) == "ip:203.0.113.5"

    def test_falls_back_to_client_host(self) -> None:
        req = MagicMock()
        req.state = MagicMock(spec=[])
        req.headers.get.return_value = ""
        req.client.host = "192.0.2.7"
        assert extract_client_id(req) == "ip:192.0.2.7"


def _make_app(*, limit: int = 3, window: float = 60.0) -> tuple[FastAPI, InMemoryRateLimitStore]:
    """Helper: spin up a tiny app guarded by the dispatch dependency."""
    app = FastAPI()
    install_rate_limit_headers_middleware(app)
    store = InMemoryRateLimitStore()
    policy = RateLimitPolicy(limit=limit, window_seconds=window)
    dep = build_rate_limit_dependency(endpoint="dispatch", policy=policy, store=store)

    @app.post("/api/dispatch", dependencies=[Depends(dep)])
    async def _dispatch() -> dict[str, str]:
        return {"ok": "yes"}

    return app, store


class TestDispatchDependency:
    def test_limit_enforced_and_429_returned(self) -> None:
        app, _store = _make_app(limit=2, window=60.0)
        client = TestClient(app)
        # Burst budget…
        for _ in range(2):
            r = client.post("/api/dispatch", json={})
            assert r.status_code == 200
        # …then the next one is rate-limited.
        r = client.post("/api/dispatch", json={})
        assert r.status_code == 429
        # Standard headers must be present on the 429.
        assert r.headers.get("Retry-After")
        assert int(r.headers["Retry-After"]) >= 1
        assert r.headers.get("RateLimit-Limit") == "2"
        assert r.headers.get("RateLimit-Remaining") == "0"
        assert "RateLimit-Reset" in r.headers

    def test_success_responses_carry_headers(self) -> None:
        app, _store = _make_app(limit=5, window=30.0)
        client = TestClient(app)
        r = client.post("/api/dispatch", json={})
        assert r.status_code == 200
        assert r.headers.get("RateLimit-Limit") == "5"
        # Remaining decrements with each call.
        assert r.headers.get("RateLimit-Remaining") == "4"
        assert "RateLimit-Reset" in r.headers
        r2 = client.post("/api/dispatch", json={})
        assert r2.headers.get("RateLimit-Remaining") == "3"

    def test_window_reset_restores_budget(self) -> None:
        clock = _FakeClock()
        app = FastAPI()
        install_rate_limit_headers_middleware(app)
        store = InMemoryRateLimitStore(monotonic=clock)
        policy = RateLimitPolicy(limit=1, window_seconds=5)
        dep = build_rate_limit_dependency(endpoint="dispatch", policy=policy, store=store)

        @app.post("/api/dispatch", dependencies=[Depends(dep)])
        async def _dispatch() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        assert client.post("/api/dispatch", json={}).status_code == 200
        assert client.post("/api/dispatch", json={}).status_code == 429
        # After the window expires the budget is restored.
        clock.advance(6)
        assert client.post("/api/dispatch", json={}).status_code == 200

    def test_per_client_isolation_via_forwarded_for(self) -> None:
        app, _store = _make_app(limit=1, window=60.0)
        client = TestClient(app)
        # Client A burns its budget…
        r1 = client.post("/api/dispatch", json={}, headers={"X-Forwarded-For": "10.0.0.1"})
        assert r1.status_code == 200
        r1b = client.post("/api/dispatch", json={}, headers={"X-Forwarded-For": "10.0.0.1"})
        assert r1b.status_code == 429
        # …Client B is unaffected.
        r2 = client.post("/api/dispatch", json={}, headers={"X-Forwarded-For": "10.0.0.2"})
        assert r2.status_code == 200


class TestDisabledByDefault:
    """When the config flag is disabled the dispatch endpoint must be unmetered.

    We exercise this through the *config* layer to lock the no-op path that the
    server wires up when ``api.dispatch_rate_limit.enabled`` is False.
    """

    def test_dispatch_rate_limit_disabled_by_default(self) -> None:
        from maxwell_daemon.config.models import APIConfig, DispatchRateLimitConfig

        api_cfg = APIConfig()
        # Default-constructed APIConfig must not enable the limiter.
        assert isinstance(api_cfg.dispatch_rate_limit, DispatchRateLimitConfig)
        assert api_cfg.dispatch_rate_limit.enabled is False

    def test_disabled_dependency_is_noop_under_load(self) -> None:
        # Build a stand-in for what server.py does when the flag is off:
        # an async no-op dependency. We then call the endpoint many more times
        # than the would-be limit and prove no 429s appear.
        async def _noop() -> None:
            return None

        app = FastAPI()

        @app.post("/api/dispatch", dependencies=[Depends(_noop)])
        async def _dispatch() -> dict[str, str]:
            return {"ok": "yes"}

        client = TestClient(app)
        # 50 calls, no limit configured anywhere.
        for _ in range(50):
            r = client.post("/api/dispatch", json={})
            assert r.status_code == 200
            assert "Retry-After" not in r.headers
            assert "RateLimit-Limit" not in r.headers


class TestPrometheusCounterRegistered:
    def test_rate_limit_exceeded_counter_exists(self) -> None:
        from maxwell_daemon.metrics import RATE_LIMIT_EXCEEDED_TOTAL

        # Labelled counters expose a ``.labels(...)`` factory; calling it must
        # not raise and the resulting child must be incrementable.
        child = RATE_LIMIT_EXCEEDED_TOTAL.labels(endpoint="dispatch")
        before = child._value.get()
        child.inc()
        after = child._value.get()
        assert after == before + 1
