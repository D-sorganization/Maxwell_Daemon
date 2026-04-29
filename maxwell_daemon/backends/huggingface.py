"""HuggingFace Inference API backend adapter.

Supports HuggingFace's serverless inference API and dedicated endpoints via the
OpenAI-compatible ``/v1/chat/completions`` endpoint introduced in the Inference API.

Set ``HUGGINGFACE_API_KEY`` (or ``HF_TOKEN``, or pass ``api_key``) to authenticate.
The ``base_url`` defaults to the public serverless inference endpoint; override it
for dedicated/private TGI deployments (e.g., ``http://my-tgi-host:8080/v1``).
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

_BASE_URL = "https://api-inference.huggingface.co/v1"


class HuggingFaceBackend(ILLMBackend):
    """HuggingFace Inference API (OpenAI-compatible).

    Works with both the public serverless endpoint and private TGI / Text-Generation
    WebUI deployments that expose an OpenAI-compatible ``/v1/chat/completions`` path.
    """

    name = "huggingface"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("HUGGINGFACE_API_KEY") or os.environ.get("HF_TOKEN")
        # For private/local TGI instances no key may be needed, but we require
        # explicit opt-in for the public HF endpoint.
        if not key and base_url == _BASE_URL:
            raise BackendUnavailableError(
                "HUGGINGFACE_API_KEY (or HF_TOKEN) not set and no api_key passed"
            )
        self._client = openai.AsyncOpenAI(
            api_key=key or "not-needed",
            base_url=base_url,
            timeout=timeout,
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
        vision_hint = any(kw in model.lower() for kw in ("vision", "vl", "idefics"))
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=False,
            supports_vision=vision_hint,
            supports_system_prompt=True,
            max_context_tokens=8_192,
            is_local=False,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("huggingface", HuggingFaceBackend)
