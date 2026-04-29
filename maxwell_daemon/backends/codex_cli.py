"""Codex CLI backend — shells out to OpenAI's `codex exec` for headless use.

The `codex` binary (openai/codex, Rust) ships three approval modes — ``suggest``,
``auto-edit``, ``full-auto`` — and a `--profile` flag to swap model/provider per
invocation. We use `codex exec` (one-shot/CI mode) and pipe the concatenated
prompt over stdin. Auth is whatever the user's `codex` login already uses; we
don't manage keys here.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

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

__all__ = ["CodexCLIBackend"]

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]

ApprovalMode = Literal["suggest", "auto-edit", "full-auto"]


def _ignore_unused(*_values: object) -> None:
    return None


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
    stdout, stderr = await proc.communicate(input=stdin)
    return proc.returncode or 0, stdout, stderr


class CodexCLIBackend(ILLMBackend):
    name = "codex-cli"

    def __init__(
        self,
        *,
        runner: RunnerFn | None = None,
        binary: str = "codex",
        approval: ApprovalMode = "suggest",
        profile: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._run = runner or _default_runner
        self._binary = binary
        self._approval: ApprovalMode = approval
        self._profile = profile
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
        _ignore_unused(temperature, max_tokens, tools, kwargs)
        prompt = self._format_prompt(messages)
        if not prompt:
            raise BackendUnavailableError("codex-cli: refusing to send empty prompt")

        argv: list[str] = [self._binary, "exec", "--approval", self._approval]
        if self._profile:
            argv += ["--profile", self._profile]
        argv += ["--model", model]

        try:
            rc, stdout, stderr = await asyncio.wait_for(
                self._run(*argv, stdin=prompt.encode()),
                timeout=self._timeout,
            )
        except (FileNotFoundError, TimeoutError, asyncio.TimeoutError) as e:
            raise BackendUnavailableError(f"codex CLI unreachable: {e}") from e

        if rc != 0:
            detail = stderr.decode(errors="replace").strip() or "codex exec failed"
            import structlog

            structlog.get_logger(__name__).error("codex exec failed", rc=rc, stderr=detail[-32768:])
            raise BackendUnavailableError(f"codex exec rc={rc}: {detail[:500]}")

        content = stdout.decode(errors="replace").strip()
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
        _ignore_unused(tools, kwargs)
        # `codex exec` is one-shot; true token streaming would need a different
        # codex subcommand. For now we deliver the full response as one chunk.
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
        _ignore_unused(model)
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=128_000,
            is_local=False,
            # Codex rides the user's own OpenAI subscription/API key — no
            # pricing owned by this adapter.
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("codex-cli", CodexCLIBackend)
