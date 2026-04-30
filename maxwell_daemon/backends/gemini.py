"""Google Gemini backend adapter.

Uses the ``google-generativeai`` SDK to communicate with Google's Gemini API.
Set ``GOOGLE_API_KEY`` (or pass ``api_key``) to authenticate.
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
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.pricing import get_rates
from maxwell_daemon.backends.registry import registry

_MODEL_CONTEXT: dict[str, int] = {
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 2_097_152,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-flash-lite": 1_048_576,
}


def _to_gemini_role(role: MessageRole) -> str:
    if role is MessageRole.ASSISTANT:
        return "model"
    return "user"


class GeminiBackend(ILLMBackend):
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise BackendUnavailableError("GOOGLE_API_KEY not set and no api_key passed")
        try:
            self._genai = cast(Any, import_module("google.generativeai"))
        except ModuleNotFoundError as exc:
            raise BackendUnavailableError(
                "google-generativeai SDK not installed; install maxwell-daemon[gemini]"
            ) from exc
        self._genai.configure(api_key=key)
        self._timeout = timeout

    def _build_contents(self, messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []
        for m in messages:
            if m.role is MessageRole.SYSTEM:
                system_parts.append(m.content)
            else:
                contents.append({"role": _to_gemini_role(m.role), "parts": [{"text": m.content}]})
        return "\n\n".join(system_parts) or None, contents

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
        del tools
        system_instruction, contents = self._build_contents(messages)
        gen_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            gen_config["max_output_tokens"] = max_tokens

        gmodel = self._genai.GenerativeModel(
            model_name=model,
            system_instruction=system_instruction,
            generation_config=self._genai.types.GenerationConfig(**gen_config),
        )
        resp = await gmodel.generate_content_async(contents, **kwargs)
        usage_meta = getattr(resp, "usage_metadata", None)
        prompt_tokens = getattr(usage_meta, "prompt_token_count", 0) or 0
        completion_tokens = getattr(usage_meta, "candidates_token_count", 0) or 0
        return BackendResponse(
            content=resp.text,
            finish_reason="stop",
            usage=TokenUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            model=model,
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
        del tools
        system_instruction, contents = self._build_contents(messages)
        gen_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            gen_config["max_output_tokens"] = max_tokens

        gmodel = self._genai.GenerativeModel(
            model_name=model,
            system_instruction=system_instruction,
            generation_config=self._genai.types.GenerationConfig(**gen_config),
        )
        async for chunk in await gmodel.generate_content_async(contents, stream=True, **kwargs):
            if chunk.text:
                yield chunk.text

    async def health_check(self) -> bool:
        try:
            gmodel = self._genai.GenerativeModel(model_name="gemini-1.5-flash")
            await gmodel.generate_content_async("ping")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def list_models(self) -> list[str]:
        try:
            return [m.name for m in self._genai.list_models()]
        except Exception:  # noqa: BLE001
            return []

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates(self.name, model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision=True,
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 1_048_576),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("gemini", GeminiBackend)
