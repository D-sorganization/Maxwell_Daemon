"""OpenRouter backend adapter.

OpenRouter exposes an OpenAI-compatible API at https://openrouter.ai/api/v1
providing access to ~200 models from multiple providers under a single API key.

Set ``OPENROUTER_API_KEY`` (or pass ``api_key``) to authenticate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import openai

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.backends.registry import registry

_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterBackend(ILLMBackend):
    """OpenAI-compatible adapter for OpenRouter.

    Because OpenRouter proxies ~200 models, capabilities and context windows are
    model-dependent and not statically enumerable.  All models are assumed to support
    streaming; tool-use and vision support depends on the underlying model.
    """

    name = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        timeout: float = 120.0,
        site_url: str | None = None,
        site_name: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise BackendUnavailableError("OPENROUTER_API_KEY not set and no api_key passed")

        default_headers: dict[str, str] = {}
        if site_url:
            default_headers["HTTP-Referer"] = site_url
        if site_name:
            default_headers["X-Title"] = site_name

        self._client = openai.AsyncOpenAI(
            api_key=key,
            base_url=base_url,
            timeout=timeout,
            default_headers=default_headers or None,
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
        if tools:
            params["tools"] = tools
        params.update(kwargs)

        resp = await self._client.chat.completions.create(**params)
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
        del tools
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
        # OpenRouter hosts hundreds of models; we can't enumerate them all.
        # Return sensible defaults — vision/tool-use depend on the underlying model.
        vision_hint = any(kw in model.lower() for kw in ("vision", "gpt-4o", "claude-3", "gemini"))
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision=vision_hint,
            supports_system_prompt=True,
            max_context_tokens=128_000,
            is_local=False,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("openrouter", OpenRouterBackend)
