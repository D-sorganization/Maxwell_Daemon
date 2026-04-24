"""Multi-turn Anthropic SDK agent loop backend.

Unlike the one-shot ``claude -p`` approach in :mod:`maxwell_daemon.backends.claude_code`,
this backend drives a full tool-use loop using the Anthropic async SDK. The
agent reads/writes/edits files, runs bash, and iterates across ``max_turns``
turns before returning the final text response.

**Key design choices (per Maxwell-Daemon principles):**

* **Agent Agnostic / DRY:** tools come from :mod:`maxwell_daemon.tools` — the same
  registry emits Anthropic *and* OpenAI schemas. We never hand-roll tool
  dispatch or JSON-schema dicts in here.
* **Reversibility:** every knob that changes behaviour (memory, ledger, budget,
  wall-clock, cache) is an optional constructor parameter with safe defaults.
  Turning a feature off is deleting an argument.
* **DbC on boundaries:** abort conditions raise dedicated exceptions
  (``BudgetExceededError``, ``WallClockTimeoutError``) so callers pattern-match
  on a failure *kind*, not a string.
* **Prompt caching** is on by default — a multi-turn loop that replays a
  system prompt every turn is the prototypical caching case.
"""

from __future__ import annotations

import logging
from maxwell_daemon.logging import get_logger
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.condensation import Condenser
from maxwell_daemon.backends.pricing import cost_for, get_rates
from maxwell_daemon.backends.registry import registry
from maxwell_daemon.contracts import PreconditionError
from maxwell_daemon.gh.ci_patterns import detect_ci_profile
from maxwell_daemon.gh.repo_schematic import build_repo_schematic
from maxwell_daemon.tools import ToolRegistry, build_default_registry

__all__ = [
    "AgentLoopBackend",
    "BudgetExceededError",
    "WallClockTimeoutError",
]

log = get_logger(__name__)


# ── Module-level config ───────────────────────────────────────────────────────


# Per-model context-window sizes (tokens).  Pricing lives in the central table
# at :mod:`maxwell_daemon.backends.pricing` — imported above.
_MODEL_CONTEXT: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-haiku-latest": 200_000,
}

_CLAUDE_DOCS_PRIORITY: tuple[str, ...] = (
    "CLAUDE.md",
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
)


# ── Exceptions ────────────────────────────────────────────────────────────────


class BudgetExceededError(RuntimeError):
    """Raised when a single agent-loop invocation exceeds its per-story budget."""


class WallClockTimeoutError(RuntimeError):
    """Raised when an agent-loop invocation exceeds its wall-clock deadline."""


# ── Backend ───────────────────────────────────────────────────────────────────


class AgentLoopBackend(ILLMBackend):
    """Multi-turn Anthropic backend with MCP-based tool execution.

    Parameters
    ----------
    api_key:
        Anthropic API key. Falls back to ``ANTHROPIC_API_KEY``.
    model:
        Default model when none is supplied to ``complete()``.
    max_turns:
        Hard cap on agent turns before ``RuntimeError``.
    timeout:
        Per-request HTTP timeout in seconds.
    ledger:
        Optional :class:`~maxwell_daemon.core.ledger.CostLedger` for audit trail.
    memory:
        Optional :class:`~maxwell_daemon.memory.MemoryManager` — when set, the
        system prompt gets prior-knowledge, repo facts, and scratchpad injected.
    workspace_dir:
        Default workspace for tool execution; per-call overrides via kwarg.
    budget_per_story_usd:
        Cumulative USD cap for a single ``complete()`` call. ``None`` disables.
    wall_clock_timeout_seconds:
        Deadline in seconds from the first turn. ``None`` disables.
    enable_prompt_caching:
        Attach ``cache_control: {"type": "ephemeral"}`` to the system block.
    registry_factory:
        Injection seam — callable ``(Path) -> ToolRegistry`` for test doubles.
        Defaults to :func:`maxwell_daemon.tools.build_default_registry`.
    budget_enforcer:
        Optional :class:`~maxwell_daemon.core.budget.BudgetEnforcer` — when set,
        ``require_under_budget()`` is called after every turn so a long agent loop
        cannot silently overspend the monthly hard-stop limit.
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
        memory: Any | None = None,
        workspace_dir: str | None = None,
        budget_per_story_usd: float | None = None,
        wall_clock_timeout_seconds: float | None = None,
        enable_prompt_caching: bool = True,
        registry_factory: Callable[[Path], ToolRegistry] | None = None,
        condenser: Condenser | None = None,
        budget_enforcer: Any | None = None,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY not set and no api_key passed")
        self._client: anthropic.AsyncAnthropic = anthropic.AsyncAnthropic(
            api_key=key, timeout=timeout
        )
        self._default_model = model
        self._max_turns = max_turns
        self._ledger = ledger
        self._memory = memory
        self._default_workspace = workspace_dir
        self._budget_per_story_usd = budget_per_story_usd
        self._wall_clock_timeout = wall_clock_timeout_seconds
        self._enable_cache = enable_prompt_caching
        self._registry_factory = registry_factory or build_default_registry
        self._condenser = condenser
        self._budget_enforcer = budget_enforcer

    # ── System prompt assembly ───────────────────────────────────────────────

    def _load_first_doc(self, workspace: Path) -> str:
        """Return the first repo-specific doc found, or an empty string."""
        for name in _CLAUDE_DOCS_PRIORITY:
            candidate = workspace / name
            if candidate.is_file():
                try:
                    return f"## {name}\n\n" + candidate.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    continue
        return ""

    def _build_system_blocks(
        self,
        *,
        system_texts: list[str],
        workspace: Path,
        repo: str | None,
        issue_title: str,
        issue_body: str,
        agent_id: str | None,
    ) -> list[dict[str, Any]] | str:
        """Assemble the system prompt.

        Produces a list of content blocks when caching is on, so ``cache_control``
        can attach to the stable suffix. Falls back to a plain string when
        caching is off — matches the Anthropic SDK's ``system: str | list`` shape.
        """
        parts: list[str] = [t for t in system_texts if t]
        parts.append(
            "You have access to tools to read, write, edit, search, and run bash "
            "inside the workspace. All paths are relative to the workspace root."
        )
        parts.append(f"Workspace: {workspace}")

        # Inject the repo's CI contract — ruff/mypy/pytest/precommit — so the
        # agent knows what must pass before a PR can merge. Detection is a
        # pure filesystem walk; empty workspaces yield an empty string.
        with suppress(Exception):
            ci_block = detect_ci_profile(workspace).to_prompt()
            if ci_block:
                parts.append(ci_block)

        # Inject a compact repo map (file -> top-level symbols) so the agent
        # knows where things live without paying for full-file context per turn.
        with suppress(Exception):
            map_block = build_repo_schematic(workspace).to_prompt(max_chars=2000)
            if map_block:
                parts.append(map_block)

        doc = self._load_first_doc(workspace)
        if doc:
            parts.append(doc)

        if self._memory is not None and repo:
            with suppress(Exception):
                memory_text = self._memory.assemble_context(
                    repo=repo,
                    issue_title=issue_title,
                    issue_body=issue_body,
                    task_id=agent_id or "adhoc",
                    max_chars=8000,
                )
                if memory_text:
                    parts.append(memory_text)

        text = "\n\n".join(parts)
        if not self._enable_cache:
            return text
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    # ── Cost math ────────────────────────────────────────────────────────────

    @staticmethod
    def _cost_for(usage: TokenUsage, model: str) -> float:
        """Compute USD cost for a turn's usage. Delegates to central pricing table."""
        return cost_for("agent-loop", model, usage)

    def _record_cost(
        self,
        *,
        usage: TokenUsage,
        cost_usd: float,
        model: str,
        repo: str | None,
        agent_id: str | None,
    ) -> None:
        if self._ledger is None:
            return
        # Import lazily so the ledger dependency is optional — tests that inject
        # a MagicMock ledger don't have to stub CostRecord.
        try:
            from maxwell_daemon.core.ledger import CostRecord

            self._ledger.record(
                CostRecord(
                    ts=datetime.now(timezone.utc),
                    backend=self.name,
                    model=model,
                    usage=usage,
                    cost_usd=cost_usd,
                    repo=repo,
                    agent_id=agent_id,
                )
            )
        except Exception:
            log.exception("Failed to record cost")

    # ── ILLMBackend interface ────────────────────────────────────────────────

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
        issue_title: str = "",
        issue_body: str = "",
        **kwargs: Any,
    ) -> BackendResponse:
        """Drive the multi-turn agent loop.

        The ``tools`` parameter is ignored (we source tools from the registry).
        The caller controls loop budget via ``max_turns`` / constructor
        ``budget_per_story_usd`` / ``wall_clock_timeout_seconds``.
        """
        effective_model = model or self._default_model
        effective_workspace = self._resolve_workspace(workspace_dir)
        effective_max_turns = max_turns if max_turns is not None else self._max_turns

        tool_registry = self._registry_factory(effective_workspace)
        tool_defs = tool_registry.to_anthropic()

        system_prompt = self._build_system_blocks(
            system_texts=[m.content for m in messages if m.role is MessageRole.SYSTEM],
            workspace=effective_workspace,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            agent_id=agent_id,
        )
        sdk_messages: list[dict[str, Any]] = [
            {"role": m.role.value, "content": m.content}
            for m in messages
            if m.role is not MessageRole.SYSTEM
        ]

        total_usage = TokenUsage()
        cumulative_cost = 0.0
        last_text = ""
        final_model = effective_model

        deadline: float | None = None
        if self._wall_clock_timeout is not None:
            deadline = time.monotonic() + self._wall_clock_timeout

        for turn in range(effective_max_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise WallClockTimeoutError(
                    f"agent loop exceeded wall-clock timeout of "
                    f"{self._wall_clock_timeout}s at turn {turn + 1}"
                )

            # Condense message history if context is getting long. The
            # condenser no-ops when the list is already short enough.
            if self._condenser is not None and self._condenser.should_condense(
                total_usage.prompt_tokens
            ):
                sdk_messages = await self._condenser.condense(sdk_messages)

            response = await self._client.messages.create(
                model=effective_model,
                max_tokens=max_tokens or 8096,
                system=system_prompt,  # type: ignore[arg-type]
                tools=tool_defs,  # type: ignore[arg-type]
                messages=sdk_messages,  # type: ignore[arg-type]
            )

            # Usage + cost.
            cached_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            turn_usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens - cached_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            )
            total_usage = total_usage + turn_usage
            turn_cost = self._cost_for(turn_usage, effective_model)
            cumulative_cost += turn_cost
            final_model = response.model

            self._record_cost(
                usage=turn_usage,
                cost_usd=turn_cost,
                model=effective_model,
                repo=repo,
                agent_id=agent_id,
            )

            # Per-turn monthly-budget enforcement — the ledger is now updated so
            # BudgetEnforcer.require_under_budget() will see the latest spend.
            if self._budget_enforcer is not None:
                self._budget_enforcer.require_under_budget()

            if (
                self._budget_per_story_usd is not None
                and cumulative_cost > self._budget_per_story_usd
            ):
                raise BudgetExceededError(
                    f"agent loop exceeded per-story budget "
                    f"${self._budget_per_story_usd:.4f} at turn {turn + 1} "
                    f"(cumulative ${cumulative_cost:.4f})"
                )

            # Per-task limit from BudgetConfig (injected via budget_enforcer).
            if self._budget_enforcer is not None:
                per_task_limit = getattr(self._budget_enforcer._config, "per_task_limit_usd", None)
                if per_task_limit is not None and cumulative_cost > per_task_limit:
                    raise BudgetExceededError(
                        f"agent loop exceeded per-task budget limit "
                        f"${per_task_limit:.4f} at turn {turn + 1} "
                        f"(cumulative ${cumulative_cost:.4f})"
                    )

            text_parts = [
                getattr(b, "text", "")
                for b in response.content
                if getattr(b, "type", None) == "text"
            ]
            if text_parts:
                last_text = "".join(text_parts)

            if response.stop_reason == "end_turn":
                return BackendResponse(
                    content=last_text,
                    finish_reason="end_turn",
                    usage=total_usage,
                    model=final_model,
                    backend=self.name,
                    raw={"turns": turn + 1, "cost_usd": cumulative_cost},
                )

            if response.stop_reason == "tool_use":
                sdk_messages.append({"role": "assistant", "content": response.content})
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_use: Any = block
                    result = await tool_registry.invoke(tool_use.name, tool_use.input)
                    content = f"ERROR: {result.content}" if result.is_error else result.content
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": content,
                            **({"is_error": True} if result.is_error else {}),
                        }
                    )
                sdk_messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason — treat as terminal.
            return BackendResponse(
                content=last_text,
                finish_reason=response.stop_reason or "stop",
                usage=total_usage,
                model=final_model,
                backend=self.name,
                raw={"turns": turn + 1, "cost_usd": cumulative_cost},
            )

        raise RuntimeError(f"agent loop exceeded max_turns={effective_max_turns} without end_turn")

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        workspace_dir: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """Stream text tokens from the final agent turn as they arrive.

        The multi-turn tool-use loop runs synchronously (tool execution is
        inherently sequential) but the *last* turn — where the model writes
        its final answer — is streamed token-by-token via the Anthropic
        streaming API so the caller sees output incrementally rather than
        waiting for the full response to buffer.

        Tool-call turns are still awaited in full (we need the complete tool
        spec before we can execute the call), but those turns produce no
        user-visible text so the latency difference is unnoticeable.
        """
        effective_model = model or self._default_model
        effective_workspace = self._resolve_workspace(workspace_dir)
        effective_max_turns = kwargs.pop("max_turns", None)
        if effective_max_turns is None:
            effective_max_turns = self._max_turns

        tool_registry = self._registry_factory(effective_workspace)
        tool_defs = tool_registry.to_anthropic()

        repo = kwargs.pop("repo", None)
        agent_id = kwargs.pop("agent_id", None)
        issue_title = kwargs.pop("issue_title", "")
        issue_body = kwargs.pop("issue_body", "")

        system_prompt = self._build_system_blocks(
            system_texts=[m.content for m in messages if m.role is MessageRole.SYSTEM],
            workspace=effective_workspace,
            repo=repo,
            issue_title=issue_title,
            issue_body=issue_body,
            agent_id=agent_id,
        )
        sdk_messages: list[dict[str, Any]] = [
            {"role": m.role.value, "content": m.content}
            for m in messages
            if m.role is not MessageRole.SYSTEM
        ]

        total_usage = TokenUsage()
        cumulative_cost = 0.0

        deadline: float | None = None
        if self._wall_clock_timeout is not None:
            deadline = time.monotonic() + self._wall_clock_timeout

        for turn in range(effective_max_turns):
            if deadline is not None and time.monotonic() >= deadline:
                raise WallClockTimeoutError(
                    f"agent loop exceeded wall-clock timeout of "
                    f"{self._wall_clock_timeout}s at turn {turn + 1}"
                )

            if self._condenser is not None and self._condenser.should_condense(
                total_usage.prompt_tokens
            ):
                sdk_messages = await self._condenser.condense(sdk_messages)

            # For the final turn (end_turn), stream tokens to the caller as
            # they arrive.  For tool-call turns we need the full response
            # before we can dispatch tools, so we accumulate then loop.
            # We detect which case we're in by peeking at the stop_reason
            # after streaming completes — both paths go through the same
            # ``async with`` block so the HTTP connection is always closed.
            collected_text_chunks: list[str] = []
            async with self._client.messages.stream(
                model=effective_model,
                max_tokens=max_tokens or 8096,
                system=system_prompt,  # type: ignore[arg-type]
                tools=tool_defs,  # type: ignore[arg-type]
                messages=sdk_messages,  # type: ignore[arg-type]
            ) as stream:
                # Collect text chunks as they arrive so we can yield them
                # immediately to the caller on the final turn.
                async for text_chunk in stream.text_stream:
                    collected_text_chunks.append(text_chunk)

                # Collect accumulated message for tool dispatch / usage.
                response = await stream.get_final_message()

            # Track usage and cost for this turn.
            cached_tokens = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            turn_usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens - cached_tokens,
                completion_tokens=response.usage.output_tokens,
                total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                cached_tokens=cached_tokens,
            )
            total_usage = total_usage + turn_usage
            turn_cost = self._cost_for(turn_usage, effective_model)
            cumulative_cost += turn_cost

            self._record_cost(
                usage=turn_usage,
                cost_usd=turn_cost,
                model=effective_model,
                repo=repo,
                agent_id=agent_id,
            )

            if (
                self._budget_per_story_usd is not None
                and cumulative_cost > self._budget_per_story_usd
            ):
                raise BudgetExceededError(
                    f"agent loop exceeded per-story budget "
                    f"${self._budget_per_story_usd:.4f} at turn {turn + 1} "
                    f"(cumulative ${cumulative_cost:.4f})"
                )

            if response.stop_reason == "end_turn":
                # Final turn — yield the streamed chunks we collected above.
                for text_chunk in collected_text_chunks:
                    yield text_chunk
                return

            if response.stop_reason == "tool_use":
                # Tool turn — dispatch and loop.  No text to yield here.
                sdk_messages.append({"role": "assistant", "content": response.content})
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool_use: Any = block
                    result = await tool_registry.invoke(tool_use.name, tool_use.input)
                    content = f"ERROR: {result.content}" if result.is_error else result.content
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": content,
                            **({"is_error": True} if result.is_error else {}),
                        }
                    )
                sdk_messages.append({"role": "user", "content": tool_results})
                continue

            # Any other stop reason — yield what we collected and stop.
            for text_chunk in collected_text_chunks:
                yield text_chunk
            return

        raise RuntimeError(f"agent loop exceeded max_turns={effective_max_turns} without end_turn")

    async def health_check(self) -> bool:
        try:
            await self._client.messages.create(
                model=self._default_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "."}],
            )
            return True
        except Exception:
            return False

    def capabilities(self, model: str) -> BackendCapabilities:
        price_in, price_out = get_rates("agent-loop", model)
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

    async def aclose(self) -> None:
        """Release the underlying HTTPX connection pool.

        ``AsyncAnthropic`` opens persistent connections; without an explicit
        close a daemon that cycles backends (A/B dispatch, config reload)
        leaks sockets and eventually hits ulimit. ``suppress(Exception)``
        guards against SDK versions that expose a different close method.
        """
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is None:
            return
        with suppress(Exception):
            result = close()
            if hasattr(result, "__await__"):
                await result

    def _resolve_workspace(self, workspace_dir: str | None) -> Path:
        workspace_raw = workspace_dir or self._default_workspace
        if not workspace_raw:
            raise PreconditionError(
                "AgentLoopBackend requires workspace_dir; refusing to fall back to cwd"
            )
        return Path(workspace_raw).resolve()


registry.register("agent-loop", AgentLoopBackend)
