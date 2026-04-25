"""Cost alert dispatcher — POSTs Slack-shaped JSON when forecast > threshold."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends import TokenUsage
from maxwell_daemon.config import BudgetConfig
from maxwell_daemon.core import BudgetEnforcer, CostLedger, CostRecord
from maxwell_daemon.core.cost_alerter import (
    AlertDispatch,
    AlertLevel,
    CostAlerter,
    format_slack_payload,
)


@pytest.fixture
def ledger(tmp_path: Path) -> CostLedger:
    return CostLedger(tmp_path / "l.db")


def _spend(ledger: CostLedger, amount: float, ts: datetime) -> None:
    ledger.record(
        CostRecord(
            ts=ts,
            backend="claude",
            model="m",
            usage=TokenUsage(total_tokens=10),
            cost_usd=amount,
        )
    )


class TestEvaluate:
    def test_no_limit_never_alerts(self, ledger: CostLedger) -> None:
        _spend(ledger, 500, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(BudgetEnforcer(BudgetConfig(), ledger), webhook_url="http://ex")
        assert alerter.evaluate(now=datetime(2026, 4, 15, tzinfo=timezone.utc)) is None

    def test_no_webhook_never_alerts(self, ledger: CostLedger) -> None:
        _spend(ledger, 900, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=1000), ledger),
            webhook_url=None,
        )
        assert alerter.evaluate(now=datetime(2026, 4, 15, tzinfo=timezone.utc)) is None

    def test_warn_when_forecast_exceeds_multiplier(self, ledger: CostLedger) -> None:
        # $50 spent by day 16 → forecast $100 against $80 limit → 1.25x → warn.
        _spend(ledger, 50.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=80.0), ledger),
            webhook_url="http://ex",
            warn_multiplier=1.1,
        )
        result = alerter.evaluate(now=datetime(2026, 4, 16, tzinfo=timezone.utc))
        assert result is not None
        assert result.level is AlertLevel.WARN

    def test_breached_when_already_over_budget(self, ledger: CostLedger) -> None:
        _spend(ledger, 150.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger),
            webhook_url="http://ex",
        )
        result = alerter.evaluate(now=datetime(2026, 4, 15, tzinfo=timezone.utc))
        assert result is not None
        assert result.level is AlertLevel.BREACHED

    def test_ok_when_forecast_under_limit(self, ledger: CostLedger) -> None:
        _spend(ledger, 10.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=100.0), ledger),
            webhook_url="http://ex",
        )
        assert alerter.evaluate(now=datetime(2026, 4, 10, tzinfo=timezone.utc)) is None


class TestDebounce:
    def test_does_not_refire_inside_window(self, ledger: CostLedger) -> None:
        _spend(ledger, 50.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=80.0), ledger),
            webhook_url="http://ex",
            warn_multiplier=1.1,
            debounce=timedelta(hours=6),
        )
        now = datetime(2026, 4, 16, tzinfo=timezone.utc)
        first = alerter.evaluate(now=now)
        assert first is not None
        second = alerter.evaluate(now=now + timedelta(hours=1))
        assert second is None

    def test_refires_after_debounce_window(self, ledger: CostLedger) -> None:
        _spend(ledger, 50.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=80.0), ledger),
            webhook_url="http://ex",
            warn_multiplier=1.1,
            debounce=timedelta(hours=6),
        )
        first = alerter.evaluate(now=datetime(2026, 4, 16, tzinfo=timezone.utc))
        assert first is not None
        later = alerter.evaluate(now=datetime(2026, 4, 16, 7, 0, tzinfo=timezone.utc))
        assert later is not None

    def test_escalation_warn_then_breach_refires(self, ledger: CostLedger) -> None:
        _spend(ledger, 50.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=80.0), ledger),
            webhook_url="http://ex",
            warn_multiplier=1.1,
            debounce=timedelta(hours=6),
        )
        warn = alerter.evaluate(now=datetime(2026, 4, 16, tzinfo=timezone.utc))
        assert warn is not None and warn.level is AlertLevel.WARN
        # Push spend past the limit.
        _spend(ledger, 50.0, datetime(2026, 4, 17, tzinfo=timezone.utc))
        breach = alerter.evaluate(now=datetime(2026, 4, 17, tzinfo=timezone.utc))
        assert breach is not None and breach.level is AlertLevel.BREACHED


class TestSendAlert:
    def test_posts_slack_shape(self, ledger: CostLedger) -> None:
        _spend(ledger, 50.0, datetime(2026, 4, 5, tzinfo=timezone.utc))
        alerter = CostAlerter(
            BudgetEnforcer(BudgetConfig(monthly_limit_usd=80.0), ledger),
            webhook_url="http://example/hook",
            warn_multiplier=1.1,
        )

        captured: list[dict[str, Any]] = []

        class _FakeClient:
            def __init__(self, *a: Any, **kw: Any) -> None:
                pass

            async def __aenter__(self) -> _FakeClient:
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

            async def post(self, url: str, *, json: Any, timeout: float) -> Any:
                captured.append({"url": url, "json": json})

                class _R:
                    status_code = 200

                    def raise_for_status(self) -> None: ...

                return _R()

        from unittest.mock import patch

        import httpx

        dispatch = alerter.evaluate(now=datetime(2026, 4, 16, tzinfo=timezone.utc))
        assert dispatch is not None
        with patch.object(httpx, "AsyncClient", _FakeClient):
            asyncio.run(alerter.send(dispatch))

        assert captured[0]["url"] == "http://example/hook"
        body = captured[0]["json"]
        assert "text" in body
        assert "Maxwell-Daemon" in body["text"]


class TestSlackPayload:
    def test_formats_warn(self) -> None:
        d = AlertDispatch(
            level=AlertLevel.WARN,
            spent_usd=50.0,
            forecast_usd=100.0,
            limit_usd=80.0,
            utilisation=0.625,
        )
        body = format_slack_payload(d)
        assert "text" in body
        assert "$100" in body["text"]
        assert "$80" in body["text"]
        # Slack-compatible: supports `attachments` with a color.
        assert body["attachments"][0]["color"] in {"warning", "good", "danger"}

    def test_formats_breach(self) -> None:
        d = AlertDispatch(
            level=AlertLevel.BREACHED,
            spent_usd=150.0,
            forecast_usd=150.0,
            limit_usd=100.0,
            utilisation=1.5,
        )
        body = format_slack_payload(d)
        assert body["attachments"][0]["color"] == "danger"
