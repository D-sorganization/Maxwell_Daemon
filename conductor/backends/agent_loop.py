"""Multi-turn Anthropic SDK agent loop backend.

Unlike the one-shot ``claude -p`` approach in :mod:`conductor.backends.claude_code`,
this backend drives a full tool-use loop using the Anthropic Python SDK directly.
The agent can read/write/edit files, run bash commands, and search the workspace
across up to ``max_turns`` turns before returning the final text response.

All file operations are confined to ``workspace_dir`` — any attempt to access a
path outside that directory (via ``..`` traversal or absolute path escape) is
rejected and the tool call returns an error string instead of raising.
"""

from __future__ import annotations

import glob as _glob
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from conductor.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from conductor.backends.registry import registry

__all__ = ["AgentLoopBackend"]

# ---------------------------------------------------------------------------
# Tool schemas (Anthropic tool-use format)
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file inside the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the workspace.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (create or overwrite) a file inside the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Full content to write to the file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact substring in a file (str_replace style). "
            "Fails if ``old_str`` is not found or appears more than once."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path to the file within the workspace.",
                },
                "old_str": {
                    "type": "string",
                    "description": "The exact string to replace (must appear exactly once).",
                },
                "new_str": {
                    "type": "string",
                    "description": "The replacement string.",
                },
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "run_bash",
        "description": "Run a shell command inside the workspace directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30).",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "glob_files",
        "description": "Return a list of file paths matching a glob pattern in the workspace.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to workspace (e.g. '**/*.py').",
                }
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_files",
        "description": "Recursively search files for a pattern using grep.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Sub-path within workspace to search (default '.').",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
    },
]

# Anthropic public pricing (USD per 1M tokens) as of 2026-04.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (0.80, 4.0),
    "claude-3-5-sonnet-latest": (3.0, 15.0),
    "claude-3-5-haiku-latest": (0.80, 4.0),
}

_MODEL_CONTEXT: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-haiku-latest": 200_000,
}


class AgentLoopBackend(ILLMBackend):
    """Multi-turn Anthropic SDK backend with a built-in tool-execution loop.

    Parameters
    ----------
    api_key:
        Anthropic API key. Falls back to ``ANTHROPIC_API_KEY`` env var.
    model:
        Default model to use when none is supplied to ``complete()``.
    max_turns:
        Maximum number of agentic turns before raising ``RuntimeError``.
    timeout:
        HTTP timeout for each SDK call (seconds).
    ledger:
        Optional :class:`~conductor.core.ledger.CostLedger` for cost tracking.
    workspace_dir:
        Default workspace directory for tool execution. Can be overridden per
        call via ``kwargs["workspace_dir"]``.
    """

    name = "agent-loop"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = "claude-sonnet-4-6",
        max_turns: int = 150,
        timeout: float = 120.0,
        ledger: Any | None = None,
        workspace_dir: str | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY not set and no api_key passed")
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout)
        self._default_model = model
        self._max_turns = max_turns
        self._ledger = ledger
        self._default_workspace = workspace_dir

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------

    def _safe_path(self, workspace_dir: str, relative_path: str) -> Path:
        """Resolve ``relative_path`` inside ``workspace_dir``.

        Raises ``ValueError`` if the resolved path escapes the workspace.
        """
        workspace = Path(workspace_dir).resolve()
        candidate = (workspace / relative_path).resolve()
        if not str(candidate).startswith(str(workspace)):
            msg = f"Path traversal rejected: {relative_path!r} escapes workspace"
            raise ValueError(msg)
        return candidate

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    def _execute_tool(
        self, name: str, inputs: dict[str, Any], workspace_dir: str
    ) -> str:
        """Dispatch a single tool call and return the string result.

        All exceptions are caught and returned as error strings so the loop
        can continue (the model will see the error and adapt).
        """
        try:
            return self._dispatch_tool(name, inputs, workspace_dir)
        except Exception as exc:
            return f"ERROR: {exc}"

    def _dispatch_tool(
        self, name: str, inputs: dict[str, Any], workspace_dir: str
    ) -> str:
        if name == "read_file":
            path = self._safe_path(workspace_dir, inputs["path"])
            return path.read_text(errors="replace")

        if name == "write_file":
            path = self._safe_path(workspace_dir, inputs["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inputs["content"])
            return f"Written {len(inputs['content'])} chars to {inputs['path']}"

        if name == "edit_file":
            path = self._safe_path(workspace_dir, inputs["path"])
            text = path.read_text(errors="replace")
            old_str: str = inputs["old_str"]
            new_str: str = inputs["new_str"]
            count = text.count(old_str)
            if count == 0:
                raise ValueError(f"old_str not found in {inputs['path']!r}")
            if count > 1:
                raise ValueError(
                    f"old_str appears {count} times in {inputs['path']!r}; must be unique"
                )
            path.write_text(text.replace(old_str, new_str, 1))
            return f"Replaced 1 occurrence in {inputs['path']}"

        if name == "run_bash":
            command: str = inputs["command"]
            timeout: int = int(inputs.get("timeout") or 30)
            result = subprocess.run(
                command,
                shell=True,
                cwd=workspace_dir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            if result.returncode != 0:
                return f"Exit {result.returncode}:\n{output}"
            return output or "(no output)"

        if name == "glob_files":
            pattern: str = inputs["pattern"]
            workspace = Path(workspace_dir).resolve()
            matches = _glob.glob(pattern, root_dir=str(workspace), recursive=True)
            return "\n".join(sorted(matches)) if matches else "(no matches)"

        if name == "grep_files":
            pattern = inputs["pattern"]
            search_path = inputs.get("path") or "."
            safe_search = self._safe_path(workspace_dir, search_path)
            workspace = Path(workspace_dir).resolve()
            grep_matches: list[str] = []
            for file_path in sorted(p for p in safe_search.rglob("*") if p.is_file()):
                try:
                    text = file_path.read_text(errors="replace")
                except OSError:
                    continue
                try:
                    rel_path = file_path.relative_to(workspace)
                except ValueError:
                    rel_path = file_path.relative_to(safe_search)
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if pattern in line:
                        grep_matches.append(f"{rel_path}:{line_no}:{line}")
            return "\n".join(grep_matches) if grep_matches else "(no matches)"

        raise ValueError(f"Unknown tool: {name!r}")

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self, system_messages: list[str], workspace_dir: str) -> str:
        parts = list(system_messages)
        parts.append(
            f"You have access to tools to read/write/edit files and run bash commands.\n"
            f"Your working workspace is: {workspace_dir}\n"
            f"All file paths must be relative to the workspace root."
        )
        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # Cost recording
    # ------------------------------------------------------------------

    def _record_cost(
        self,
        response: anthropic.types.Message,
        model: str,
        repo: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        if self._ledger is None:
            return
        try:
            from conductor.core.ledger import CostRecord

            price_in, price_out = _MODEL_PRICING.get(model, (3.0, 15.0))
            usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            )
            cost_usd = (
                usage.prompt_tokens * price_in / 1_000_000
                + usage.completion_tokens * price_out / 1_000_000
            )
            rec = CostRecord(
                ts=datetime.now(timezone.utc),
                backend=self.name,
                model=model,
                usage=usage,
                cost_usd=cost_usd,
                repo=repo,
                agent_id=agent_id,
            )
            self._ledger.record(rec)
        except Exception:
            pass  # Cost recording must never crash the agent loop

    # ------------------------------------------------------------------
    # ILLMBackend interface
    # ------------------------------------------------------------------

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
        repo: str | None = None,
        agent_id: str | None = None,
        **kwargs: Any,
    ) -> BackendResponse:
        """Run the multi-turn agent loop and return the final response.

        Parameters
        ----------
        workspace_dir:
            Directory in which file/bash tools operate. Defaults to
            ``self._default_workspace`` or the process cwd.
        max_turns:
            Override the instance-level ``max_turns`` for this call.
        repo, agent_id:
            Forwarded to the cost ledger for attribution.
        """
        effective_model = model or self._default_model
        effective_workspace = workspace_dir or self._default_workspace or os.getcwd()
        effective_max_turns = max_turns if max_turns is not None else self._max_turns

        # Split system messages from conversation messages.
        system_parts: list[str] = []
        sdk_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role is MessageRole.SYSTEM:
                system_parts.append(m.content)
            else:
                sdk_messages.append({"role": m.role.value, "content": m.content})

        system_prompt = self._build_system_prompt(system_parts, effective_workspace)

        total_usage = TokenUsage()
        last_text = ""
        final_model = effective_model

        for turn in range(effective_max_turns):
            tool_params: Any = TOOL_SCHEMAS
            message_params: Any = sdk_messages
            response = self._client.messages.create(
                model=effective_model,
                max_tokens=max_tokens or 8096,
                system=system_prompt,
                tools=tool_params,
                messages=message_params,
            )

            # Accumulate usage.
            turn_usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            )
            total_usage = total_usage + turn_usage
            final_model = response.model

            # Record cost per turn.
            self._record_cost(response, effective_model, repo=repo, agent_id=agent_id)

            # Extract text from this turn.
            text_parts = [
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            ]
            if text_parts:
                last_text = "".join(text_parts)

            # If end_turn, we're done.
            if response.stop_reason == "end_turn":
                return BackendResponse(
                    content=last_text,
                    finish_reason="end_turn",
                    usage=total_usage,
                    model=final_model,
                    backend=self.name,
                    raw={"turns": turn + 1},
                )

            # If tool_use, execute tools and continue loop.
            if response.stop_reason == "tool_use":
                # Append the assistant turn with all content blocks.
                sdk_messages.append(
                    {"role": "assistant", "content": response.content}
                )

                # Build tool results.
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_use: Any = block
                        result = self._execute_tool(
                            tool_use.name, tool_use.input, effective_workspace
                        )
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use.id,
                                "content": result,
                            }
                        )

                sdk_messages.append({"role": "user", "content": tool_results})
                continue

            # Unexpected stop reason — treat as end_turn.
            return BackendResponse(
                content=last_text,
                finish_reason=response.stop_reason or "stop",
                usage=total_usage,
                model=final_model,
                backend=self.name,
                raw={"turns": turn + 1},
            )

        raise RuntimeError(
            f"Agent loop exceeded max_turns={effective_max_turns} without end_turn"
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
        """Run the agent loop and yield the final text as a single chunk."""
        resp = await self.complete(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        yield resp.content

    async def health_check(self) -> bool:
        try:
            self._client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            return True
        except Exception:
            return False

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = _MODEL_PRICING.get(model, (3.0, 15.0))
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=True,
            supports_system_prompt=True,
            max_context_tokens=_MODEL_CONTEXT.get(model, 200_000),
            is_local=False,
            cost_per_1k_input_tokens=price_in / 1000,
            cost_per_1k_output_tokens=price_out / 1000,
        )


registry.register("agent-loop", AgentLoopBackend)
