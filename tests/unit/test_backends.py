"""Unit tests for the backend abstraction layer.

We intentionally don't hit real APIs here — those are integration tests. These
tests pin down the interface contract, the registry behavior, and cost math.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from maxwell_daemon.backends import (
    BackendCapabilities,
    BackendError,
    BackendManifest,
    BackendRegistry,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.pricing import cost_for, get_rates, is_free_provider


class FakeBackend(ILLMBackend):
    name = "fake"

    def __init__(self, *, canned_response: str = "hello", **_: Any) -> None:
        self.canned = canned_response
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BackendResponse:
        self.calls.append({"messages": messages, "model": model})
        return BackendResponse(
            content=self.canned,
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            model=model,
            backend=self.name,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        for ch in self.canned:
            yield ch

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(
            cost_per_1k_input_tokens=0.001,
            cost_per_1k_output_tokens=0.002,
        )


class TestTokenUsage:
    def test_addition_sums_all_fields(self) -> None:
        a = TokenUsage(
            prompt_tokens=10, completion_tokens=20, total_tokens=30, cached_tokens=5
        )
        b = TokenUsage(
            prompt_tokens=1, completion_tokens=2, total_tokens=3, cached_tokens=1
        )
        result = a + b
        assert result.prompt_tokens == 11
        assert result.completion_tokens == 22
        assert result.total_tokens == 33
        assert result.cached_tokens == 6


class TestBackendRegistry:
    def test_register_and_create(self) -> None:
        reg = BackendRegistry()
        reg.register("fake", FakeBackend)
        backend = reg.create("fake", {"canned_response": "hi"})
        assert isinstance(backend, FakeBackend)
        assert backend.canned == "hi"

    def test_duplicate_registration_rejected(self) -> None:
        reg = BackendRegistry()
        reg.register("fake", FakeBackend)
        with pytest.raises(BackendError, match="already registered"):
            reg.register("fake", FakeBackend)

    def test_unknown_backend_rejected(self) -> None:
        reg = BackendRegistry()
        with pytest.raises(BackendError, match="Unknown backend"):
            reg.create("nonexistent", {})

    def test_available_returns_sorted(self) -> None:
        reg = BackendRegistry()
        reg.register("zulu", FakeBackend)
        reg.register("alpha", FakeBackend)
        assert reg.available() == ["alpha", "zulu"]

    def test_catalog_includes_builtin_manifest_metadata(self) -> None:
        reg = BackendRegistry()

        catalog = {entry.name: entry for entry in reg.catalog()}

        assert catalog["claude"] == BackendManifest(
            module_name="claude",
            name="claude",
            display_name="Anthropic Claude",
            description="Anthropic-hosted Claude models over the public API.",
            requires_api_key=True,
            local_only=False,
            default_endpoint=None,
            api_key_env_var="ANTHROPIC_API_KEY",
            endpoint_env_var=None,
            install_extra=None,
            command=None,
        )
        assert catalog["ollama"].local_only is True
        assert catalog["ollama"].default_endpoint == "http://localhost:11434"
        assert catalog["codex-cli"].command == "codex"

    def test_catalog_appends_runtime_registered_backends(self) -> None:
        reg = BackendRegistry()
        reg.register("custom-backend", FakeBackend)

        catalog = {entry.name: entry for entry in reg.catalog()}

        assert catalog["custom-backend"] == BackendManifest(
            module_name=None,
            name="custom-backend",
            display_name="Custom Backend",
            description="Runtime-registered backend.",
            requires_api_key=False,
            local_only=False,
            default_endpoint=None,
            api_key_env_var=None,
            endpoint_env_var=None,
            install_extra=None,
            command=None,
        )


class TestCostEstimation:
    def test_cost_calc(self) -> None:
        backend = FakeBackend()
        usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        # 1000 tokens * $0.001/1k + 500 tokens * $0.002/1k = $0.001 + $0.001 = $0.002
        assert backend.estimate_cost(usage, "any-model") == pytest.approx(0.002)

    def test_pricing_free_provider_short_circuits(self) -> None:
        assert is_free_provider("ollama")
        assert get_rates("ollama", "any-local-model") == (0.0, 0.0)

    def test_unknown_pricing_falls_back_to_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)

        assert get_rates("unknown-provider", "mystery") == (0.0, 0.0)
        assert get_rates("openai", "mystery") == (0.0, 0.0)
        assert cost_for("unknown-provider", "mystery", usage) == 0.0
        captured = capsys.readouterr()
        output = captured.out + captured.err
        assert "Unknown provider" in output
        assert "Unknown model" in output

    def test_anthropic_models_priced_nonzero(self) -> None:
        # Pin the Anthropic lookup so the existing behavior is regression-guarded
        # after the OpenAI/Azure tables were added alongside it (#155).
        for model in ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"):
            price_in, price_out = get_rates("claude", model)
            assert price_in > 0, f"claude {model} input price should be > 0"
            assert (
                price_out > price_in
            ), f"claude {model} output should cost more than input"

    @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"])
    def test_openai_models_priced_nonzero(self, model: str) -> None:
        price_in, price_out = get_rates("openai", model)
        assert price_in > 0, f"openai {model} input price should be > 0"
        assert price_out > 0, f"openai {model} output price should be > 0"

    @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"])
    def test_azure_models_priced_nonzero(self, model: str) -> None:
        # Azure OpenAI Service standard deployments charge the same per-token
        # rates as OpenAI direct; the table mirrors those values.
        price_in, price_out = get_rates("azure", model)
        assert price_in > 0, f"azure {model} input price should be > 0"
        assert price_out > 0, f"azure {model} output price should be > 0"

    def test_cost_for_openai_nonzero(self) -> None:
        usage = TokenUsage(
            prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000
        )
        # gpt-4o: $2.50 in + $10.00 out per 1M = $12.50 for a 1M/1M request.
        assert cost_for("openai", "gpt-4o", usage) == pytest.approx(12.5)

    def test_cost_for_azure_nonzero(self) -> None:
        usage = TokenUsage(
            prompt_tokens=1_000_000, completion_tokens=1_000_000, total_tokens=2_000_000
        )
        assert cost_for("azure", "gpt-4o", usage) == pytest.approx(12.5)

    def test_unknown_openai_model_warns_without_crashing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        # Known provider, unknown model — should warn and return 0.0, not raise.
        cost = cost_for("openai", "gpt-5-fantasy", usage)
        assert cost == 0.0
        captured = capsys.readouterr()
        assert "Unknown model" in captured.out or "Unknown model" in captured.err
        assert "gpt-5-fantasy" in captured.out or "gpt-5-fantasy" in captured.err

    def test_unknown_azure_model_warns_without_crashing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        cost = cost_for("azure", "mystery-deployment", usage)
        assert cost == 0.0
        captured = capsys.readouterr()
        assert "Unknown model" in captured.out or "Unknown model" in captured.err
        assert (
            "mystery-deployment" in captured.out or "mystery-deployment" in captured.err
        )


class TestBackendInterface:
    """Synchronous tests that drive async methods via asyncio.run()."""

    def test_complete_returns_response(self) -> None:
        import asyncio

        backend = FakeBackend(canned_response="world")
        resp = asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="test-model",
            )
        )
        assert resp.content == "world"
        assert resp.backend == "fake"
        assert resp.usage.total_tokens == 30

    def test_stream_yields_chunks(self) -> None:
        import asyncio

        async def collect() -> list[str]:
            backend = FakeBackend(canned_response="abc")
            return [
                c
                async for c in backend.stream(
                    [Message(role=MessageRole.USER, content="hi")], model="test"
                )
            ]

        assert asyncio.run(collect()) == ["a", "b", "c"]

    def test_health_check(self) -> None:
        import asyncio

        assert asyncio.run(FakeBackend().health_check()) is True
