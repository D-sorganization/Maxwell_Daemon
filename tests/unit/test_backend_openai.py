"""OpenAIBackend — configuration, registry, and capability reporting.

Streaming + complete call paths hit the OpenAI SDK which isn't easily stubbed
via respx without a lot of ceremony (the SDK does its own pooling). We cover
the easily-mocked surface here and defer end-to-end HTTP interaction to the
integration test tier.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError, Message, MessageRole
from maxwell_daemon.backends.openai import OpenAIBackend
from maxwell_daemon.backends.registry import registry


class _FakeUsage:
    prompt_tokens = 7
    completion_tokens = 11
    total_tokens = 18


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeDelta:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None = "done", finish_reason: str | None = "stop") -> None:
        self.message = _FakeMessage(content)
        self.delta = _FakeDelta(content)
        self.finish_reason = finish_reason


class _FakeResponse:
    model = "gpt-test"
    usage = _FakeUsage()

    def __init__(self, content: str | None = "done", finish_reason: str | None = "stop") -> None:
        self.choices = [_FakeChoice(content=content, finish_reason=finish_reason)]

    def model_dump(self) -> dict[str, Any]:
        return {"model": self.model}


class _FakeStream:
    def __init__(self, chunks: list[str | None]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[_FakeResponse]:
        for chunk in self._chunks:
            yield _FakeResponse(content=chunk)


class _FakeCompletions:
    def __init__(self, result: _FakeResponse | _FakeStream) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> _FakeResponse | _FakeStream:
        self.calls.append(params)
        return self.result


class _FakeClient:
    def __init__(self, result: _FakeResponse | _FakeStream) -> None:
        self.chat = type("Chat", (), {"completions": _FakeCompletions(result)})()


class _FakeModels:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises

    async def list(self) -> list[str]:
        if self.raises:
            raise RuntimeError("unreachable")
        return []


def _backend_with_client(client: object) -> OpenAIBackend:
    backend = OpenAIBackend(base_url="http://localhost:8000/v1")
    backend._client = client  # type: ignore[assignment]
    return backend


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


class TestComplete:
    def test_complete_maps_messages_options_and_usage(self) -> None:
        client = _FakeClient(_FakeResponse(content="hello", finish_reason="tool_calls"))
        backend = _backend_with_client(client)

        response = asyncio.run(
            backend.complete(
                [
                    Message(role=MessageRole.SYSTEM, content="be terse"),
                    Message(role=MessageRole.USER, content="hi"),
                ],
                model="gpt-test",
                temperature=0.2,
                max_tokens=123,
                tools=[{"type": "function"}],
                seed=9,
            )
        )

        call = client.chat.completions.calls[0]
        assert call["messages"] == [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]
        assert call["temperature"] == 0.2
        assert call["max_tokens"] == 123
        assert call["tools"] == [{"type": "function"}]
        assert call["seed"] == 9
        assert response.content == "hello"
        assert response.finish_reason == "tool_calls"
        assert response.usage.prompt_tokens == 7
        assert response.usage.completion_tokens == 11
        assert response.usage.total_tokens == 18
        assert response.model == "gpt-test"
        assert response.backend == "openai"
        assert response.raw == {"model": "gpt-test"}

    def test_complete_defaults_empty_content_and_finish_reason(self) -> None:
        backend = _backend_with_client(_FakeClient(_FakeResponse(content=None, finish_reason=None)))

        response = asyncio.run(
            backend.complete([Message(role=MessageRole.USER, content="hi")], model="gpt-test")
        )

        assert response.content == ""
        assert response.finish_reason == "stop"


class TestStream:
    def test_stream_yields_non_empty_delta_content(self) -> None:
        client = _FakeClient(_FakeStream(["hello", None, "", " world"]))
        backend = _backend_with_client(client)

        async def collect() -> list[str]:
            return [
                chunk
                async for chunk in backend.stream(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="gpt-test",
                    max_tokens=20,
                    tools=[{"type": "function"}],
                    user="test-user",
                )
            ]

        assert asyncio.run(collect()) == ["hello", " world"]
        call = client.chat.completions.calls[0]
        assert call["stream"] is True
        assert call["max_tokens"] == 20
        assert call["tools"] == [{"type": "function"}]
        assert call["user"] == "test-user"


class TestHealthCheck:
    def test_health_check_returns_true_when_models_list_succeeds(self) -> None:
        client = type("Client", (), {"models": _FakeModels()})()
        assert asyncio.run(_backend_with_client(client).health_check()) is True

    def test_health_check_returns_false_when_models_list_fails(self) -> None:
        client = type("Client", (), {"models": _FakeModels(raises=True)})()
        assert asyncio.run(_backend_with_client(client).health_check()) is False


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
        assert caps.cost_per_1k_input_tokens >= 0
        assert caps.max_context_tokens > 0


class TestCostEstimation:
    def test_cost_scales_with_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon.backends.base import TokenUsage

        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        backend = OpenAIBackend()
        small = backend.estimate_cost(TokenUsage(100, 100, 200), "gpt-4o")
        big = backend.estimate_cost(TokenUsage(1000, 1000, 2000), "gpt-4o")
        assert big == pytest.approx(small * 10)


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
