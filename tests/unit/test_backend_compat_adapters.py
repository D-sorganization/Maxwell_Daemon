"""Tests for OpenAI-compatible LLM backend adapters.

Covers: DeepSeek, Together, OpenRouter.

Groq and Mistral use optional native SDKs imported via import_module; their
BackendUnavailableError paths are already covered in
test_backend_optional_sdks.py.

These adapters all follow the same ILLMBackend protocol; we verify:
- Init raises BackendUnavailableError when no API key is available.
- Capabilities are reported with sensible defaults.
- complete() maps the response correctly.
- stream() yields only non-empty deltas.
- health_check() returns True/False appropriately.
- list_models() returns a list and silently handles errors.
- Each backend is registered in the global registry.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from maxwell_daemon.backends.base import (
    BackendUnavailableError,
    Message,
    MessageRole,
)
from maxwell_daemon.backends.registry import registry

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _msg(content: str = "hello") -> list[Message]:
    return [Message(role=MessageRole.USER, content=content)]


def _fake_response(content: str = "answer") -> object:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=3, total_tokens=8),
        model="test-model",
        model_dump=lambda: {"ok": True},
    )


class _FakeStream:
    def __init__(self, parts: list[str | None]) -> None:
        self._parts = parts
        self._i = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> object:
        if self._i >= len(self._parts):
            raise StopAsyncIteration
        part = self._parts[self._i]
        self._i += 1
        return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=part))])


def _fake_client(content: str = "hi", stream_parts: list[str | None] | None = None) -> object:
    parts = stream_parts if stream_parts is not None else ["he", None, "llo"]

    async def create(**_: Any) -> object:
        if _.get("stream"):
            return _FakeStream(parts)
        return _fake_response(content)

    async def models_list() -> list[Any]:
        return [SimpleNamespace(id="m1"), SimpleNamespace(id="m2")]

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        models=SimpleNamespace(list=models_list),
    )


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------


class TestDeepSeekBackend:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        with pytest.raises(BackendUnavailableError):
            DeepSeekBackend()

    def test_accepts_key_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        b = DeepSeekBackend()
        assert b is not None

    def test_capabilities_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        caps = DeepSeekBackend().capabilities("deepseek-chat")
        assert caps.supports_streaming is True
        assert caps.supports_tool_use is True
        assert caps.supports_vision is False

    def test_capabilities_reasoner_no_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        caps = DeepSeekBackend().capabilities("deepseek-reasoner")
        assert caps.supports_tool_use is False

    @pytest.mark.asyncio
    async def test_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: _fake_client("deepseek answer"),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        resp = await DeepSeekBackend().complete(_msg(), model="deepseek-chat")
        assert resp.content == "deepseek answer"
        assert resp.backend == "deepseek"

    @pytest.mark.asyncio
    async def test_complete_with_max_tokens_and_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        calls: list[dict[str, Any]] = []

        async def fake_create(**kw: Any) -> Any:
            calls.append(kw)
            return _fake_response()

        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
                models=SimpleNamespace(list=AsyncMock(return_value=[])),
            ),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        await DeepSeekBackend().complete(
            _msg(), model="deepseek-chat", max_tokens=50, tools=[{"type": "function"}]
        )
        assert calls[0]["max_tokens"] == 50
        assert calls[0]["tools"] == [{"type": "function"}]

    @pytest.mark.asyncio
    async def test_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: _fake_client(stream_parts=["x", None, "y"]),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        parts = [p async for p in DeepSeekBackend().stream(_msg(), model="deepseek-chat")]
        assert parts == ["x", "y"]

    @pytest.mark.asyncio
    async def test_health_check_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: _fake_client(),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        assert await DeepSeekBackend().health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

        async def broken_list() -> None:
            raise RuntimeError("down")

        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(models=SimpleNamespace(list=broken_list)),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        assert await DeepSeekBackend().health_check() is False

    @pytest.mark.asyncio
    async def test_list_models(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

        async def fake_models_list() -> object:
            """Returns an object with a .data list, as the real openai SDK does."""
            return SimpleNamespace(data=[SimpleNamespace(id="m1"), SimpleNamespace(id="m2")])

        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(
                chat=SimpleNamespace(
                    completions=SimpleNamespace(create=AsyncMock(return_value=_fake_response()))
                ),
                models=SimpleNamespace(list=fake_models_list),
            ),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        models = await DeepSeekBackend().list_models()
        assert "m1" in models

    @pytest.mark.asyncio
    async def test_list_models_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")

        async def broken_list() -> None:
            raise RuntimeError("down")

        monkeypatch.setattr(
            "maxwell_daemon.backends.deepseek.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(models=SimpleNamespace(list=broken_list)),
        )
        from maxwell_daemon.backends.deepseek import DeepSeekBackend

        assert await DeepSeekBackend().list_models() == []

    def test_registered(self) -> None:
        assert "deepseek" in registry.available()


# ---------------------------------------------------------------------------
# Together AI
# ---------------------------------------------------------------------------


class TestTogetherBackend:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
        from maxwell_daemon.backends.together import TogetherBackend

        with pytest.raises(BackendUnavailableError):
            TogetherBackend()

    def test_accepts_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        from maxwell_daemon.backends.together import TogetherBackend

        assert TogetherBackend() is not None

    def test_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        from maxwell_daemon.backends.together import TogetherBackend

        caps = TogetherBackend().capabilities("meta-llama/Llama-3-70b-chat-hf")
        assert caps.supports_streaming is True

    @pytest.mark.asyncio
    async def test_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.together.openai.AsyncOpenAI",
            lambda **_: _fake_client("together answer"),
        )
        from maxwell_daemon.backends.together import TogetherBackend

        resp = await TogetherBackend().complete(_msg(), model="m")
        assert resp.content == "together answer"

    @pytest.mark.asyncio
    async def test_complete_with_max_tokens_and_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        calls: list[dict[str, Any]] = []

        async def fake_create(**kw: Any) -> Any:
            calls.append(kw)
            return _fake_response()

        monkeypatch.setattr(
            "maxwell_daemon.backends.together.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
                models=SimpleNamespace(list=AsyncMock(return_value=[])),
            ),
        )
        from maxwell_daemon.backends.together import TogetherBackend

        await TogetherBackend().complete(
            _msg(), model="m", max_tokens=10, tools=[{"type": "function"}]
        )
        assert calls[0]["max_tokens"] == 10

    @pytest.mark.asyncio
    async def test_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.together.openai.AsyncOpenAI",
            lambda **_: _fake_client(stream_parts=["a", "b"]),
        )
        from maxwell_daemon.backends.together import TogetherBackend

        parts = [p async for p in TogetherBackend().stream(_msg(), model="m")]
        assert parts == ["a", "b"]

    @pytest.mark.asyncio
    async def test_health_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.together.openai.AsyncOpenAI",
            lambda **_: _fake_client(),
        )
        from maxwell_daemon.backends.together import TogetherBackend

        assert await TogetherBackend().health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOGETHER_API_KEY", "ta-test")

        async def broken_list() -> None:
            raise RuntimeError("down")

        monkeypatch.setattr(
            "maxwell_daemon.backends.together.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(models=SimpleNamespace(list=broken_list)),
        )
        from maxwell_daemon.backends.together import TogetherBackend

        assert await TogetherBackend().health_check() is False

    def test_registered(self) -> None:
        assert "together" in registry.available()


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


class TestOpenRouterBackend:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        with pytest.raises(BackendUnavailableError):
            OpenRouterBackend()

    def test_accepts_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        assert OpenRouterBackend() is not None

    def test_capabilities(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        caps = OpenRouterBackend().capabilities("openai/gpt-4o")
        assert caps.supports_streaming is True

    @pytest.mark.asyncio
    async def test_complete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.openrouter.openai.AsyncOpenAI",
            lambda **_: _fake_client("openrouter answer"),
        )
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        resp = await OpenRouterBackend().complete(_msg(), model="openai/gpt-4o")
        assert resp.content == "openrouter answer"

    @pytest.mark.asyncio
    async def test_complete_with_max_tokens_and_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        calls: list[dict[str, Any]] = []

        async def fake_create(**kw: Any) -> Any:
            calls.append(kw)
            return _fake_response()

        monkeypatch.setattr(
            "maxwell_daemon.backends.openrouter.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
                models=SimpleNamespace(list=AsyncMock(return_value=[])),
            ),
        )
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        await OpenRouterBackend().complete(
            _msg(), model="m", max_tokens=20, tools=[{"type": "function"}]
        )
        assert calls[0]["max_tokens"] == 20

    @pytest.mark.asyncio
    async def test_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.openrouter.openai.AsyncOpenAI",
            lambda **_: _fake_client(stream_parts=["p", "q"]),
        )
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        parts = [p async for p in OpenRouterBackend().stream(_msg(), model="m")]
        assert parts == ["p", "q"]

    @pytest.mark.asyncio
    async def test_health_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")
        monkeypatch.setattr(
            "maxwell_daemon.backends.openrouter.openai.AsyncOpenAI",
            lambda **_: _fake_client(),
        )
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        assert await OpenRouterBackend().health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-test")

        async def broken_list() -> None:
            raise RuntimeError("down")

        monkeypatch.setattr(
            "maxwell_daemon.backends.openrouter.openai.AsyncOpenAI",
            lambda **_: SimpleNamespace(models=SimpleNamespace(list=broken_list)),
        )
        from maxwell_daemon.backends.openrouter import OpenRouterBackend

        assert await OpenRouterBackend().health_check() is False

    def test_registered(self) -> None:
        assert "openrouter" in registry.available()
