from __future__ import annotations

class TestTokenBucketEdgeCases:
    def test_try_consume_multi(self) -> None:
        from maxwell_daemon.api.rate_limit import TokenBucket
        b = TokenBucket(capacity=5, refill_per_second=1.0)
        assert b.try_consume(amount=3.0) is True
        assert b.try_consume(amount=3.0) is False

    def test_retry_after_zero_refill(self) -> None:
        from maxwell_daemon.api.rate_limit import TokenBucket
        b = TokenBucket(capacity=1, refill_per_second=0.0)
        b.try_consume()
        assert b.retry_after_seconds() == 1.0

class TestLimiterEdgeCases:
    def test_init_with_none_groups(self) -> None:
        from maxwell_daemon.api.rate_limit import TokenBucketLimiter
        lim = TokenBucketLimiter(default_rate=1.0, default_burst=1, groups=None)
        assert lim.check("ip") is True

class TestClientKeyEdgeCases:
    def test_missing_client(self) -> None:
        from unittest.mock import MagicMock
        from maxwell_daemon.api.rate_limit import _client_key
        req = MagicMock()
        req.headers.get.return_value = ""
        req.client = None
        assert _client_key(req) == "ip:unknown"
