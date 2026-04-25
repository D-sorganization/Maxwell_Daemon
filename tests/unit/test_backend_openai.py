"""OpenAIBackend — configuration, registry, and capability reporting.

Streaming + complete call paths hit the OpenAI SDK which isn't easily stubbed
via respx without a lot of ceremony (the SDK does its own pooling). We cover
the easily-mocked surface here and defer end-to-end HTTP interaction to the
integration test tier.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError, Message, MessageRole
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
        assert mini.cost_per_1k_input_tokens < full.cost_per_1k_input_tokens  # type: ignore[operator]

    def test_o1_supports_vision(self) -> None:
        b = OpenAIBackend()
        assert b.capabilities("o1").supports_vision is True

    def test_unknown_model_falls_back_safely(self) -> None:
        b = OpenAIBackend()
        caps = b.capabilities("obscure-model")
        assert caps.cost_per_1k_input_tokens >= 0  # type: ignore[operator]
        assert caps.max_context_tokens > 0


class TestCostEstimation:
    def test_cost_scales_with_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon.backends.base import TokenUsage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend()
        small = backend.estimate_cost(TokenUsage(100, 100, 200), "gpt-4o")
        big = backend.estimate_cost(TokenUsage(1000, 1000, 2000), "gpt-4o")
        assert big == pytest.approx(small * 10)  # type: ignore[operator]


class _FakeOpenAIStream:
    def __init__(self, parts: list[str | None]) -> None:
        self._parts = parts
        self._i = 0

    def __aiter__(self) -> _FakeOpenAIStream:
        return self

    async def __anext__(self) -> object:
        if self._i >= len(self._parts):
            raise StopAsyncIteration
        part = self._parts[self._i]
        self._i += 1
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])


class TestRequestPaths:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    @pytest.mark.asyncio
    async def test_complete_maps_response_and_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=None),
                        finish_reason=None,
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18),
                model="gpt-4o-mini",
                model_dump=lambda: {"ok": True},
            )

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
            models=SimpleNamespace(list=lambda: []),
        )
        monkeypatch.setattr(
            "maxwell_daemon.backends.openai.openai.AsyncOpenAI",
            lambda **_: fake_client,
        )

        backend = OpenAIBackend()
        resp = await backend.complete(
            [Message(role=MessageRole.USER, content="hi")],
            model="gpt-4o-mini",
            max_tokens=64,
            tools=[{"type": "function"}],
            top_p=0.5,
        )

        assert resp.content == ""
        assert resp.finish_reason == "stop"
        assert resp.usage.total_tokens == 18
        assert resp.raw == {"ok": True}
        assert calls and calls[0]["max_tokens"] == 64
        assert calls[0]["tools"] == [{"type": "function"}]
        assert calls[0]["top_p"] == 0.5

    @pytest.mark.asyncio
    async def test_stream_and_health_check_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs: object) -> object:
            calls.append(kwargs)
            assert kwargs["stream"] is True
            return _FakeOpenAIStream(["a", None, "b"])

        async def ok_models_list() -> list[str]:
            return ["gpt-4o-mini"]

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
            models=SimpleNamespace(list=ok_models_list),
        )
        monkeypatch.setattr(
            "maxwell_daemon.backends.openai.openai.AsyncOpenAI",
            lambda **_: fake_client,
        )
        backend = OpenAIBackend()

        parts = [
            p
            async for p in backend.stream(
                [],
                model="gpt-4o-mini",
                max_tokens=12,
                tools=[{"type": "function"}],
            )
        ]
        assert parts == ["a", "b"]
        assert calls and calls[0]["max_tokens"] == 12
        assert calls[0]["tools"] == [{"type": "function"}]
        assert await backend.health_check() is True

        async def broken_models_list() -> list[str]:
            raise RuntimeError("boom")

        fake_client.models = SimpleNamespace(list=broken_models_list)
        assert await backend.health_check() is False


class TestRegistry:
    def test_registered_under_openai(self) -> None:
        assert "openai" in registry.available()
