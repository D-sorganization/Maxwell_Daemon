"""Generic (non-GitHub) webhook trigger logic.

This module provides the pure request-handling logic for the
``POST /api/webhooks/trigger`` endpoint.  It is intentionally separated from
the FastAPI layer so it can be unit-tested without an ASGI test client.

Security model
--------------
Callers may optionally supply an ``X-Maxwell-Signature`` header whose value
is ``sha256=<hex>``, computed as HMAC-SHA256 over the raw request body with
a shared secret configured per-daemon.  If the daemon has no
``webhook_secret`` configured the endpoint accepts unsigned requests (useful
for local or trusted-network deployments).

Idempotency
-----------
Callers may supply an ``X-Idempotency-Key`` header.  If a key is presented
and was already seen within the configurable dedup window (default 10 min),
the endpoint returns 200 with ``{"duplicate": true}`` and does **not** enqueue
a second task.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from maxwell_daemon.logging import get_logger

__all__ = [
    "WebhookTriggerPayload",
    "WebhookTriggerResult",
    "enqueue_webhook_task",
]

log = get_logger(__name__)


@dataclass
class WebhookTriggerPayload:
    """Validated inbound payload for a generic webhook trigger.

    Attributes:
        prompt: The task prompt to enqueue.
        repo: Optional repository hint (passed through to ``daemon.submit``).
        backend: Optional backend hint.
        priority: Task priority (lower = higher priority; default 100).
        idempotency_key: Caller-supplied dedup key (optional).
    """

    prompt: str
    repo: str | None = None
    backend: str | None = None
    priority: int = 100
    idempotency_key: str | None = None


@dataclass
class WebhookTriggerResult:
    """Outcome of processing one inbound webhook trigger."""

    task_id: str | None
    duplicate: bool = False
    error: str | None = None


# In-process dedup store: maps idempotency_key → expiry datetime (UTC).
_DEDUP: dict[str, datetime] = {}
_DEDUP_WINDOW: timedelta = timedelta(minutes=10)


def _is_duplicate(key: str) -> bool:
    """Return True if *key* was seen within the dedup window, pruning stale entries."""
    now = datetime.now(timezone.utc)
    # Prune expired entries to prevent unbounded growth.
    expired = [k for k, exp in _DEDUP.items() if exp < now]
    for k in expired:
        del _DEDUP[k]
    if key in _DEDUP:
        return True
    _DEDUP[key] = now + _DEDUP_WINDOW
    return False


def verify_webhook_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 verification for ``X-Maxwell-Signature``.

    Returns False for any malformed or missing input so callers can uniformly
    respond 401 without distinguishing the failure mode.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    presented = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, presented)


def enqueue_webhook_task(
    payload: WebhookTriggerPayload,
    *,
    daemon: Any,
) -> WebhookTriggerResult:
    """Validate, dedup, and enqueue a task from a generic webhook trigger.

    Args:
        payload: The validated trigger payload.
        daemon: A ``Daemon`` instance (or any object with a compatible
            ``submit(prompt, *, repo, backend, priority)`` method).

    Returns:
        :class:`WebhookTriggerResult` indicating whether the task was enqueued
        or was a duplicate.
    """
    if not payload.prompt or not payload.prompt.strip():
        return WebhookTriggerResult(task_id=None, error="prompt must not be empty")

    if payload.idempotency_key and _is_duplicate(payload.idempotency_key):
        log.info(
            "webhook trigger deduplicated: idempotency_key=%r",
            payload.idempotency_key,
        )
        return WebhookTriggerResult(task_id=None, duplicate=True)

    try:
        task = daemon.submit(
            payload.prompt,
            repo=payload.repo,
            backend=payload.backend,
            priority=payload.priority,
        )
    except Exception as exc:
        log.exception("webhook trigger submit failed")
        return WebhookTriggerResult(task_id=None, error=str(exc))

    log.info(
        "webhook trigger enqueued task=%s prompt=%r",
        task.id,
        payload.prompt[:80],
    )
    return WebhookTriggerResult(task_id=task.id)
