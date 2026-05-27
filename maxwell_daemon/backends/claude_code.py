"""Claude Code CLI backend — shells out to `claude -p` for those who want
the tool-use sandbox the CLI ships with.

We use ``--output-format stream-json`` so we can stream responses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import uuid
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

logger = logging.getLogger(__name__)

RunnerFn = Callable[..., Awaitable[tuple[int, bytes, bytes]]]


def _ignore_unused(*_values: object) -> None:
    return None


async def _default_runner(
    *argv: str,
    cwd: str | None = None,
    stdin: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    command = (
        ["cmd", "/c", *argv] if os.name == "nt" and (not argv or argv[0] != "cmd") else list(argv)
    )

    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if stdin is None:
        stdout, stderr = await proc.communicate()
    else:
        stdout, stderr = await proc.communicate(input=stdin)
    return proc.returncode or 0, stdout, stderr


@contextlib.asynccontextmanager
async def TemporaryPromptFiles(  # noqa: N802
    system_prompts: list[str],
) -> AsyncIterator[tuple[str | None, str | None]]:
    """Context manager to write system prompts to files under /tmp with 0o600 permissions.

    Falls back to system temp directory if /tmp is not writable.
    """
    tmp_dir = "/tmp"
    if not os.path.isdir(tmp_dir):
        with contextlib.suppress(OSError):
            os.makedirs(tmp_dir, exist_ok=True)
        if not os.path.isdir(tmp_dir):
            tmp_dir = tempfile.gettempdir()

    system_prompt_path: str | None = None
    append_system_prompt_path: str | None = None

    try:
        if len(system_prompts) > 0:
            first_prompt = system_prompts[0]
            system_prompt_path = os.path.join(tmp_dir, f"maxwell-system-prompt-{uuid.uuid4()}.txt")
            fd = os.open(system_prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(first_prompt)

        if len(system_prompts) > 1:
            remaining_prompt = "\n\n".join(system_prompts[1:])
            append_system_prompt_path = os.path.join(
                tmp_dir, f"maxwell-append-system-prompt-{uuid.uuid4()}.txt"
            )
            fd = os.open(append_system_prompt_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(remaining_prompt)

        yield system_prompt_path, append_system_prompt_path
    finally:
        for path in (system_prompt_path, append_system_prompt_path):
            if path and os.path.exists(path):
                with contextlib.suppress(OSError):
                    os.remove(path)


_READ_ONLY_ALLOWED_BASH_PATTERNS: tuple[str, ...] = (
    # Git read commands
    "Bash(git status)",
    "Bash(git status *)",
    "Bash(git log)",
    "Bash(git log *)",
    "Bash(git diff)",
    "Bash(git diff *)",
    "Bash(git show)",
    "Bash(git show *)",
    "Bash(git branch)",
    "Bash(git branch *)",
    "Bash(git tag)",
    "Bash(git tag *)",
    "Bash(git remote)",
    "Bash(git remote *)",
    "Bash(git rev-parse *)",
    "Bash(git describe)",
    "Bash(git describe *)",
    "Bash(git blame *)",
    "Bash(git shortlog)",
    "Bash(git shortlog *)",
    "Bash(git stash list)",
    "Bash(git stash list *)",
    "Bash(git ls-files)",
    "Bash(git ls-files *)",
    "Bash(git ls-tree *)",
    "Bash(git cat-file *)",
    # Filesystem read commands
    "Bash(ls)",
    "Bash(ls *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(find *)",
    "Bash(tree)",
    "Bash(tree *)",
    "Bash(file *)",
    "Bash(wc *)",
    "Bash(du)",
    "Bash(du *)",
    "Bash(stat *)",
    "Bash(realpath *)",
    "Bash(readlink *)",
    # Search commands
    "Bash(grep *)",
    "Bash(rg *)",
    "Bash(ag *)",
    "Bash(ack *)",
    # Info/version commands
    "Bash(which *)",
    "Bash(where *)",
    "Bash(type *)",
    "Bash(echo *)",
    "Bash(pwd)",
    "Bash(env)",
    "Bash(printenv)",
    "Bash(printenv *)",
    "Bash(uname)",
    "Bash(uname *)",
    "Bash(whoami)",
    "Bash(date)",
    "Bash(date *)",
    "Bash(node --version)",
    "Bash(npm --version)",
    "Bash(python --version)",
    "Bash(python3 --version)",
    "Bash(cargo --version)",
    "Bash(go version)",
    "Bash(rustc --version)",
    # Build/dependency inspection (read-only)
    "Bash(npm ls)",
    "Bash(npm ls *)",
    "Bash(npm list)",
    "Bash(npm list *)",
    "Bash(npm info *)",
    "Bash(npm view *)",
    "Bash(cargo tree)",
    "Bash(cargo tree *)",
    "Bash(pip list)",
    "Bash(pip list *)",
    "Bash(pip show *)",
    # Process/system info
    "Bash(ps)",
    "Bash(ps *)",
    "Bash(df)",
    "Bash(df *)",
    # gh read commands
    "Bash(gh pr view)",
    "Bash(gh pr view *)",
    "Bash(gh pr list)",
    "Bash(gh pr list *)",
    "Bash(gh pr diff)",
    "Bash(gh pr diff *)",
    "Bash(gh pr checks)",
    "Bash(gh pr checks *)",
    "Bash(gh pr status)",
    "Bash(gh pr status *)",
    "Bash(gh issue view)",
    "Bash(gh issue view *)",
    "Bash(gh issue list)",
    "Bash(gh issue list *)",
    "Bash(gh run list)",
    "Bash(gh run list *)",
    "Bash(gh run view)",
    "Bash(gh run view *)",
    "Bash(gh repo view)",
    "Bash(gh repo view *)",
    "Bash(gh release list)",
    "Bash(gh release list *)",
    "Bash(gh release view)",
    "Bash(gh release view *)",
    "Bash(gh api *)",
)


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

    def _build_argv(
        self,
        messages: list[Message],
        model: str,
        sys_path: str | None,
        app_path: str | None,
        kwargs: dict[str, Any],
    ) -> list[str]:
        user_parts: list[str] = []
        for m in messages:
            if m.role is not MessageRole.SYSTEM:
                user_parts.append(m.content)
        body = "\n\n".join(user_parts).strip()

        argv = [
            self._binary,
            "-p",
            body,
            "--model",
            model,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if sys_path:
            argv.extend(["--system-prompt-file", sys_path])
        if app_path:
            argv.extend(["--append-system-prompt-file", app_path])

        mcp_config_path = kwargs.get("mcp_config_path")
        if mcp_config_path:
            argv.extend(["--mcp-config", str(mcp_config_path), "--strict-mcp-config"])

        is_read_only = kwargs.get("mode") == "read-only" or kwargs.get("read_only") is True
        if is_read_only:
            argv.extend(["--permission-mode", "dontAsk"])
            argv.extend(["--disallowed-tools", "Edit,Write,NotebookEdit"])
            allowed_tools = [
                "Read",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
                *_READ_ONLY_ALLOWED_BASH_PATTERNS,
            ]
            argv.append("--allowedTools")
            argv.extend(allowed_tools)

        return argv

    async def _stream_mock_process(
        self,
        argv: list[str],
        status: dict[str, Any] | None,
    ) -> AsyncIterator[tuple[str, bytes]]:
        try:
            rc, stdout, stderr = await asyncio.wait_for(
                self._run(*argv, stdin=None), timeout=self._timeout
            )
        except (FileNotFoundError, asyncio.TimeoutError) as e:
            raise BackendUnavailableError(f"claude CLI unreachable: {e}") from e

        for line in stdout.splitlines(keepends=True):
            yield "stdout", line
        for line in stderr.splitlines(keepends=True):
            yield "stderr", line

        if rc != 0 and (status is None or not status.get("result_seen")):
            detail = stderr.decode(errors="replace").strip() or "claude failed"
            raise BackendUnavailableError(f"claude rc={rc}: {detail[:1024]}")

    def _check_return_code(
        self,
        rc: int | None,
        status: dict[str, Any] | None,
        stderr_lines: list[bytes],
    ) -> None:
        if rc != 0 and rc is not None and (status is None or not status.get("result_seen")):
            err_detail = b"".join(stderr_lines).decode(errors="replace").strip() or "claude failed"
            raise BackendUnavailableError(f"claude rc={rc}: {err_detail[:1024]}")

    async def _stream_process(
        self,
        argv: list[str],
        stdin: bytes | None = None,
        status: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[str, bytes]]:
        stderr_lines: list[bytes] = []

        if self._run is not _default_runner:
            async for name, line in self._stream_mock_process(argv, status):
                yield name, line
            return

        cmd_argv = (
            ["cmd", "/c", *argv]
            if os.name == "nt" and (not argv or argv[0] != "cmd")
            else list(argv)
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise BackendUnavailableError(f"claude CLI unreachable: {e}") from e

        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            with contextlib.suppress(ConnectionResetError):
                await proc.stdin.drain()
            proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()

        queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()

        async def read_stream(stream: asyncio.StreamReader, name: str) -> None:
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    await queue.put((name, line))
            except Exception as e:  # noqa: BLE001
                await queue.put(("error", str(e).encode()))
            finally:
                await queue.put((f"eof_{name}", b""))

        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_task = asyncio.create_task(read_stream(proc.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(proc.stderr, "stderr"))

        active_readers = 2
        try:
            while active_readers > 0:
                try:
                    name, line = await asyncio.wait_for(queue.get(), timeout=self._timeout)
                except asyncio.TimeoutError as e:
                    stdout_task.cancel()
                    stderr_task.cancel()
                    with contextlib.suppress(Exception):
                        proc.kill()
                    raise BackendUnavailableError(f"claude CLI timeout: {e}") from e

                if name == "error":
                    raise BackendUnavailableError(
                        f"claude CLI read error: {line.decode(errors='replace')}"
                    )
                if name.startswith("eof_"):
                    active_readers -= 1
                elif name == "stderr":
                    stderr_lines.append(line)
                    yield name, line
                else:
                    yield name, line
        finally:
            stdout_task.cancel()
            stderr_task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

            rc = proc.returncode
            if rc is None:
                with contextlib.suppress(Exception):
                    rc = await proc.wait()

            self._check_return_code(rc, status, stderr_lines)

    async def _parse_events(
        self,
        argv: list[str],
        status: dict[str, Any],
    ) -> AsyncIterator[dict[str, Any]]:
        async for stream_type, line_bytes in self._stream_process(argv, status=status):
            if stream_type == "stderr":
                continue

            line = line_bytes.decode(errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(event, dict):
                continue

            ev_type = event.get("type")
            if ev_type is None and ("result" in event or "usage" in event or "output" in event):
                event = dict(event, type="result")
                ev_type = "result"

            if ev_type == "result" and event.get("is_error"):
                errors = event.get("errors", [])
                err_msg = (
                    "; ".join(errors) if errors else event.get("result", "Claude result error")
                )
                raise BackendUnavailableError(err_msg)

            if ev_type == "error":
                err_msg = event.get("error") or event.get("message") or "Claude stream error"
                raise BackendUnavailableError(err_msg)

            yield event

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
        _ignore_unused(temperature, max_tokens, tools)
        system_prompts = [m.content for m in messages if m.role is MessageRole.SYSTEM]

        from pathlib import Path

        from maxwell_daemon.mcp.server import start_mcp_http_server

        config_path_raw = kwargs.get("config_path")
        config_path = Path(config_path_raw) if config_path_raw is not None else None

        async with start_mcp_http_server(config_path) as (temp_path, _):
            build_kwargs = dict(kwargs, mcp_config_path=temp_path)
            async with TemporaryPromptFiles(system_prompts) as (sys_path, app_path):
                argv = self._build_argv(messages, model, sys_path, app_path, build_kwargs)

            final_text = ""
            status = {"result_seen": False}
            duration_ms = 0
            cost_usd = 0.0
            input_tokens = 0
            output_tokens = 0
            stop_reason = "stop"
            raw_result: dict[str, Any] = {}

            try:
                async for event in self._parse_events(argv, status=status):
                    ev_type = event.get("type")
                    if ev_type == "assistant":
                        msg_data = event.get("message", {})
                        content = msg_data.get("content", [])
                        if isinstance(content, list):
                            text_blocks = [
                                block.get("text", "")
                                for block in content
                                if block.get("type") == "text"
                            ]
                            full_text = "".join(text_blocks)
                            if full_text:
                                final_text = full_text
                    elif ev_type == "result":
                        status["result_seen"] = True
                        raw_result = event
                        duration_ms = event.get("duration_ms", 0)
                        cost_usd = event.get("total_cost_usd", 0.0)

                        usage_dict = event.get("usage", {})
                        input_tokens = usage_dict.get("input_tokens", 0)
                        output_tokens = usage_dict.get("output_tokens", 0)
                        stop_reason = event.get("stop_reason") or event.get("subtype") or "stop"

                        if "result" in event or "output" in event:
                            res_val = (
                                event.get("result")
                                or event.get("output")
                                or event.get("text")
                                or ""
                            )
                            final_text = str(res_val)
            except Exception as e:
                if not isinstance(e, BackendUnavailableError):
                    raise BackendUnavailableError(f"claude -p failed: {e}") from e
                raise

            if not status.get("result_seen"):
                raise BackendUnavailableError("No structured result received from Claude CLI")

            return BackendResponse(
                content=final_text,
                finish_reason=stop_reason,
                usage=TokenUsage(
                    prompt_tokens=input_tokens,
                    completion_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                ),
                model=model,
                backend=self.name,
                raw={
                    "duration_ms": duration_ms,
                    "total_cost_usd": cost_usd,
                    "result_event": raw_result,
                },
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
        _ignore_unused(temperature, max_tokens, tools)
        system_prompts = [m.content for m in messages if m.role is MessageRole.SYSTEM]

        from pathlib import Path

        from maxwell_daemon.mcp.server import start_mcp_http_server

        config_path_raw = kwargs.get("config_path")
        config_path = Path(config_path_raw) if config_path_raw is not None else None

        async with start_mcp_http_server(config_path) as (temp_path, _):
            build_kwargs = dict(kwargs, mcp_config_path=temp_path)
            async with TemporaryPromptFiles(system_prompts) as (sys_path, app_path):
                argv = self._build_argv(messages, model, sys_path, app_path, build_kwargs)

            status = {"result_seen": False}
            last_message_id = None
            yielded_text = ""

            try:
                async for event in self._parse_events(argv, status=status):
                    ev_type = event.get("type")
                    if ev_type == "assistant":
                        msg_data = event.get("message", {})
                        current_message_id = msg_data.get("id")
                        if current_message_id != last_message_id:
                            last_message_id = current_message_id
                            yielded_text = ""

                        content = msg_data.get("content", [])
                        if isinstance(content, list):
                            text_blocks = [
                                block.get("text", "")
                                for block in content
                                if block.get("type") == "text"
                            ]
                            full_text = "".join(text_blocks)
                            if len(full_text) > len(yielded_text):
                                delta = full_text[len(yielded_text) :]
                                yielded_text = full_text
                                yield delta
                    elif ev_type == "result":
                        status["result_seen"] = True
                        if "result" in event or "output" in event:
                            res_val = (
                                event.get("result")
                                or event.get("output")
                                or event.get("text")
                                or ""
                            )
                            res_str = str(res_val)
                            if len(res_str) > len(yielded_text):
                                delta = res_str[len(yielded_text) :]
                                yielded_text = res_str
                                yield delta
            except Exception as e:
                if not isinstance(e, BackendUnavailableError):
                    raise BackendUnavailableError(f"claude -p failed: {e}") from e
                raise

            if not status.get("result_seen"):
                raise BackendUnavailableError("No structured result received from Claude CLI")

    async def health_check(self) -> bool:
        try:
            rc, _, _ = await self._run(self._binary, "--version")
        except (FileNotFoundError, OSError):
            return False
        return rc == 0

    def capabilities(self, model: str) -> BackendCapabilities:
        _ignore_unused(model)
        return BackendCapabilities(
            supports_streaming=True,
            supports_tool_use=True,
            supports_vision=True,
            supports_system_prompt=True,
            max_context_tokens=200_000,
            is_local=False,
            cost_per_1k_input_tokens=None,
            cost_per_1k_output_tokens=None,
        )


registry.register("claude-code-cli", ClaudeCodeCLIBackend)
