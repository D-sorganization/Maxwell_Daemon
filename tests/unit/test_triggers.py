"""Tests for trigger execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from maxwell_daemon.triggers.cron import _matches
from maxwell_daemon.triggers.webhook import (
    WebhookTriggerPayload,
    enqueue_webhook_task,
    verify_webhook_signature,
)


def test_cron_matches() -> None:
    dt = datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc)
    # 0 9 * * * means 9:00 AM every day
    assert _matches(dt, "0 9 * * *") is True
    # 1 9 * * * means 9:01 AM every day
    assert _matches(dt, "1 9 * * *") is False
    # * * * * * means every minute
    assert _matches(dt, "* * * * *") is True


def test_webhook_trigger_enqueue(mocker: Any) -> None:
    payload = WebhookTriggerPayload(prompt="hello")
    daemon_mock = mocker.Mock()
    task_mock = mocker.Mock()
    task_mock.id = "task-1"
    daemon_mock.submit.return_value = task_mock

    res = enqueue_webhook_task(payload, daemon=daemon_mock)
    assert res.task_id == "task-1"
    assert res.error is None
    assert res.duplicate is False
    daemon_mock.submit.assert_called_once_with("hello", repo=None, backend=None, priority=100)


def test_webhook_trigger_empty_prompt(mocker: Any) -> None:
    payload = WebhookTriggerPayload(prompt="   ")
    res = enqueue_webhook_task(payload, daemon=mocker.Mock())
    assert res.task_id is None
    assert res.error is not None and "prompt must not be empty" in res.error


def test_webhook_trigger_dedup(mocker: Any) -> None:
    payload1 = WebhookTriggerPayload(prompt="hello", idempotency_key="key1")
    payload2 = WebhookTriggerPayload(prompt="hello again", idempotency_key="key1")
    daemon_mock = mocker.Mock()

    res1 = enqueue_webhook_task(payload1, daemon=daemon_mock)
    assert res1.duplicate is False

    res2 = enqueue_webhook_task(payload2, daemon=daemon_mock)
    assert res2.duplicate is True
    assert res2.task_id is None


def test_verify_webhook_signature() -> None:
    # missing prefix
    assert verify_webhook_signature("secret", b"body", "signature") is False
    # correct signature
    import hashlib
    import hmac

    expected = hmac.new(b"secret", b"body", hashlib.sha256).hexdigest()
    assert verify_webhook_signature("secret", b"body", f"sha256={expected}") is True
