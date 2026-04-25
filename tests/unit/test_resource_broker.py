"""Subscription-aware resource broker decision model."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from maxwell_daemon.core.resource_broker import (
    CapabilityProfile,
    QuotaSnapshot,
    ResourceAccount,
    ResourceBroker,
    RoutingPolicy,
)

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)


def _account(
    provider_id: str,
    *,
    disabled: bool = False,
    monthly_budget_usd: float | None = 50.0,
    secret_token: str | None = None,
) -> ResourceAccount:
    return ResourceAccount(
        provider_id=provider_id,
        display_name=provider_id.title(),
        integration_kind="api",
        auth_status="configured",
        terms_mode="official",
        monthly_budget_usd=monthly_budget_usd,
        disabled=disabled,
        secret_token=secret_token,
    )


def _quota(
    provider_id: str,
    *,
    available_quota: float | None = 100.0,
    spent_usd_month_to_date: float = 0.0,
    confidence: float = 1.0,
) -> QuotaSnapshot:
    return QuotaSnapshot(
        provider_id=provider_id,
        captured_at=NOW,
        available_quota=available_quota,
        confidence=confidence,
        source="user",
        spent_usd_month_to_date=spent_usd_month_to_date,
    )


def _profile(
    provider_id: str,
    backend_id: str,
    tags: set[str],
    *,
    cost: float = 1.0,
    latency_ms: int = 100,
) -> CapabilityProfile:
    return CapabilityProfile(
        provider_id=provider_id,
        backend_id=backend_id,
        capability_tags=tags,
        max_context_tokens=32_000,
        estimated_cost_usd=cost,
        latency_ms=latency_ms,
        concurrency_limit=1,
    )


class TestResourceModels:
    def test_resource_requires_stable_id_and_routable_state(self) -> None:
        with pytest.raises(ValueError, match="provider_id"):
            _account("")
        with pytest.raises(ValueError, match="monthly_budget_usd"):
            _account("openai", monthly_budget_usd=-1.0)

    def test_quota_requires_source_and_confidence(self) -> None:
        with pytest.raises(ValueError, match="source"):
            QuotaSnapshot(
                provider_id="openai",
                captured_at=NOW,
                available_quota=1.0,
                confidence=0.5,
                source="",  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="confidence"):
            _quota("openai", confidence=1.5)

    def test_capability_profile_requires_tags_and_positive_limits(self) -> None:
        with pytest.raises(ValueError, match="capability"):
            _profile("openai", "gpt", set())
        with pytest.raises(ValueError, match="estimated_cost_usd"):
            _profile("openai", "gpt", {"code-edit"}, cost=-0.01)

    def test_policy_rejects_conflicting_provider_rules(self) -> None:
        with pytest.raises(ValueError, match=r"allowed.*forbidden"):
            RoutingPolicy(
                allowed_providers={"openai"},
                forbidden_providers={"openai"},
            )


class TestResourceBrokerRouting:
    def test_picks_resource_with_required_capability_tags(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai"), _account("local")],
            capabilities=[
                _profile("openai", "gpt-code", {"code-edit", "long-context"}, cost=2.0),
                _profile("local", "ollama-small", {"cheap", "local"}, cost=0.0),
            ],
            quotas=[_quota("openai"), _quota("local")],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(role_capability_map={"developer": {"code-edit"}}),
        )

        assert decision.runnable is True
        assert decision.provider_id == "openai"
        assert decision.backend_id == "gpt-code"
        assert "selected" in decision.reason_codes
        assert len(decision.alternatives) == 2

    def test_rejects_disabled_provider_and_uses_fallback(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("primary", disabled=True), _account("fallback")],
            capabilities=[
                _profile("primary", "primary-code", {"code-edit"}, cost=0.10),
                _profile("fallback", "fallback-code", {"code-edit"}, cost=0.20),
            ],
            quotas=[_quota("primary"), _quota("fallback")],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(role_capability_map={"developer": {"code-edit"}}),
        )

        assert decision.runnable is True
        assert decision.provider_id == "fallback"
        assert "fallback_selected" in decision.reason_codes
        rejected = next(alt for alt in decision.alternatives if alt.provider_id == "primary")
        assert rejected.runnable is False
        assert "provider_disabled" in rejected.reason_codes

    def test_hard_monthly_budget_violation_fails_closed(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai", monthly_budget_usd=10.0)],
            capabilities=[_profile("openai", "gpt-code", {"code-edit"}, cost=0.01)],
            quotas=[_quota("openai", spent_usd_month_to_date=10.0)],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(
                role_capability_map={"developer": {"code-edit"}},
                hard_budget=True,
            ),
        )

        assert decision.runnable is False
        assert decision.provider_id is None
        assert "no_capable_resource" in decision.reason_codes
        assert "over_monthly_budget_hard" in decision.alternatives[0].reason_codes

    def test_missing_quota_for_budgeted_provider_fails_closed(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai", monthly_budget_usd=10.0)],
            capabilities=[_profile("openai", "gpt-code", {"code-edit"}, cost=0.01)],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(role_capability_map={"developer": {"code-edit"}}),
        )

        assert decision.runnable is False
        assert "quota_missing" in decision.alternatives[0].reason_codes

    def test_soft_monthly_budget_violation_warns_but_can_route(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai", monthly_budget_usd=10.0)],
            capabilities=[_profile("openai", "gpt-code", {"code-edit"}, cost=0.01)],
            quotas=[_quota("openai", spent_usd_month_to_date=10.0)],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(
                role_capability_map={"developer": {"code-edit"}},
                hard_budget=False,
            ),
        )

        assert decision.runnable is True
        assert decision.provider_id == "openai"
        assert "soft_budget_warning" in decision.reason_codes

    def test_prefer_local_selects_local_when_sufficient(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("cloud"), _account("local")],
            capabilities=[
                _profile("cloud", "sonnet", {"code-edit", "long-context"}, cost=0.30),
                _profile("local", "ollama", {"code-edit", "local"}, cost=0.0),
            ],
            quotas=[_quota("cloud"), _quota("local")],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(
                prefer_local=True,
                role_capability_map={"developer": {"code-edit"}},
            ),
        )

        assert decision.provider_id == "local"
        assert "prefer_local" in decision.reason_codes

    def test_role_capability_map_escalates_to_stronger_resource(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("cheap"), _account("strong")],
            capabilities=[
                _profile("cheap", "haiku", {"code-edit", "cheap"}, cost=0.02),
                _profile(
                    "strong",
                    "opus",
                    {"code-edit", "security-review", "long-context"},
                    cost=1.25,
                ),
            ],
            quotas=[_quota("cheap"), _quota("strong")],
        )

        decision = broker.route(
            role="security-reviewer",
            policy=RoutingPolicy(
                role_capability_map={
                    "developer": {"code-edit"},
                    "security-reviewer": {"code-edit", "security-review"},
                },
            ),
        )

        assert decision.provider_id == "strong"
        assert "role_capability_required" in decision.reason_codes
        cheap = next(alt for alt in decision.alternatives if alt.provider_id == "cheap")
        assert "missing_capability" in cheap.reason_codes

    def test_allowed_and_forbidden_providers_are_enforced(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai"), _account("anthropic"), _account("local")],
            capabilities=[
                _profile("openai", "gpt", {"code-edit"}, cost=0.20),
                _profile("anthropic", "sonnet", {"code-edit"}, cost=0.10),
                _profile("local", "ollama", {"code-edit"}, cost=0.0),
            ],
            quotas=[_quota("openai"), _quota("anthropic"), _quota("local")],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(
                allowed_providers={"openai"},
                forbidden_providers={"anthropic"},
                role_capability_map={"developer": {"code-edit"}},
            ),
        )

        assert decision.provider_id == "openai"
        local = next(alt for alt in decision.alternatives if alt.provider_id == "local")
        anthropic = next(alt for alt in decision.alternatives if alt.provider_id == "anthropic")
        assert "provider_not_allowed" in local.reason_codes
        assert "provider_forbidden" in anthropic.reason_codes

    def test_decision_serialization_redacts_provider_secrets(self) -> None:
        broker = ResourceBroker(
            accounts=[_account("openai", secret_token="sk-test-secret")],
            capabilities=[_profile("openai", "gpt-code", {"code-edit"}, cost=0.10)],
            quotas=[_quota("openai")],
        )

        decision = broker.route(
            role="developer",
            policy=RoutingPolicy(role_capability_map={"developer": {"code-edit"}}),
        )
        payload = decision.to_dict()
        rendered = repr(payload)

        assert "sk-test-secret" not in rendered
        assert "secret_token" not in rendered
        assert payload["provider_id"] == "openai"
        assert payload["alternatives"][0]["provider_id"] == "openai"
