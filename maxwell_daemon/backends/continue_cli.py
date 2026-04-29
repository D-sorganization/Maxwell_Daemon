"""Continue CLI backend — shells out to Continue.dev's `cn` binary.

Continue.dev ships a `cn` binary (newer 2025) that wraps a local
``.continue/config.yaml`` + assistant model. We invoke it one-shot via
``cn ask "<prompt>"`` and surface the stdout verbatim as the response.

The ``model`` argument to :meth:`complete` is ignored: Continue picks
its model from its own config. We record whatever the CLI reports (or
the caller's value as a fallback) in :attr:`BackendResponse.model`.
"""

from __future__ import annotations

import asyncio
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

__all__ = ["ContinueCLIBackend"]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


async def _default_runner(
    *argv: str, cwd: str | None = None, stdin: bytes | None = None
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if stdin is None:
        stdout, stderr = await proc.communicate()
    else:
        stdout, stderr = await proc.communicate(stdin)
    return proc.returncode or 0, stdout, stderr


class ContinueCLIBackend(ILLMBackend):
    name = "continue-cli"

    def __init__(
        self,
        *,
        runner: RunnerFn | None = None,
        binary: str = "cn",
        assistant: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._run = runner or _default_runner
        self._binary = binary
        self._assistant = assistant
        self._timeout = timeout

    def _format_prompt(self, messages: list[Message]) -> str:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for m in messages:
            if m.role is MessageRole.SYSTEM:
                system_parts.append(m.content)
            else:
                user_parts.append(m.content)
        # `cn ask` is one-shot: concatenate system prefix then user turns.
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
        del temperature, max_tokens, tools, kwargs
        prompt = self._format_prompt(messages)
        argv: list[str] = [self._binary, "ask", prompt]
        if self._assistant:
            argv.extend(["--assistant", self._assistant])
        try:
            rc, stdout, stderr = await asyncio.wait_for(self._run(*argv), timeout=self._timeout)
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            raise BackendUnavailableError(f"cn CLI unreachable: {e}") from e
        if rc != 0:
            detail = stderr.decode(errors="replace").strip() or "cn ask failed"
            import structlog

            structlog.get_logger(__name__).error("cn ask failed", rc=rc, stderr=detail[-32768:])
            raise BackendUnavailableError(f"cn ask rc={rc}: {detail[:500]}")

        # Continue picks the model from its own config; we record what the
        # caller requested since the CLI doesn't emit a machine-readable
        # model field in plain-text output.
        content = stdout.decode(errors="replace")
        return BackendResponse(
            content=content,
            finish_reason="stop",
            usage=TokenUsage(),
            model=model,
            backend=self.name,
            raw={"stdout": content},
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
        # One-shot: delegate to complete() and yield once.
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
        del model
        # Continue manages tool use internally; we can't pass function
        # schemas through `cn ask`.
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=False,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=128_000,
            is_local=False,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("continue-cli", ContinueCLIBackend)
