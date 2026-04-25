"""Claude Code CLI backend — shells out to `claude -p` for those who want
the tool-use sandbox the CLI ships with.

We use ``--output-format json`` so the response shape is stable. The CLI
inherits its own auth from the user's ``claude`` login; we don't manage keys
here.
"""

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

__all__ = ["ClaudeCodeCLIBackend"]

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


class ClaudeCodeCLIBackend(ILLMBackend):
    name = "claude-code-cli"

    def __init__(
        self,
        *,
        runner: RunnerFn | None = None,
        binary: str = "claude",
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
        # Squish the conversation into a single prompt since `claude -p` is
        # one-shot: put system instructions first, then the user turn(s).
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
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
        ]
        try:
            rc, stdout, stderr = await asyncio.wait_for(
                self._run(*argv), timeout=self._timeout
            )
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            raise BackendUnavailableError(f"claude CLI unreachable: {e}") from e
        if rc != 0:
            detail = stderr.decode(errors="replace").strip() or "claude -p failed"
            import structlog

            structlog.get_logger(__name__).error(
                "claude -p failed", rc=rc, stderr=detail[-32768:]
            )
            raise BackendUnavailableError(f"claude -p rc={rc}: {detail[:1024]}")

        try:
            payload = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError as e:
            raise BackendUnavailableError(f"claude -p returned non-JSON: {e}") from e

        # Accept a few shapes so this adapter is tolerant of upstream changes.
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
        # One-shot: call complete() and yield once. True streaming would need
        # `--output-format stream-json` — future work.
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
        # Claude Code's built-in tool use is the main reason to pick this
        # backend over the raw API adapter.
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=True,
            supports_system_prompt=True,
            max_context_tokens=200_000,
            is_local=False,
            # No authoritative pricing for the CLI — rely on the user's
            # subscription / API key cost accounting.
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("claude-code-cli", ClaudeCodeCLIBackend)
