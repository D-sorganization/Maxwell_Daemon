"""OpenAIBackend — configuration, registry, and capability reporting.

Streaming + complete call paths hit the OpenAI SDK which isn't easily stubbed
via respx without a lot of ceremony (the SDK does its own pooling). We cover
the easily-mocked surface here and defer end-to-end HTTP interaction to the
integration test tier.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.openai import OpenAIBackend
from maxwell_daemon.backends.registry import registry


class TestConfiguration:
    def test_requires_key_or_base_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(BackendUnavailableError):
            OpenAIBackend()

    def test_base_url_alone_is_enough_for_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        backend = OpenAIBackend(base_url="http://localhost:8000/v1")
        assert backend is not None

    def test_api_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend()
        assert backend is not None


class TestCapabilities:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def test_gpt4o_pricing(self) -> None:
        b = OpenAIBackend()
        caps = b.capabilities("gpt-4o")
        assert caps.cost_per_1k_input_tokens == pytest.approx(0.0025)
        assert caps.cost_per_1k_output_tokens == pytest.approx(0.01)
        assert caps.supports_vision is True

    def test_gpt4o_mini_cheaper(self) -> None:
        b = OpenAIBackend()
        mini = b.capabilities("gpt-4o-mini")
        full = b.capabilities("gpt-4o")
        assert mini.cost_per_1k_input_tokens < full.cost_per_1k_input_tokens

    def test_o1_supports_vision(self) -> None:
        b = OpenAIBackend()
        assert b.capabilities("o1").supports_vision is True

    def test_unknown_model_falls_back_safely(self) -> None:
        b = OpenAIBackend()
        caps = b.capabilities("obscure-model")
        assert caps.cost_per_1k_input_tokens > 0
        assert caps.max_context_tokens > 0


class TestCostEstimation:
    def test_cost_scales_with_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon.backends.base import TokenUsage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend()
        small = backend.estimate_cost(TokenUsage(100, 100, 200), "gpt-4o")
        big = backend.estimate_cost(TokenUsage(1000, 1000, 2000), "gpt-4o")
        assert big == pytest.approx(small * 10)


class TestRegistry:
    def test_registered_under_openai(self) -> None:
        assert "openai" in registry.available()
