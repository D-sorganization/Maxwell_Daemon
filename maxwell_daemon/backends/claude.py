"""Anthropic Claude backend adapter."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import anthropic
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
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.pricing import get_rates
from maxwell_daemon.backends.registry import registry

# Per-model context-window sizes (tokens).  Pricing lives in the central table
# at :mod:`maxwell_daemon.backends.pricing`.
_MODEL_CONTEXT: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-haiku-latest": 200_000,
}


class ClaudeBackend(ILLMBackend):
    name = "claude"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise BackendUnavailableError(
                "ANTHROPIC_API_KEY not set and no api_key passed"
            )
        self._client = anthropic.AsyncAnthropic(
            api_key=key, base_url=base_url, timeout=timeout
        )

    def _split_system(
        self, messages: list[Message]
    ) -> tuple[str | None, list[dict[str, Any]]]:
        system: str | None = None
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role is MessageRole.SYSTEM:
                system = m.content if system is None else f"{system}\n\n{m.content}"
                continue
            out.append({"role": m.role.value, "content": m.content})
        return system, out

    @retry(
        retry=retry_if_exception_type(
            (anthropic.APIConnectionError, anthropic.RateLimitError)
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
        system, msgs = self._split_system(messages)
        resp = await self._client.messages.create(
            model=model,
            messages=msgs,  # type: ignore[arg-type]
            system=system or anthropic.NOT_GIVEN,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            tools=tools or anthropic.NOT_GIVEN,  # type: ignore[arg-type]
            **kwargs,
        )
        text_parts = [
            getattr(b, "text", "")
            for b in resp.content
            if getattr(b, "type", None) == "text"
        ]
        # Extract usage fields once to avoid repeating the resp.usage chain.
        usage = resp.usage
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        return BackendResponse(
            content="".join(text_parts),
            finish_reason=resp.stop_reason or "stop",
            usage=TokenUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
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
        system, msgs = self._split_system(messages)
        async with self._client.messages.stream(
            model=model,
            messages=msgs,  # type: ignore[arg-type]
            system=system or anthropic.NOT_GIVEN,  # type: ignore[arg-type]
            temperature=temperature,
            max_tokens=max_tokens or 4096,
            tools=tools or anthropic.NOT_GIVEN,  # type: ignore[arg-type]
            **kwargs,
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def health_check(self) -> bool:
        try:
            await self._client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            return True
        except Exception:
            return False

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates("claude", model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision=True,
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 200_000),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("claude", ClaudeBackend)
