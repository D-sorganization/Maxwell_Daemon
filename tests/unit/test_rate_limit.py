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


# ---------------------------------------------------------------------------
# Phase 1 (#796): env-driven per-IP middleware.
# ---------------------------------------------------------------------------


def _build_env_app(
    *,
    default_per_min: int | None = None,
    write_per_min: int | None = None,
):  # type: ignore[no-untyped-def]
    """Build a FastAPI app with the env-driven limiter installed."""
    from fastapi import FastAPI

    from maxwell_daemon.api.rate_limit import install_env_rate_limiter

    app = FastAPI()

    @app.get("/api/health")
    async def _health() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/api/version")
    async def _version() -> dict[str, str]:
        return {"v": "1"}

    @app.get("/api/status")
    async def _status() -> dict[str, str]:
        return {"state": "idle"}

    @app.post("/api/dispatch")
    async def _dispatch() -> dict[str, str]:
        return {"id": "t1"}

    @app.post("/api/control/{action}")
    async def _control(action: str) -> dict[str, str]:
        return {"action": action}

    install_env_rate_limiter(
        app,
        default_per_min=default_per_min,
        write_per_min=write_per_min,
    )
    return app


class TestEnvRateLimiter:
    def test_under_limit_allowed(self) -> None:
        from fastapi.testclient import TestClient

        app = _build_env_app(default_per_min=600, write_per_min=600)
        client = TestClient(app)
        for _ in range(10):
            assert client.get("/api/status").status_code == 200

    def test_over_limit_returns_429_with_retry_after(self) -> None:
        from fastapi.testclient import TestClient

        app = _build_env_app(default_per_min=2, write_per_min=2)
        client = TestClient(app)

        # Burst budget == 2 → first two succeed, third must be rejected.
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/status").status_code == 200

        resp = client.get("/api/status")
        assert resp.status_code == 429
        retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
        assert retry_after is not None
        assert int(retry_after) >= 1
        body = resp.json()
        assert body["detail"] == "rate limit exceeded"
        assert isinstance(body["retry_after"], int)
        assert body["retry_after"] >= 1

    def test_health_and_version_exempt(self) -> None:
        from fastapi.testclient import TestClient

        # Tiny budget; even so, exempt paths must keep returning 200 indefinitely.
        app = _build_env_app(default_per_min=1, write_per_min=1)
        client = TestClient(app)
        for _ in range(25):
            assert client.get("/api/health").status_code == 200
            assert client.get("/api/version").status_code == 200

    def test_write_bucket_strictness_independent_of_default(self) -> None:
        from fastapi.testclient import TestClient

        # Write budget 1, default 50 → /api/dispatch trips after one POST while
        # GETs continue unaffected.
        app = _build_env_app(default_per_min=50, write_per_min=1)
        client = TestClient(app)

        assert client.post("/api/dispatch").status_code == 200
        rejected = client.post("/api/dispatch")
        assert rejected.status_code == 429
        # Default bucket is unaffected.
        assert client.get("/api/status").status_code == 200

        # /api/control/* shares the write bucket too.
        ctl = client.post("/api/control/pause")
        assert ctl.status_code == 429

    def test_per_ip_isolation(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import ENV_TRUST_PROXY

        # XFF-based per-IP isolation requires the trust-proxy opt-in.
        monkeypatch.setenv(ENV_TRUST_PROXY, "1")

        app = _build_env_app(default_per_min=1, write_per_min=1)
        client = TestClient(app)

        # ip A burns its single token.
        assert client.get("/api/status", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 200
        assert client.get("/api/status", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429

        # ip B is unaffected.
        assert client.get("/api/status", headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200

    def test_env_var_overrides_threshold(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import (
            ENV_DEFAULT_PER_MIN,
            ENV_TRUST_PROXY,
            ENV_WRITE_PER_MIN,
        )

        monkeypatch.setenv(ENV_DEFAULT_PER_MIN, "3")
        monkeypatch.setenv(ENV_WRITE_PER_MIN, "1")
        # Trust proxy so XFF in the test below isolates the two pseudo-clients.
        monkeypatch.setenv(ENV_TRUST_PROXY, "1")

        # No explicit kwargs → middleware reads env vars.
        app = _build_env_app()
        client = TestClient(app)

        # Default bucket: 3 succeed, 4th is rejected.
        for _ in range(3):
            assert (
                client.get("/api/status", headers={"X-Forwarded-For": "10.0.0.7"}).status_code
                == 200
            )
        assert client.get("/api/status", headers={"X-Forwarded-For": "10.0.0.7"}).status_code == 429

        # Write bucket overridden to 1 → second dispatch is rejected.
        assert (
            client.post("/api/dispatch", headers={"X-Forwarded-For": "10.0.0.8"}).status_code == 200
        )
        assert (
            client.post("/api/dispatch", headers={"X-Forwarded-For": "10.0.0.8"}).status_code == 429
        )

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import ENV_DEFAULT_PER_MIN

        monkeypatch.setenv(ENV_DEFAULT_PER_MIN, "not-a-number")
        # Should not raise; falls back to the 120/min default.
        app = _build_env_app(write_per_min=600)
        client = TestClient(app)
        # 5 requests well under the 120/min fallback.
        for _ in range(5):
            assert client.get("/api/status").status_code == 200


class TestRouteClass:
    def test_post_dispatch_is_write(self) -> None:
        from maxwell_daemon.api.rate_limit import _route_class

        assert _route_class("POST", "/api/dispatch") == "write"

    def test_post_control_action_is_write(self) -> None:
        from maxwell_daemon.api.rate_limit import _route_class

        assert _route_class("POST", "/api/control/pause") == "write"
        assert _route_class("POST", "/api/control/resume") == "write"

    def test_get_dispatch_is_default(self) -> None:
        from maxwell_daemon.api.rate_limit import _route_class

        # Defensive: only POSTs to write-class paths get the strict bucket.
        assert _route_class("GET", "/api/dispatch") == "default"

    def test_arbitrary_post_is_default(self) -> None:
        from maxwell_daemon.api.rate_limit import _route_class

        assert _route_class("POST", "/api/tasks") == "default"


class TestIpKey:
    def test_xff_ignored_by_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """X-Forwarded-For must be ignored unless ``MAXWELL_TRUST_PROXY`` is set.

        Otherwise any direct client could mint a fresh bucket per request.
        """
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import ENV_TRUST_PROXY, _ip_key

        monkeypatch.delenv(ENV_TRUST_PROXY, raising=False)

        req = MagicMock()
        req.headers.get.side_effect = lambda name, default="": {
            "x-forwarded-for": "203.0.113.7, 10.0.0.1",
        }.get(name, default)
        req.client.host = "10.0.0.99"
        # Direct ASGI peer wins; XFF is dropped on the floor.
        assert _ip_key(req) == "ip:10.0.0.99"

    def test_xff_honored_when_trust_proxy_set(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import ENV_TRUST_PROXY, _ip_key

        monkeypatch.setenv(ENV_TRUST_PROXY, "1")

        req = MagicMock()
        req.headers.get.side_effect = lambda name, default="": {
            "x-forwarded-for": "203.0.113.7, 10.0.0.1",
        }.get(name, default)
        req.client.host = "10.0.0.99"
        # First XFF entry wins when trust-proxy is enabled.
        assert _ip_key(req) == "ip:203.0.113.7"

    def test_xff_invalid_trust_proxy_falls_back_to_default(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """A non-truthy ``MAXWELL_TRUST_PROXY`` value behaves like the default."""
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import ENV_TRUST_PROXY, _ip_key

        monkeypatch.setenv(ENV_TRUST_PROXY, "maybe")

        req = MagicMock()
        req.headers.get.side_effect = lambda name, default="": {
            "x-forwarded-for": "203.0.113.7",
        }.get(name, default)
        req.client.host = "10.0.0.99"
        assert _ip_key(req) == "ip:10.0.0.99"

    def test_falls_back_to_client_host(self) -> None:
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import _ip_key

        req = MagicMock()
        req.headers.get.side_effect = lambda name, default="": default
        req.client.host = "192.0.2.5"
        assert _ip_key(req) == "ip:192.0.2.5"

    def test_returns_unknown_when_no_client(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from unittest.mock import MagicMock

        from maxwell_daemon.api.rate_limit import ENV_TRUST_PROXY, _ip_key

        monkeypatch.delenv(ENV_TRUST_PROXY, raising=False)

        req = MagicMock()
        req.headers.get.side_effect = lambda name, default="": default
        req.client = None
        assert _ip_key(req) == "ip:unknown"


class TestBucketEviction:
    """Bound the bucket dict so attackers can't drive memory growth."""

    def test_bucket_dict_evicts_when_full(self) -> None:
        from maxwell_daemon.api.rate_limit import TokenBucketLimiter

        # Tiny cap so we can observe eviction without bursting 10k requests.
        lim = TokenBucketLimiter(default_rate=1.0, default_burst=1, max_buckets=3)

        # Fill to capacity with three distinct keys.
        for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
            lim.check(ip)
        # The internal dict carries one entry per (key, group) pair.
        assert len(lim._buckets) == 3

        # The next distinct key must trigger eviction of the oldest entry.
        lim.check("4.4.4.4")
        assert len(lim._buckets) == 3
        # The oldest key (1.1.1.1) must be gone; newest must remain.
        assert ("1.1.1.1", "default") not in lim._buckets
        assert ("4.4.4.4", "default") in lim._buckets

    def test_eviction_does_not_affect_existing_keys(self) -> None:
        """Re-touching an existing key shouldn't evict anyone."""
        from maxwell_daemon.api.rate_limit import TokenBucketLimiter

        lim = TokenBucketLimiter(default_rate=10.0, default_burst=10, max_buckets=2)
        lim.check("a")
        lim.check("b")
        # Re-touching "a" should be a hit, not an insert → nothing evicted.
        for _ in range(5):
            lim.check("a")
        assert len(lim._buckets) == 2
        assert ("a", "default") in lim._buckets
        assert ("b", "default") in lim._buckets


class TestEnvLimiterSkipsWhenConfigDriven:
    """Codex P1 #1 — env limiter must not stack on top of the config one."""

    def test_env_limiter_noop_when_config_limiter_present(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import (
            install_env_rate_limiter,
            install_rate_limiter,
        )

        app = FastAPI()

        @app.get("/api/status")
        async def _status() -> dict[str, str]:
            return {"state": "idle"}

        # Generous YAML-driven limiter. If the env limiter stacked underneath
        # with its strict 120/min default, we'd see a 429 well before request 50.
        install_rate_limiter(
            app,
            default_rate=1000.0,
            default_burst=1000,
            groups={},
        )
        middleware_after_config = len(app.user_middleware)

        # The env limiter call should be a no-op and return None.
        result = install_env_rate_limiter(app)
        assert result is None
        # And it must NOT have registered a second middleware.
        assert len(app.user_middleware) == middleware_after_config

        # Behavior check: bursting under the config-driven budget all succeeds.
        client = TestClient(app)
        for _ in range(50):
            assert client.get("/api/status").status_code == 200

    def test_env_limiter_installs_when_no_config_limiter(self) -> None:
        """Sanity check: if the config-driven path didn't run, the env one engages."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from maxwell_daemon.api.rate_limit import install_env_rate_limiter

        app = FastAPI()

        @app.get("/api/status")
        async def _status() -> dict[str, str]:
            return {"state": "idle"}

        limiter = install_env_rate_limiter(app, default_per_min=1, write_per_min=1)
        assert limiter is not None

        client = TestClient(app)
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/status").status_code == 429
