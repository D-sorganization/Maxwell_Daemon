"""Groq backend adapter.

Uses the ``groq`` SDK (OpenAI-compatible wire format, ultra-low latency).
Set ``GROQ_API_KEY`` (or pass ``api_key``) to authenticate.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from importlib import import_module
from typing import Any, cast

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.backends.pricing import get_rates
from maxwell_daemon.backends.registry import registry

_MODEL_CONTEXT: dict[str, int] = {
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-8b-instant": 128_000,
    "mixtral-8x7b-32768": 32_768,
    "gemma2-9b-it": 8_192,
}

# Models that support tool use on Groq.
_TOOL_USE_MODELS: frozenset[str] = frozenset(
    {
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    }
)


class GroqBackend(ILLMBackend):
    name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("GROQ_API_KEY")
        if not key:
            raise BackendUnavailableError("GROQ_API_KEY not set and no api_key passed")
        try:
            groq_sdk = cast(Any, import_module("groq"))
        except ModuleNotFoundError as exc:
            raise BackendUnavailableError(
                "groq SDK not installed; install maxwell-daemon[groq]"
            ) from exc
        self._client = groq_sdk.AsyncGroq(api_key=key, timeout=timeout)
        self._retryable_errors: tuple[type[BaseException], ...] = (
            cast(type[BaseException], groq_sdk.APIConnectionError),
            cast(type[BaseException], groq_sdk.RateLimitError),
        )

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
        params: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if tools and model in _TOOL_USE_MODELS:
            params["tools"] = tools
        params.update(kwargs)

        delay_seconds = 1.0
        for attempt in range(3):
            try:
                resp = await self._client.chat.completions.create(**params)
                break
            except Exception as exc:
                if not isinstance(exc, self._retryable_errors) or attempt == 2:
                    raise
                await asyncio.sleep(delay_seconds)
                delay_seconds = min(delay_seconds * 2, 10.0)
        choice = resp.choices[0]
        usage = resp.usage
        return BackendResponse(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason or "stop",
            usage=TokenUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            model=resp.model,
            backend=self.name,
            raw=resp.model_dump(),
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
        params: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    async def health_check(self) -> bool:
        try:
            await self._client.models.list()
            return True
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list()
            return [m.id for m in page.data]
        except Exception:
            return []

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates(self.name, model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=model in _TOOL_USE_MODELS,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 32_768),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("groq", GroqBackend)
