"""OpenAI backend adapter (also covers Azure OpenAI and OpenAI-compatible endpoints).

Codex CLI and any OpenAI-compatible server (vLLM, LM Studio, LocalAI) can point this
adapter at a custom `base_url` — the wire protocol is the same.
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

# Per-model context-window sizes (tokens).  Pricing lives in the central table
# at :mod:`maxwell_daemon.backends.pricing`.
_MODEL_CONTEXT: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
}


class OpenAIBackend(ILLMBackend):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        organization: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        # For fully local OpenAI-compatible servers a dummy key is fine, but we
        # still require the caller to opt in explicitly.
        if not key and not base_url:
            raise BackendUnavailableError(
                "OPENAI_API_KEY not set and no base_url passed (pass base_url for local servers)"
            )
        self._client = openai.AsyncOpenAI(
            api_key=key or "not-needed",
            base_url=base_url,
            organization=organization,
            timeout=timeout,
        )

    @retry(
        retry=retry_if_exception_type(
            (openai.APIConnectionError, openai.RateLimitError)
        ),
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
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
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
        params: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": m.role.value, "content": m.content} for m in messages
            ],
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if tools:
            params["tools"] = tools
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

    def capabilities(self, model: str) -> BackendCapabilities:
        # Look up pricing under ``self.name`` so subclasses (e.g. AzureOpenAIBackend)
        # hit their own provider entry in the pricing table rather than always
        # routing through the ``openai`` entry.
        price_in, price_out = get_rates(self.name, model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision="gpt-4o" in model or "o1" in model,
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 128_000),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("openai", OpenAIBackend)
