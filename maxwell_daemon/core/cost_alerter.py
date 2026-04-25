"""Cost alert dispatcher — evaluates budget forecasts and POSTs to a webhook.

Orthogonal to ``BudgetEnforcer`` (which decides whether to refuse work) — the
alerter is purely about *telling humans* something has changed, with debouncing
so we don't spam the Slack channel every polling tick.

Payload shape is Slack-compatible, but any incoming webhook that accepts
``{text, attachments}`` will render it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx

from maxwell_daemon.core.budget import BudgetEnforcer

__all__ = [
    "AlertDispatch",
    "AlertLevel",
    "CostAlerter",
    "format_slack_payload",
]


class AlertLevel(Enum):
    WARN = "warn"
    BREACHED = "breached"


@dataclass(slots=True, frozen=True)
class AlertDispatch:
    level: AlertLevel
    spent_usd: float
    forecast_usd: float
    limit_usd: float
    utilisation: float


class CostAlerter:
    def __init__(
        self,
        enforcer: BudgetEnforcer,
        *,
        webhook_url: str | None,
        warn_multiplier: float = 1.1,
        debounce: timedelta = timedelta(hours=6),
    ) -> None:
        self._enforcer = enforcer
        self._webhook_url = webhook_url
        self._warn_multiplier = warn_multiplier
        self._debounce = debounce
        self._last_fired: dict[AlertLevel, datetime] = {}

    def evaluate(self, *, now: datetime | None = None) -> AlertDispatch | None:
        if self._webhook_url is None:
            return None
        now = now or datetime.now(timezone.utc)
        check = self._enforcer.check(now=now)
        if check.limit_usd is None or check.forecast_usd is None:
            return None

        level: AlertLevel | None
        if check.status == "exceeded" or check.spent_usd >= check.limit_usd:
            level = AlertLevel.BREACHED
        elif check.forecast_usd >= check.limit_usd * self._warn_multiplier:
            level = AlertLevel.WARN
        else:
            level = None

        if level is None:
            return None

        # Debounce: don't re-fire the same level inside the window. But
        # escalating (warn → breached) always fires so humans see the step-up.
        last = self._last_fired.get(level)
        if last is not None and now - last < self._debounce:
            return None

        self._last_fired[level] = now
        return AlertDispatch(
            level=level,
            spent_usd=check.spent_usd,
            forecast_usd=check.forecast_usd,
            limit_usd=check.limit_usd,
            utilisation=check.utilisation,
        )

    async def send(self, dispatch: AlertDispatch) -> None:
        if self._webhook_url is None:
            raise ValueError(
                "CostAlerter.send() called without a webhook_url configured"
            )
        payload = format_slack_payload(dispatch)
        async with httpx.AsyncClient() as client:
            r = await client.post(self._webhook_url, json=payload, timeout=10.0)
            r.raise_for_status()


def format_slack_payload(dispatch: AlertDispatch) -> dict[str, Any]:
    colour = {"warn": "warning", "breached": "danger"}[dispatch.level.value]
    verb = {
        AlertLevel.WARN: "on track to exceed",
        AlertLevel.BREACHED: "has exceeded",
    }[dispatch.level]
    text = (
        f"Maxwell-Daemon budget {verb} its monthly limit: "
        f"forecast ${dispatch.forecast_usd:.2f} vs limit ${dispatch.limit_usd:.2f} "
        f"(spent ${dispatch.spent_usd:.2f}, {dispatch.utilisation:.0%})"
    )
    return {
        "text": text,
        "attachments": [
            {
                "color": colour,
                "fields": [
                    {
                        "title": "Month-to-date",
                        "value": f"${dispatch.spent_usd:.2f}",
                        "short": True,
                    },
                    {
                        "title": "Forecast",
                        "value": f"${dispatch.forecast_usd:.2f}",
                        "short": True,
                    },
                    {
                        "title": "Limit",
                        "value": f"${dispatch.limit_usd:.2f}",
                        "short": True,
                    },
                    {
                        "title": "Utilisation",
                        "value": f"{dispatch.utilisation:.1%}",
                        "short": True,
                    },
                ],
            }
        ],
    }
