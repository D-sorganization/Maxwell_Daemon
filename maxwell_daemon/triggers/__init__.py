"""Trigger primitives for Maxwell-Daemon.

Exposes time-based (cron) and HTTP webhook triggers that enqueue tasks
without requiring GitHub as the event source.
"""

from maxwell_daemon.triggers.cron import CronScheduler, CronTrigger
from maxwell_daemon.triggers.webhook import (
    WebhookTriggerPayload,
    WebhookTriggerResult,
    enqueue_webhook_task,
)

__all__ = [
    "CronScheduler",
    "CronTrigger",
    "WebhookTriggerPayload",
    "WebhookTriggerResult",
    "enqueue_webhook_task",
]
