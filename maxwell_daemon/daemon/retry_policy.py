"""Pure retry/backoff policy used by the daemon's task lifecycle.

Phase 1 of the runner.py decomposition (issue #798). This module deliberately
contains no I/O, no async, and no logging — only the math required to decide
*whether* to retry a task and *how long* to wait before the next attempt.
That makes it trivially unit-testable and lets follow-up phases route every
retry/backoff site through one collaborator without behavior drift.

The current daemon does not yet thread a ``retry_count`` through the
``Task`` model; ``next_retry_delay`` is therefore introduced as a pure
helper in anticipation of the wider extraction. The existing call sites
(``QueueSaturationError`` raise points) consume the retry delay only at
``retry_count == 0`` via :meth:`RetryPolicy.queue_saturation_backoff`,
which preserves the previously hard-coded 60-second value.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

# ``Task`` lives in ``maxwell_daemon.daemon.runner``, which imports
# ``DEFAULT_RETRY_POLICY`` from this module at import time. Importing
# ``Task`` here — even guarded by ``TYPE_CHECKING`` — is enough for static
# analyzers (CodeQL) to flag a cyclic-import risk. ``should_retry`` only
# touches duck-typed attributes, so the parameter is annotated as ``Any``
# and the terminal-status check uses string values from ``TaskStatus``
# (which subclasses ``str, Enum``) without importing the enum itself.
_TERMINAL_STATUS_VALUES = frozenset({"completed", "cancelled"})


# Default backoff parameters chosen to preserve the previously hard-coded
# 60-second queue-saturation backoff: ``base_delay`` * 2 ** 0 == 60.
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY_SECONDS = 60.0
_DEFAULT_MAX_DELAY_SECONDS = 600.0
# Bounded multiplicative jitter in [1 - JITTER_RATIO, 1 + JITTER_RATIO].
_JITTER_RATIO = 0.1


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Pure configuration + math for task retry decisions.

    Attributes:
        max_retries: Inclusive cap on the number of retry attempts.
            ``should_retry`` returns ``False`` once a task's recorded
            attempt count reaches this value.
        base_delay_seconds: Backoff at ``retry_count == 0`` (before jitter).
            Doubled for each subsequent retry.
        max_delay_seconds: Hard cap on the post-exponential delay
            *before* jitter is applied. The final returned delay may
            therefore be at most ``max_delay_seconds * (1 + JITTER_RATIO)``.
    """

    max_retries: int = _DEFAULT_MAX_RETRIES
    base_delay_seconds: float = _DEFAULT_BASE_DELAY_SECONDS
    max_delay_seconds: float = _DEFAULT_MAX_DELAY_SECONDS

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(
                f"max_retries must be >= 0, got {self.max_retries}",
            )
        if self.base_delay_seconds < 0:
            raise ValueError(
                f"base_delay_seconds must be >= 0, got {self.base_delay_seconds}",
            )
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                "max_delay_seconds must be >= base_delay_seconds; "
                f"got max={self.max_delay_seconds} base={self.base_delay_seconds}",
            )

    def should_retry(self, task: Any) -> bool:
        """Return ``True`` while the task has retry budget remaining.

        Counts attempts via ``getattr(task, "retry_count", 0)`` so this is
        safe to call against the current ``Task`` dataclass — which does
        not yet carry an explicit retry counter — and against a future
        version that does. Tasks already terminal (``COMPLETED``,
        ``CANCELLED``) never qualify for retry.

        ``task`` is annotated as ``Any`` to avoid an import cycle with
        :mod:`maxwell_daemon.daemon.runner`; the terminal-status check
        uses the underlying string values of ``TaskStatus`` (a
        ``str, Enum``) and accepts either the enum member or its raw
        string form.
        """
        status = getattr(task, "status", None)
        # ``TaskStatus`` is ``str, Enum``, so equality with a string works
        # without importing the enum and breaking the cycle.
        status_value = getattr(status, "value", status)
        if isinstance(status_value, str) and status_value.lower() in _TERMINAL_STATUS_VALUES:
            return False
        attempts = int(getattr(task, "retry_count", 0))
        return attempts < self.max_retries

    def next_retry_delay(self, retry_count: int) -> timedelta:
        """Compute the wait before the next attempt with bounded jitter.

        The schedule is exponential — ``base_delay_seconds * 2 ** retry_count`` —
        capped at ``max_delay_seconds`` *before* a small symmetric jitter is
        applied to avoid retry storms. Jitter draws from
        ``secrets.randbelow`` so the policy is deterministic-modulo-randomness
        and uses a CSPRNG (no extra dependency).
        """
        if retry_count < 0:
            raise ValueError(f"retry_count must be >= 0, got {retry_count}")
        # Clamp the exponent so ``2 ** retry_count`` never builds a
        # pathologically large int for absurd inputs. 64 is a generous
        # ceiling: ``base_delay_seconds * 2 ** 64`` ~= 1.8e19 * base, which
        # exceeds any realistic ``max_delay_seconds``, so the
        # ``min(raw, max_delay_seconds)`` below still binds first and the
        # documented "exponential then cap" behavior holds even for large
        # retry counts.
        capped_exp = min(retry_count, 64)
        raw = self.base_delay_seconds * (2**capped_exp)
        capped = min(raw, self.max_delay_seconds)
        # Multiplicative jitter in [1 - _JITTER_RATIO, 1 + _JITTER_RATIO].
        jitter_unit = secrets.randbelow(1_000_001) / 1_000_000  # [0.0, 1.0]
        jitter_multiplier = 1.0 + (jitter_unit * 2.0 - 1.0) * _JITTER_RATIO
        delayed = capped * jitter_multiplier
        # Always non-negative; the policy never returns a negative timedelta.
        return timedelta(seconds=max(delayed, 0.0))

    def queue_saturation_backoff(self) -> int:
        """Backoff (in seconds) advertised when the priority queue is full.

        Returned as ``int`` because :class:`QueueSaturationError` exposes the
        value as ``backoff_seconds: int`` over the HTTP contract. Computed
        from ``base_delay_seconds`` to keep one source of truth, and rounded
        deterministically (no jitter) so the public retry hint is stable.
        """
        return round(self.base_delay_seconds)


# Module-level default used at the existing call sites. Wrapping the dataclass
# in a constant keeps the runner's import surface tiny (one symbol) without
# forcing every caller to repeat the construction.
DEFAULT_RETRY_POLICY = RetryPolicy()
