"""Deterministic subscription-aware resource routing.

This module is intentionally pure: callers provide static accounts, capability
profiles, and quota snapshots; the broker explains a deterministic routing
decision without probing provider APIs or CLIs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

__all__ = [
    "CapabilityProfile",
    "QuotaSnapshot",
    "ResourceAccount",
    "ResourceBroker",
    "RoutingAlternative",
    "RoutingDecision",
    "RoutingPolicy",
]

IntegrationKind = Literal["api", "cli", "local", "cloud", "manual"]
AuthStatus = Literal["configured", "missing", "expired", "unknown"]
TermsMode = Literal["official", "user-entered", "heuristic", "disabled"]
QuotaSource = Literal["api", "cli", "user", "heuristic"]


@dataclass(slots=True, frozen=True)
class ResourceAccount:
    """A configured provider account or local resource."""

    provider_id: str
    display_name: str
    integration_kind: IntegrationKind
    auth_status: AuthStatus
    terms_mode: TermsMode
    monthly_budget_usd: float | None = None
    disabled: bool = False
    secret_token: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_non_empty("provider_id", self.provider_id)
        _require_non_empty("display_name", self.display_name)
        if self.monthly_budget_usd is not None and self.monthly_budget_usd < 0:
            raise ValueError("monthly_budget_usd must be >= 0 when set")


@dataclass(slots=True, frozen=True)
class QuotaSnapshot:
    """Point-in-time usage/quota estimate for a provider."""

    provider_id: str
    captured_at: datetime
    available_quota: float | None
    confidence: float
    source: QuotaSource
    reset_at: datetime | None = None
    spent_usd_month_to_date: float = 0.0

    def __post_init__(self) -> None:
        _require_non_empty("provider_id", self.provider_id)
        _require_non_empty("source", self.source)
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if self.available_quota is not None and self.available_quota < 0:
            raise ValueError("available_quota must be >= 0 when set")
        if self.spent_usd_month_to_date < 0:
            raise ValueError("spent_usd_month_to_date must be >= 0")


@dataclass(slots=True, frozen=True)
class CapabilityProfile:
    """Routable model/tool/backend capabilities attached to a provider."""

    provider_id: str
    backend_id: str
    capability_tags: set[str] | frozenset[str]
    max_context_tokens: int
    estimated_cost_usd: float
    latency_ms: int
    concurrency_limit: int

    def __post_init__(self) -> None:
        _require_non_empty("provider_id", self.provider_id)
        _require_non_empty("backend_id", self.backend_id)
        tags = frozenset(_normalise_tag(tag) for tag in self.capability_tags)
        if not tags:
            raise ValueError("capability_tags must include at least one capability")
        object.__setattr__(self, "capability_tags", tags)
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be > 0")
        if self.estimated_cost_usd < 0:
            raise ValueError("estimated_cost_usd must be >= 0")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")
        if self.concurrency_limit <= 0:
            raise ValueError("concurrency_limit must be > 0")


@dataclass(slots=True, frozen=True)
class RoutingPolicy:
    """Selection policy applied to a task role."""

    max_spend_per_task_usd: float | None = None
    max_spend_per_day_usd: float | None = None
    max_spend_per_month_usd: float | None = None
    prefer_local: bool = False
    allowed_providers: set[str] | frozenset[str] = field(default_factory=frozenset)
    forbidden_providers: set[str] | frozenset[str] = field(default_factory=frozenset)
    escalation_thresholds: dict[str, float] = field(default_factory=dict)
    role_capability_map: dict[str, set[str] | frozenset[str]] = field(
        default_factory=dict
    )
    hard_budget: bool = True
    soft_budget_utilization_threshold: float = 0.80

    def __post_init__(self) -> None:
        allowed = frozenset(
            _normalise_id(provider) for provider in self.allowed_providers
        )
        forbidden = frozenset(
            _normalise_id(provider) for provider in self.forbidden_providers
        )
        if allowed & forbidden:
            raise ValueError(
                "allowed_providers and forbidden_providers must not overlap"
            )
        object.__setattr__(self, "allowed_providers", allowed)
        object.__setattr__(self, "forbidden_providers", forbidden)
        normalised_map = {
            _normalise_id(role): frozenset(_normalise_tag(tag) for tag in tags)
            for role, tags in self.role_capability_map.items()
        }
        object.__setattr__(self, "role_capability_map", normalised_map)
        for field_name in (
            "max_spend_per_task_usd",
            "max_spend_per_day_usd",
            "max_spend_per_month_usd",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be >= 0 when set")
        if not 0.0 <= self.soft_budget_utilization_threshold <= 1.0:
            raise ValueError(
                "soft_budget_utilization_threshold must be between 0.0 and 1.0"
            )

    def required_capabilities_for(self, role: str) -> frozenset[str]:
        return frozenset(self.role_capability_map.get(_normalise_id(role), frozenset()))


@dataclass(slots=True, frozen=True)
class RoutingAlternative:
    provider_id: str
    backend_id: str
    runnable: bool
    reason_codes: tuple[str, ...]
    estimated_cost_usd: float
    missing_capabilities: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend_id": self.backend_id,
            "runnable": self.runnable,
            "reason_codes": list(self.reason_codes),
            "estimated_cost_usd": self.estimated_cost_usd,
            "missing_capabilities": list(self.missing_capabilities),
        }


@dataclass(slots=True, frozen=True)
class RoutingDecision:
    runnable: bool
    provider_id: str | None
    backend_id: str | None
    reason_codes: tuple[str, ...]
    estimated_cost_usd: float | None
    quota_impact: dict[str, Any]
    alternatives: tuple[RoutingAlternative, ...]
    fallback_plan: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reason_codes:
            raise ValueError("RoutingDecision must include reason_codes")
        if self.runnable and (self.provider_id is None or self.backend_id is None):
            raise ValueError(
                "runnable RoutingDecision requires provider_id and backend_id"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return API-safe decision data without account credentials or secrets."""
        return {
            "runnable": self.runnable,
            "provider_id": self.provider_id,
            "backend_id": self.backend_id,
            "reason_codes": list(self.reason_codes),
            "estimated_cost_usd": self.estimated_cost_usd,
            "quota_impact": dict(self.quota_impact),
            "alternatives": [
                alternative.to_dict() for alternative in self.alternatives
            ],
            "fallback_plan": list(self.fallback_plan),
        }


class ResourceBroker:
    """Route a role to a configured resource under static policy and quota data."""

    def __init__(
        self,
        *,
        accounts: list[ResourceAccount],
        capabilities: list[CapabilityProfile],
        quotas: list[QuotaSnapshot] | None = None,
    ) -> None:
        self._accounts = _index_unique_accounts(accounts)
        self._capabilities = tuple(
            sorted(capabilities, key=lambda item: item.backend_id)
        )
        self._quotas = _index_latest_quotas(quotas or [])
        for profile in self._capabilities:
            if profile.provider_id not in self._accounts:
                raise ValueError(
                    f"CapabilityProfile references unknown provider: {profile.provider_id}"
                )

    def route(
        self,
        *,
        role: str,
        policy: RoutingPolicy,
        required_capabilities: set[str] | frozenset[str] | None = None,
    ) -> RoutingDecision:
        _require_non_empty("role", role)
        required = policy.required_capabilities_for(role) | frozenset(
            _normalise_tag(tag) for tag in (required_capabilities or frozenset())
        )
        alternatives = tuple(
            self._evaluate(profile=profile, required=required, policy=policy)
            for profile in self._capabilities
        )
        runnable = [alternative for alternative in alternatives if alternative.runnable]
        if not runnable:
            return RoutingDecision(
                runnable=False,
                provider_id=None,
                backend_id=None,
                reason_codes=("no_capable_resource",),
                estimated_cost_usd=None,
                quota_impact={},
                alternatives=alternatives,
                fallback_plan=_fallback_plan(alternatives),
            )

        chosen = min(runnable, key=lambda alt: self._rank(alt, policy))
        reason_codes = list(chosen.reason_codes)
        if any(not alternative.runnable for alternative in alternatives):
            reason_codes.append("fallback_selected")
        if policy.prefer_local and self._is_local(chosen.provider_id):
            reason_codes.append("prefer_local")
        if required:
            reason_codes.append("role_capability_required")
        reason_codes.append("selected")

        return RoutingDecision(
            runnable=True,
            provider_id=chosen.provider_id,
            backend_id=chosen.backend_id,
            reason_codes=_dedupe(reason_codes),
            estimated_cost_usd=chosen.estimated_cost_usd,
            quota_impact=self._quota_impact(chosen),
            alternatives=alternatives,
            fallback_plan=_fallback_plan(
                alternative for alternative in alternatives if alternative is not chosen
            ),
        )

    def _evaluate(
        self,
        *,
        profile: CapabilityProfile,
        required: frozenset[str],
        policy: RoutingPolicy,
    ) -> RoutingAlternative:
        account = self._accounts[profile.provider_id]
        quota = self._quotas.get(profile.provider_id)
        reasons: list[str] = []
        missing = tuple(sorted(required - profile.capability_tags))

        if account.disabled:
            reasons.append("provider_disabled")
        if account.auth_status != "configured":
            reasons.append("auth_not_configured")
        if account.terms_mode == "disabled":
            reasons.append("terms_disabled")
        if (
            policy.allowed_providers
            and profile.provider_id not in policy.allowed_providers
        ):
            reasons.append("provider_not_allowed")
        if profile.provider_id in policy.forbidden_providers:
            reasons.append("provider_forbidden")
        if missing:
            reasons.append("missing_capability")
        if (
            policy.max_spend_per_task_usd is not None
            and profile.estimated_cost_usd > policy.max_spend_per_task_usd
        ):
            reasons.append("over_task_budget")
        if (
            policy.max_spend_per_month_usd is not None
            and quota is not None
            and quota.spent_usd_month_to_date + profile.estimated_cost_usd
            > policy.max_spend_per_month_usd
        ):
            reasons.append(
                _budget_reason(policy, hard_code="over_policy_monthly_budget_hard")
            )
        reasons.extend(self._account_budget_reasons(account, quota, profile, policy))
        if (
            quota is not None
            and quota.available_quota is not None
            and quota.available_quota <= 0
        ):
            reasons.append("quota_exhausted")

        hard_reasons = {
            "auth_not_configured",
            "missing_capability",
            "over_monthly_budget_hard",
            "over_policy_monthly_budget_hard",
            "over_task_budget",
            "provider_disabled",
            "provider_forbidden",
            "provider_not_allowed",
            "quota_missing",
            "quota_exhausted",
            "terms_disabled",
        }
        return RoutingAlternative(
            provider_id=profile.provider_id,
            backend_id=profile.backend_id,
            runnable=not (set(reasons) & hard_reasons),
            reason_codes=_dedupe(reasons or ["eligible"]),
            estimated_cost_usd=profile.estimated_cost_usd,
            missing_capabilities=missing,
        )

    def _account_budget_reasons(
        self,
        account: ResourceAccount,
        quota: QuotaSnapshot | None,
        profile: CapabilityProfile,
        policy: RoutingPolicy,
    ) -> list[str]:
        if account.monthly_budget_usd is None:
            return []
        if quota is None:
            return ["quota_missing"]

        projected = quota.spent_usd_month_to_date + profile.estimated_cost_usd
        if projected >= account.monthly_budget_usd:
            return [_budget_reason(policy, hard_code="over_monthly_budget_hard")]

        utilisation = (
            projected / account.monthly_budget_usd
            if account.monthly_budget_usd > 0
            else 1.0
        )
        if utilisation >= policy.soft_budget_utilization_threshold:
            return ["soft_budget_warning"]
        return []

    def _rank(
        self, alternative: RoutingAlternative, policy: RoutingPolicy
    ) -> tuple[int, float, int, str, str]:
        profile = self._profile_for(alternative)
        return (
            0 if policy.prefer_local and self._is_local(alternative.provider_id) else 1,
            alternative.estimated_cost_usd,
            profile.latency_ms,
            alternative.provider_id,
            alternative.backend_id,
        )

    def _is_local(self, provider_id: str) -> bool:
        account = self._accounts[provider_id]
        provider_profile_tags = {
            tag
            for profile in self._capabilities
            if profile.provider_id == provider_id
            for tag in profile.capability_tags
        }
        return account.integration_kind == "local" or "local" in provider_profile_tags

    def _profile_for(self, alternative: RoutingAlternative) -> CapabilityProfile:
        for profile in self._capabilities:
            if (
                profile.provider_id == alternative.provider_id
                and profile.backend_id == alternative.backend_id
            ):
                return profile
        raise RuntimeError(
            f"Missing profile for {alternative.provider_id}/{alternative.backend_id}"
        )

    def _quota_impact(self, chosen: RoutingAlternative) -> dict[str, Any]:
        quota = self._quotas.get(chosen.provider_id)
        if quota is None:
            return {
                "provider_id": chosen.provider_id,
                "estimated_cost_usd": chosen.estimated_cost_usd,
            }
        return {
            "provider_id": chosen.provider_id,
            "estimated_cost_usd": chosen.estimated_cost_usd,
            "source": quota.source,
            "confidence": quota.confidence,
            "spent_usd_month_to_date": quota.spent_usd_month_to_date,
            "available_quota": quota.available_quota,
        }


def _index_unique_accounts(
    accounts: list[ResourceAccount],
) -> dict[str, ResourceAccount]:
    indexed: dict[str, ResourceAccount] = {}
    for account in accounts:
        if account.provider_id in indexed:
            raise ValueError(
                f"Duplicate ResourceAccount provider_id: {account.provider_id}"
            )
        indexed[account.provider_id] = account
    if not indexed:
        raise ValueError("ResourceBroker requires at least one ResourceAccount")
    return indexed


def _index_latest_quotas(quotas: list[QuotaSnapshot]) -> dict[str, QuotaSnapshot]:
    indexed: dict[str, QuotaSnapshot] = {}
    for quota in quotas:
        current = indexed.get(quota.provider_id)
        if current is None or quota.captured_at >= current.captured_at:
            indexed[quota.provider_id] = quota
    return indexed


def _fallback_plan(alternatives: Iterable[RoutingAlternative]) -> tuple[str, ...]:
    return tuple(
        f"{alternative.provider_id}/{alternative.backend_id}:{','.join(alternative.reason_codes)}"
        for alternative in alternatives
    )


def _budget_reason(policy: RoutingPolicy, *, hard_code: str) -> str:
    return hard_code if policy.hard_budget else "soft_budget_warning"


def _dedupe(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _normalise_id(value: str) -> str:
    _require_non_empty("id", value)
    return value.strip()


def _normalise_tag(value: str) -> str:
    _require_non_empty("capability tag", value)
    return value.strip().lower()


def _require_non_empty(field_name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
