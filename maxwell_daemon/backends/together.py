"""Together AI backend adapter.

Together AI exposes an OpenAI-compatible API at https://api.together.xyz/v1.
It hosts a wide range of open-weight models with competitive pricing.

Set ``TOGETHER_API_KEY`` (or pass ``api_key``) to authenticate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import openai
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

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

_BASE_URL = "https://api.together.xyz/v1"

_MODEL_CONTEXT: dict[str, int] = {
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": 131_072,
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": 131_072,
    "mistralai/Mixtral-8x7B-Instruct-v0.1": 32_768,
    "Qwen/Qwen2.5-72B-Instruct-Turbo": 32_768,
}


class TogetherBackend(ILLMBackend):
    name = "together"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("TOGETHER_API_KEY")
        if not key:
            raise BackendUnavailableError("TOGETHER_API_KEY not set and no api_key passed")
        self._client = openai.AsyncOpenAI(
            api_key=key,
            base_url=base_url,
            timeout=timeout,
        )

    @retry(
        retry=retry_if_exception_type((openai.APIConnectionError, openai.RateLimitError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
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
        except Exception:  # noqa: BLE001
            return False

    async def list_models(self) -> list[str]:
        try:
            page = await self._client.models.list()
            return [m.id for m in page.data]
        except Exception:  # noqa: BLE001
            return []

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates(self.name, model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision="vision" in model.lower(),
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 32_768),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("together", TogetherBackend)
