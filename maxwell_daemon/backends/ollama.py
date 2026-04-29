"""Ollama backend adapter for local model inference.

Zero API cost — the whole point is letting users run agents against models on their
own hardware. We use httpx directly to avoid depending on the `ollama` package.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.backends.registry import registry


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip().rstrip("/")
    if not value:
        return "http://localhost:11434"
    if "://" not in value:
        value = f"http://{value}"
    return value


class OllamaBackend(ILLMBackend):
    name = "ollama"

    def __init__(
        self,
        endpoint: str | None = None,
        base_url: str | None = None,
        timeout: float = 300.0,
        **_extra: Any,  # absorb router-injected config fields (tier_map, etc.)
    ) -> None:
        self._endpoint = _normalize_endpoint(
            endpoint or base_url or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
        )
        self._client = httpx.AsyncClient(timeout=timeout)

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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature, **kwargs.get("options", {})},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if tools:
            payload["tools"] = tools

        try:
            resp = await self._client.post(f"{self._endpoint}/api/chat", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise BackendUnavailableError(f"Ollama request failed: {e}") from e

        data = resp.json()
        return BackendResponse(
            content=data.get("message", {}).get("content", ""),
            finish_reason="stop" if data.get("done") else "length",
            usage=TokenUsage(
                prompt_tokens=data.get("prompt_eval_count", 0),
                completion_tokens=data.get("eval_count", 0),
                total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
            ),
            model=data.get("model", model),
            backend=self.name,
            raw=data,
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
        del tools, kwargs
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role.value, "content": m.content} for m in messages],
            "stream": True,
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens

        async with self._client.stream("POST", f"{self._endpoint}/api/chat", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                msg = data.get("message", {})
                if content := msg.get("content"):
                    yield content

    async def health_check(self) -> bool:
        try:
            r = await self._client.get(f"{self._endpoint}/api/tags", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        try:
            r = await self._client.get(f"{self._endpoint}/api/tags", timeout=5.0)
            r.raise_for_status()
            data = r.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def capabilities(self, model: str) -> BackendCapabilities:
        # Context varies per model; 8K is a conservative default. Users can override
        # via the model's Modelfile and query /api/show for the real value.
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use="llama3" in model.lower() or "qwen" in model.lower(),
            supports_vision="llava" in model.lower() or "vision" in model.lower(),
            supports_system_prompt=True,
            max_context_tokens=8_192,
            is_local=True,
            cost_per_1k_input_tokens=0.0,
            cost_per_1k_output_tokens=0.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


registry.register("ollama", OllamaBackend)
