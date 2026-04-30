"""Unit tests for the pure-logic ``RetryPolicy`` helper (issue #798, phase 1)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from maxwell_daemon.daemon.retry_policy import (
    DEFAULT_RETRY_POLICY,
    RetryPolicy,
)
from maxwell_daemon.daemon.runner import TaskStatus


def _task(*, status: TaskStatus = TaskStatus.FAILED, retry_count: int = 0) -> SimpleNamespace:
    """Build a task-shaped object with just the attributes the policy reads."""
    return SimpleNamespace(status=status, retry_count=retry_count)


class TestShouldRetry:
    def test_returns_true_until_max_retries_reached(self) -> None:
        policy = RetryPolicy(max_retries=3, base_delay_seconds=1.0, max_delay_seconds=10.0)
        assert policy.should_retry(_task(retry_count=0)) is True
        assert policy.should_retry(_task(retry_count=1)) is True
        assert policy.should_retry(_task(retry_count=2)) is True
        assert policy.should_retry(_task(retry_count=3)) is False
        assert policy.should_retry(_task(retry_count=4)) is False

    def test_zero_max_retries_never_retries(self) -> None:
        policy = RetryPolicy(max_retries=0, base_delay_seconds=1.0, max_delay_seconds=10.0)
        assert policy.should_retry(_task(retry_count=0)) is False

    def test_terminal_statuses_never_retry(self) -> None:
        policy = RetryPolicy(max_retries=5, base_delay_seconds=1.0, max_delay_seconds=10.0)
        assert policy.should_retry(_task(status=TaskStatus.COMPLETED)) is False
        assert policy.should_retry(_task(status=TaskStatus.CANCELLED)) is False
        assert policy.should_retry(_task(status=TaskStatus.FAILED)) is True

    def test_missing_retry_count_attribute_treated_as_zero(self) -> None:
        policy = RetryPolicy(max_retries=2, base_delay_seconds=1.0, max_delay_seconds=10.0)
        bare = SimpleNamespace(status=TaskStatus.FAILED)
        assert policy.should_retry(bare) is True


class TestNextRetryDelay:
    def test_grows_with_retry_count(self) -> None:
        policy = RetryPolicy(max_retries=10, base_delay_seconds=1.0, max_delay_seconds=10_000.0)
        # Run several samples per step so a single jitter draw can't fool us.
        samples_per_step = 25
        prev_max = 0.0
        for retry_count in range(0, 6):
            seen = [
                policy.next_retry_delay(retry_count).total_seconds()
                for _ in range(samples_per_step)
            ]
            current_min = min(seen)
            assert current_min >= prev_max * 0.95, (
                f"retry_count={retry_count} produced {current_min}s, "
                f"which is not greater than the previous step's max {prev_max}s"
            )
            prev_max = max(seen)

    def test_delay_is_capped_at_max_delay_seconds(self) -> None:
        max_delay = 30.0
        jitter_ceiling = max_delay * 1.1  # 10% jitter ratio
        policy = RetryPolicy(
            max_retries=99,
            base_delay_seconds=1.0,
            max_delay_seconds=max_delay,
        )
        # A high retry_count would produce 2 ** 20 seconds without the cap;
        # the cap must hold even after jitter is applied.
        for _ in range(200):
            delay = policy.next_retry_delay(20).total_seconds()
            assert delay <= jitter_ceiling, (
                f"delay {delay}s exceeded the cap+jitter ceiling {jitter_ceiling}s"
            )

    def test_jitter_stays_bounded(self) -> None:
        # With base_delay=100 and retry_count=0, raw=100 and post-jitter must
        # land in [90, 110] for every draw.
        policy = RetryPolicy(
            max_retries=99,
            base_delay_seconds=100.0,
            max_delay_seconds=100.0,
        )
        observed_min = float("inf")
        observed_max = 0.0
        for _ in range(500):
            delay = policy.next_retry_delay(0).total_seconds()
            assert 90.0 <= delay <= 110.0, f"jitter out of bounds: {delay}s"
            observed_min = min(observed_min, delay)
            observed_max = max(observed_max, delay)
        # Sanity: the jitter actually moved the value (not stuck at the mean).
        # The probability of 500 draws all being identical is vanishingly small.
        assert observed_min < observed_max

    def test_returns_timedelta(self) -> None:
        policy = RetryPolicy(max_retries=3, base_delay_seconds=1.0, max_delay_seconds=10.0)
        assert isinstance(policy.next_retry_delay(0), timedelta)

    def test_negative_retry_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            DEFAULT_RETRY_POLICY.next_retry_delay(-1)


class TestQueueSaturationBackoff:
    def test_default_policy_matches_legacy_value(self) -> None:
        # The pre-extraction code hard-coded 60 seconds at three call sites;
        # the default policy must preserve that exact value to keep behavior
        # identical for clients depending on the QueueSaturationError contract.
        assert DEFAULT_RETRY_POLICY.queue_saturation_backoff() == 60

    def test_custom_base_delay_round_trips(self) -> None:
        assert RetryPolicy(base_delay_seconds=15.0).queue_saturation_backoff() == 15
        assert RetryPolicy(base_delay_seconds=15.4).queue_saturation_backoff() == 15
        assert RetryPolicy(base_delay_seconds=15.6).queue_saturation_backoff() == 16


class TestConfigValidation:
    def test_max_retries_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(max_retries=-1)

    def test_base_delay_must_be_non_negative(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(base_delay_seconds=-1.0)

    def test_max_delay_must_be_at_least_base_delay(self) -> None:
        with pytest.raises(ValueError):
            RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=5.0)
