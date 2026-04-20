"""Rate limiter — token bucket with per-key isolation."""

from __future__ import annotations

import time

from maxwell_daemon.api.rate_limit import TokenBucket, TokenBucketLimiter


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
