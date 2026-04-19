"""Unit tests for the backend abstraction layer.

We intentionally don't hit real APIs here — those are integration tests. These
tests pin down the interface contract, the registry behavior, and cost math.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from conductor.backends import (
    BackendCapabilities,
    BackendError,
    BackendRegistry,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)


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
        a = TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30, cached_tokens=5)
        b = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, cached_tokens=1)
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


class TestCostEstimation:
    def test_cost_calc(self) -> None:
        backend = FakeBackend()
        usage = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)
        # 1000 tokens * $0.001/1k + 500 tokens * $0.002/1k = $0.001 + $0.001 = $0.002
        assert backend.estimate_cost(usage, "any-model") == pytest.approx(0.002)


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
