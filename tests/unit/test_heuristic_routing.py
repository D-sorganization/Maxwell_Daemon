"""Tests for complexity-, capability-, and latency-aware heuristic model routing."""

from __future__ import annotations

import pytest

from maxwell_daemon.model_routing.heuristic import ModelRecommendation, route_model


class TestComplexityTiers:
    def test_low_complexity_picks_small_model(self) -> None:
        rec = route_model(1.0)
        assert rec.complexity_tier == "low"
        assert "haiku" in rec.model or "mini" in rec.model or "7b" in rec.model

    def test_mid_complexity_picks_mid_model(self) -> None:
        rec = route_model(5.0)
        assert rec.complexity_tier == "mid"

    def test_high_complexity_picks_large_model(self) -> None:
        rec = route_model(9.0)
        assert rec.complexity_tier == "high"

    def test_boundary_3_is_low(self) -> None:
        assert route_model(3.0).complexity_tier == "low"

    def test_boundary_4_is_mid(self) -> None:
        assert route_model(4.0).complexity_tier == "mid"

    def test_boundary_7_is_high(self) -> None:
        assert route_model(7.0).complexity_tier == "high"

    def test_clamps_below_zero(self) -> None:
        rec = route_model(-5.0)
        assert rec.complexity_tier == "low"

    def test_clamps_above_ten(self) -> None:
        rec = route_model(99.0)
        assert rec.complexity_tier == "high"


class TestCapabilityRouting:
    def test_vision_requirement_bumps_model(self) -> None:
        rec = route_model(1.0, {"vision"}, preferred_provider="anthropic")
        assert "vision" in rec.capabilities_matched or rec.model in {
            "claude-sonnet-4-6",
            "claude-opus-4-7",
        }

    def test_long_context_capability_present_on_result(self) -> None:
        rec = route_model(5.0, {"long_context"}, preferred_provider="anthropic")
        assert "long_context" in rec.capabilities_matched or rec.capabilities_missing

    def test_missing_capabilities_reported(self) -> None:
        # "telepathy" is not a real capability — should appear in missing set.
        rec = route_model(5.0, {"telepathy"})
        assert "telepathy" in rec.capabilities_missing

    def test_empty_required_capabilities_always_succeeds(self) -> None:
        rec = route_model(5.0, set())
        assert isinstance(rec, ModelRecommendation)

    def test_none_required_capabilities_succeeds(self) -> None:
        rec = route_model(5.0, None)
        assert isinstance(rec, ModelRecommendation)


class TestLatencyTiers:
    def test_fast_tier_picks_smallest_anthropic_model(self) -> None:
        rec = route_model(9.0, latency_tier="fast", preferred_provider="anthropic")
        # Even with high complexity, fast should prefer small model.
        assert "haiku" in rec.model

    def test_quality_tier_picks_largest_anthropic_model(self) -> None:
        rec = route_model(1.0, latency_tier="quality", preferred_provider="anthropic")
        assert "opus" in rec.model

    def test_balanced_tier_is_default(self) -> None:
        rec = route_model(5.0)
        assert rec.latency_tier == "balanced"


class TestProviderSelection:
    def test_anthropic_provider_returns_claude_model(self) -> None:
        rec = route_model(5.0, preferred_provider="anthropic")
        assert rec.provider == "anthropic"
        assert "claude" in rec.model

    def test_openai_provider_returns_gpt_model(self) -> None:
        rec = route_model(5.0, preferred_provider="openai")
        assert rec.provider == "openai"
        assert "gpt" in rec.model or rec.model.startswith("o")

    def test_ollama_provider_returns_local_model(self) -> None:
        rec = route_model(5.0, preferred_provider="ollama")
        assert rec.provider == "ollama"


class TestRationale:
    def test_rationale_contains_complexity(self) -> None:
        rec = route_model(6.5)
        assert "6.5" in rec.rationale

    def test_rationale_contains_provider(self) -> None:
        rec = route_model(5.0, preferred_provider="openai")
        assert "openai" in rec.rationale

    def test_fast_latency_noted_in_rationale(self) -> None:
        rec = route_model(5.0, latency_tier="fast")
        assert "fast" in rec.rationale

    def test_missing_caps_noted_in_rationale(self) -> None:
        rec = route_model(5.0, {"unicorn_mode"})
        assert "unicorn_mode" in rec.rationale


class TestReturnType:
    def test_returns_model_recommendation(self) -> None:
        rec = route_model(5.0)
        assert isinstance(rec, ModelRecommendation)

    def test_model_is_nonempty_string(self) -> None:
        rec = route_model(5.0)
        assert isinstance(rec.model, str) and len(rec.model) > 0

    @pytest.mark.parametrize("complexity", [0, 3, 4, 6, 7, 10])
    def test_various_complexities_return_valid_result(self, complexity: int) -> None:
        rec = route_model(float(complexity))
        assert isinstance(rec, ModelRecommendation)
        assert rec.model
