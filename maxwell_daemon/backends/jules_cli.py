"""Jules CLI backend — shells out to `jules ask` to leverage Jules system subscriptions."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.registry import registry

__all__ = ["JulesCLIBackend"]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


async def _default_runner(
    *argv: str, cwd: str | None = None, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


class JulesCLIBackend(ILLMBackend):
    name = "jules-cli"

    def __init__(
        self,
        *,
        runner: RunnerFn | None = None,
        binary: str = "jules",
        timeout: float = 300.0,
    ) -> None:
        self._run = runner or _default_runner
        self._binary = binary
        self._timeout = timeout

    def _format_prompt(self, messages: list[Message]) -> str:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for m in messages:
            if m.role is MessageRole.SYSTEM:
                system_parts.append(m.content)
            else:
                user_parts.append(m.content)
        head = "\n\n".join(system_parts).strip()
        body = "\n\n".join(user_parts).strip()
        return f"{head}\n\n{body}" if head else body

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
        prompt = self._format_prompt(messages)
        argv = [
            self._binary,
            "ask",
            prompt,
            "--output-format",
            "json",
        ]
        try:
            rc, stdout, stderr = await asyncio.wait_for(
                self._run(*argv), timeout=self._timeout
            )
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            raise BackendUnavailableError(f"jules CLI unreachable: {e}") from e
        if rc != 0:
            detail = stderr.decode(errors="replace").strip() or "jules failed"
            raise BackendUnavailableError(f"jules rc={rc}: {detail[:500]}")

        try:
            payload = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as e:
            raise BackendUnavailableError(f"jules returned non-JSON: {e}") from e

        content = (
            payload.get("result") or payload.get("output") or payload.get("text") or ""
        )
        usage = payload.get("usage", {})
        returned_model = payload.get("model", model)
        return BackendResponse(
            content=str(content),
            finish_reason=str(payload.get("stop_reason", "stop")),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("input_tokens", 0)),
                completion_tokens=int(usage.get("output_tokens", 0)),
                total_tokens=int(usage.get("input_tokens", 0))
                + int(usage.get("output_tokens", 0)),
            ),
            model=returned_model,
            backend=self.name,
            raw=payload,
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
        resp = await self.complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )
        yield resp.content

    async def health_check(self) -> bool:
        try:
            rc, _, _ = await self._run(self._binary, "--version")
        except (FileNotFoundError, OSError):
            return False
        return rc == 0

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=100_000,
            is_local=False,
            cost_per_1k_input_tokens=0.0,
            cost_per_1k_output_tokens=0.0,
        )


registry.register("jules-cli", JulesCLIBackend)
