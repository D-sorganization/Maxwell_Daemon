"""Mistral AI backend adapter (La Plateforme).

Uses the ``mistralai`` SDK. Set ``MISTRAL_API_KEY`` (or pass ``api_key``) to authenticate.
"""

from __future__ import annotations

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
    "mistral-large-latest": 131_072,
    "mistral-large": 131_072,
    "mistral-small-latest": 131_072,
    "mistral-small": 131_072,
    "codestral-latest": 256_000,
    "open-mistral-nemo": 131_072,
    "open-mixtral-8x22b": 65_536,
}


class MistralBackend(ILLMBackend):
    name = "mistral"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("MISTRAL_API_KEY")
        if not key:
            raise BackendUnavailableError(
                "MISTRAL_API_KEY not set and no api_key passed"
            )
        try:
            mistral_sdk = cast(Any, import_module("mistralai"))
        except ModuleNotFoundError as exc:
            raise BackendUnavailableError(
                "mistralai SDK not installed; install maxwell-daemon[mistral]"
            ) from exc
        self._client = mistral_sdk.Mistral(api_key=key, timeout_ms=int(timeout * 1000))

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

        resp = await self._client.chat.complete_async(**params)
        choice = resp.choices[0]
        usage = resp.usage
        return BackendResponse(
            content=choice.message.content or "",
            finish_reason=str(choice.finish_reason) if choice.finish_reason else "stop",
            usage=TokenUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
                total_tokens=usage.total_tokens if usage else 0,
            ),
            model=resp.model,
            backend=self.name,
            raw={},
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
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        async for event in await self._client.chat.stream_async(**params):
            delta = event.data.choices[0].delta
            if delta and delta.content:
                yield delta.content

    async def health_check(self) -> bool:
        try:
            await self._client.models.list_async()
            return True
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            result = await self._client.models.list_async()
            return [m.id for m in result.data]
        except Exception:
            return []

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates(self.name, model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision="pixtral" in model.lower(),
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 131_072),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("mistral", MistralBackend)
