"""Multi-turn agent loop backend backed by Ollama (local inference).

Ollama exposes an OpenAI-compatible endpoint (``/v1/chat/completions``), so
we speak the same JSON shape as OpenAI: the MCP registry emits tool
schemas via ``to_openai()`` and the response's ``message.tool_calls`` drive
the next turn.

Why a separate backend instead of a flag on :class:`AgentLoopBackend`?
Keeping them split means the Anthropic loop doesn't grow a set of Ollama-
only branches and vice versa — easier to reason about, easier to delete
if one provider goes away. When their shapes converge we can collapse.

DRY: tool handlers come from ``maxwell_daemon.tools.build_default_registry``
(shared with the Anthropic loop). One registry, many providers.

LOD: the HTTP client is injected (any async-post callable with a json
kwarg will do). Tests pass a recorder; prod passes ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json as _json
import os
import time
from collections.abc import AsyncIterator
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.condensation import Condenser
from maxwell_daemon.backends.registry import registry
from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.gh.ci_patterns import detect_ci_profile
from maxwell_daemon.gh.repo_schematic import build_repo_schematic
from maxwell_daemon.tools import build_default_registry

__all__ = [
    "OllamaAgentLoopBackend",
    "WallClockTimeoutError",
]


# Reuse the same wall-clock exception the Anthropic loop uses. Import lazily
# so this module stays importable even if the other backend is removed.
from maxwell_daemon.backends.agent_loop import WallClockTimeoutError


class _PostClient(Protocol):
    """Minimal protocol for an injected HTTP client — only .post is needed."""

    async def post(
        self,
        url: str,
        json: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any: ...

    async def aclose(self) -> None: ...


#: Default URL of Ollama's OpenAI-compatible endpoint.
DEFAULT_BASE_URL = "http://localhost:11434/v1"

#: Purpose-built for agentic code editing, 24B, runs on 24 GB VRAM.
DEFAULT_MODEL = "devstral"


class OllamaAgentLoopBackend(ILLMBackend):
    """Multi-turn tool-use loop against a local Ollama server."""

    name = "ollama-agent-loop"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        max_turns: int = 150,
        timeout: float = 600.0,
        workspace_dir: str | None = None,
        wall_clock_timeout_seconds: float | None = None,
        enable_prompt_caching: bool = False,
        registry_factory: Any = None,
        client: _PostClient | None = None,
        mcp_manager: Any | None = None,
        condenser: Condenser | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.default_model = model
        self._max_turns = max_turns
        self._workspace = workspace_dir
        self._wall_clock = wall_clock_timeout_seconds
        self._enable_cache = enable_prompt_caching
        self._registry_factory = registry_factory or build_default_registry
        self._client: _PostClient = client or _default_httpx_client(timeout)
        self._mcp_manager = mcp_manager
        self._condenser = condenser

    # ── ILLMBackend surface ──────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        workspace_dir: str | None = None,
        max_turns: int | None = None,
        **kwargs: Any,
    ) -> BackendResponse:
        effective_workspace = self._resolve_workspace(workspace_dir)
        effective_model = model or self.default_model
        effective_max_turns = max_turns if max_turns is not None else self._max_turns

        tool_registry = self._registry_factory(
            effective_workspace, dry_run=kwargs.get("dry_run", False)
        )
        if self._mcp_manager is not None:
            self._mcp_manager.attach_tools(tool_registry)
        tool_defs = tool_registry.to_openai()

        sdk_messages = self._build_messages(messages, workspace=effective_workspace)

        deadline: float | None = None
        if self._wall_clock is not None:
            deadline = time.monotonic() + self._wall_clock

        total_usage = TokenUsage()
        last_text = ""

        for turn in range(effective_max_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise WallClockTimeoutError(
                    f"agent loop exceeded wall-clock timeout of {self._wall_clock}s"
                )

            if self._condenser is not None and self._condenser.should_condense(
                total_usage.prompt_tokens
            ):
                sdk_messages = await self._condenser.condense(sdk_messages)

            payload = {
                "model": effective_model,
                "messages": sdk_messages,
                "tools": tool_defs,
                "temperature": temperature,
            }
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens

            resp = await self._client.post(
                f"{self.base_url}/chat/completions", json=payload
            )
            resp.raise_for_status()
            body = resp.json()

            usage = body.get("usage") or {}
            total_usage = total_usage + TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                total_tokens=int(usage.get("total_tokens", 0)),
            )

            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or "stop"

            if message.get("content"):
                last_text = str(message["content"])

            tool_calls = message.get("tool_calls") or []
            if finish_reason == "tool_calls" and tool_calls:
                sdk_messages.append(message)  # assistant turn
                for call in tool_calls:
                    fn = call.get("function") or {}
                    args = _parse_args(fn.get("arguments"))
                    result = await tool_registry.invoke(str(fn.get("name", "")), args)
                    content = (
                        f"ERROR: {result.content}"
                        if result.is_error
                        else result.content
                    )
                    sdk_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(call.get("id", "")),
                            "content": content,
                        }
                    )
                continue

            return BackendResponse(
                content=last_text,
                finish_reason=finish_reason,
                usage=total_usage,
                model=str(body.get("model", effective_model)),
                backend=self.name,
                raw={"turns": turn + 1},
            )

        raise RuntimeError(
            f"agent loop exceeded max_turns={effective_max_turns} without end_turn"
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        resp = await self.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        yield resp.content

    def _resolve_workspace(self, workspace_dir: str | None) -> Path:
        workspace_raw = workspace_dir or self._workspace
        if not workspace_raw:
            raise PreconditionError(
                "OllamaAgentLoopBackend requires workspace_dir; refusing to fall back to cwd"
            )
        return Path(workspace_raw).resolve()

    async def health_check(self) -> bool:
        with suppress(Exception):
            resp = await self._client.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.default_model,
                    "messages": [{"role": "user", "content": "."}],
                    "max_tokens": 1,
                },
            )
            return 200 <= getattr(resp, "status_code", 500) < 300
        return False

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=128_000,
            is_local=True,
            cost_per_1k_input_tokens=0.0,
            cost_per_1k_output_tokens=0.0,
        )

    async def aclose(self) -> None:
        """Close the injected HTTP client. Safe to call multiple times.

        Without this the underlying :class:`httpx.AsyncClient` leaks TCP
        sockets when the daemon cycles backends between tasks.
        """
        close = getattr(self._client, "aclose", None) or getattr(
            self._client, "close", None
        )
        if close is None:
            return
        with suppress(Exception):
            result = close()
            if hasattr(result, "__await__"):
                await result

    # ── System prompt + message assembly ────────────────────────────────────

    def _build_messages(
        self, messages: list[Message], *, workspace: Path
    ) -> list[dict[str, Any]]:
        """Build the ``messages`` payload with system blocks prepended.

        Ollama's OpenAI-compatible endpoint uses the standard role-based
        messages. We prepend a single system message with workspace hints,
        CI profile, and repo map. No prompt-caching envelope (Ollama is
        local — caching is a cloud-bill concern).
        """
        system_texts: list[str] = [
            m.content for m in messages if m.role is MessageRole.SYSTEM
        ]
        conversation = [
            {"role": m.role.value, "content": m.content}
            for m in messages
            if m.role is not MessageRole.SYSTEM
        ]

        system_parts: list[str] = [t for t in system_texts if t]
        system_parts.append(
            "You have tools to read, write, edit, search files and run bash inside the workspace. "
            "All paths are relative to the workspace root."
        )
        system_parts.append(f"Workspace: {workspace}")

        with suppress(Exception):
            ci_block = detect_ci_profile(workspace).to_prompt()
            if ci_block:
                system_parts.append(ci_block)

        with suppress(Exception):
            map_block = build_repo_schematic(workspace).to_prompt(max_chars=2000)
            if map_block:
                system_parts.append(map_block)

        return [{"role": "system", "content": "\n\n".join(system_parts)}, *conversation]


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_args(raw: Any) -> dict[str, Any]:
    """OpenAI-spec tool arguments are a JSON string; Ollama often agrees."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _default_httpx_client(timeout: float) -> _PostClient:
    """Lazy import so the backend module stays importable without httpx."""
    import httpx

    return httpx.AsyncClient(timeout=timeout)  # type: ignore[return-value]


registry.register("ollama-agent-loop", OllamaAgentLoopBackend)
