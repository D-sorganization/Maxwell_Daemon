"""Unit tests for AgentLoopBackend — the multi-turn Anthropic agent loop.

Scope: loop mechanics (turn limit, stop-reason handling, system prompt
assembly, prompt caching, budget enforcement, wall-clock timeout, memory
injection, cost recording). Tool behavior itself is covered in
``tests/unit/test_tools_builtins.py`` — we don't re-test it here.

All Anthropic calls are mocked — no real API traffic.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from conductor.backends.agent_loop import (
    AgentLoopBackend,
    BudgetExceededError,
    WallClockTimeoutError,
)
from conductor.backends.base import Message, MessageRole

# ── Helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _block(kind: str, **fields: Any) -> MagicMock:
    b = MagicMock()
    b.type = kind
    for k, v in fields.items():
        setattr(b, k, v)
    return b


def _response(
    *,
    stop_reason: str = "end_turn",
    text: str | None = "done",
    tool_calls: Iterable[dict[str, Any]] = (),
    input_tokens: int = 10,
    output_tokens: int = 5,
    model: str = "claude-sonnet-4-6",
) -> MagicMock:
    """Build a fake ``anthropic.types.Message`` for the mock async client."""
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.model = model
    resp.usage = MagicMock()
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.usage.cache_read_input_tokens = 0
    content: list[MagicMock] = []
    if text is not None:
        content.append(_block("text", text=text))
    for tc in tool_calls:
        content.append(_block("tool_use", id=tc["id"], name=tc["name"], input=tc["input"]))
    resp.content = content
    return resp


def _install_mock_client(backend: AgentLoopBackend, responses: list[MagicMock]) -> AsyncMock:
    """Replace the backend's Anthropic client with an AsyncMock cycling ``responses``."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    backend._client = client  # type: ignore[assignment]
    return client.messages.create


def _user(text: str) -> list[Message]:
    return [Message(role=MessageRole.USER, content=text)]


def _system_as_text(system: str | list[dict[str, Any]]) -> str:
    """The backend may pass system as a plain string or a list of content blocks."""
    if isinstance(system, str):
        return system
    return "\n".join(str(b.get("text", "")) for b in system)


# ── Loop control ──────────────────────────────────────────────────────────────


class TestTurnLimit:
    async def test_raises_when_max_turns_exceeded_without_end(self, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("x")
        backend = AgentLoopBackend(max_turns=3, workspace_dir=str(tmp_path))
        looping = _response(
            stop_reason="tool_use",
            text=None,
            tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "x.txt"}}],
        )
        _install_mock_client(backend, [looping, looping, looping])
        with pytest.raises(RuntimeError, match="max_turns"):
            await backend.complete(_user("hi"), model="claude-sonnet-4-6")

    async def test_ends_on_end_turn(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(max_turns=5, workspace_dir=str(tmp_path))
        _install_mock_client(backend, [_response(text="done")])
        out = await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        assert out.content == "done"
        assert out.finish_reason == "end_turn"


class TestStopReasonHandling:
    async def test_tool_use_continues_loop(self, tmp_path: Path) -> None:
        """After a tool_use turn, the loop must feed tool_result back and continue."""
        (tmp_path / "hello.txt").write_text("world")
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        turn_1 = _response(
            stop_reason="tool_use",
            text=None,
            tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "hello.txt"}}],
        )
        turn_2 = _response(stop_reason="end_turn", text="I read it.")
        create = _install_mock_client(backend, [turn_1, turn_2])
        out = await backend.complete(_user("read hello.txt"), model="claude-sonnet-4-6")
        assert out.content == "I read it."
        # Second turn's messages must include the tool_result from the first call.
        second_call_messages = create.call_args_list[1].kwargs["messages"]
        last_user = second_call_messages[-1]
        assert last_user["role"] == "user"
        assert isinstance(last_user["content"], list)
        tr = last_user["content"][0]
        assert tr["type"] == "tool_result"
        assert tr["tool_use_id"] == "t1"
        assert "world" in tr["content"]

    async def test_unknown_stop_reason_terminates_without_raising(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        _install_mock_client(backend, [_response(stop_reason="length", text="partial")])
        out = await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        assert out.finish_reason == "length"
        assert out.content == "partial"


# ── System prompt + caching ───────────────────────────────────────────────────


class TestSystemPrompt:
    async def test_workspace_hint_included(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        blob = _system_as_text(create.call_args.kwargs["system"])
        assert str(tmp_path) in blob

    async def test_claude_md_injected_when_present(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("Project rules: use ruff.")
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        blob = _system_as_text(create.call_args.kwargs["system"])
        assert "Project rules: use ruff." in blob

    async def test_contributing_used_when_no_claude_md(self, tmp_path: Path) -> None:
        (tmp_path / "CONTRIBUTING.md").write_text("Contributor guide here.")
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        blob = _system_as_text(create.call_args.kwargs["system"])
        assert "Contributor guide here." in blob


class TestPromptCaching:
    async def test_cache_control_attached_by_default(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        system = create.call_args.kwargs["system"]
        assert isinstance(system, list)
        assert system[-1].get("cache_control") == {"type": "ephemeral"}

    async def test_cache_control_disabled_when_flag_off(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            enable_prompt_caching=False,
        )
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        system = create.call_args.kwargs["system"]
        if isinstance(system, list):
            assert all("cache_control" not in b for b in system)


# ── Memory injection ──────────────────────────────────────────────────────────


class TestMemoryInjection:
    async def test_memory_assemble_context_is_called_when_memory_set(self, tmp_path: Path) -> None:
        memory = MagicMock()
        memory.assemble_context = MagicMock(return_value="## Prior knowledge\nBe careful.")
        backend = AgentLoopBackend(workspace_dir=str(tmp_path), memory=memory)
        create = _install_mock_client(backend, [_response()])
        await backend.complete(
            _user("fix #42"),
            model="claude-sonnet-4-6",
            repo="acme/foo",
            agent_id="task-99",
            issue_title="fix 42",
            issue_body="body",
        )
        memory.assemble_context.assert_called_once()
        kwargs = memory.assemble_context.call_args.kwargs
        assert kwargs["repo"] == "acme/foo"
        assert kwargs["task_id"] == "task-99"
        blob = _system_as_text(create.call_args.kwargs["system"])
        assert "Be careful." in blob

    async def test_no_memory_no_call(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")


# ── Budget enforcement ────────────────────────────────────────────────────────


class TestBudgetEnforcement:
    async def test_aborts_when_turn_pushes_over_budget(self, tmp_path: Path) -> None:
        # Budget = $0.001. Each turn uses 1M input tokens * $3/M = $3 -> abort after turn 1.
        (tmp_path / "x").write_text("x")
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            budget_per_story_usd=0.001,
        )
        expensive = _response(
            stop_reason="tool_use",
            text=None,
            tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "x"}}],
            input_tokens=1_000_000,
            output_tokens=0,
        )
        _install_mock_client(backend, [expensive, _response()])
        with pytest.raises(BudgetExceededError, match=r"budget|0\.001"):
            await backend.complete(_user("hi"), model="claude-sonnet-4-6")

    async def test_budget_not_exceeded_completes_normally(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            budget_per_story_usd=10.0,
        )
        cheap = _response(input_tokens=10, output_tokens=5)
        _install_mock_client(backend, [cheap])
        out = await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        assert out.finish_reason == "end_turn"

    async def test_no_budget_means_no_limit(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))  # no budget
        pricey = _response(input_tokens=5_000_000, output_tokens=5_000_000)
        _install_mock_client(backend, [pricey])
        out = await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        assert out.finish_reason == "end_turn"


# ── Wall-clock timeout ───────────────────────────────────────────────────────


class TestWallClockTimeout:
    async def test_aborts_past_deadline(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            wall_clock_timeout_seconds=0.0,
        )
        (tmp_path / "x").write_text("x")
        _install_mock_client(
            backend,
            [
                _response(
                    stop_reason="tool_use",
                    text=None,
                    tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "x"}}],
                )
            ],
        )
        with pytest.raises(WallClockTimeoutError):
            await backend.complete(_user("hi"), model="claude-sonnet-4-6")

    async def test_deadline_not_hit_completes(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            wall_clock_timeout_seconds=60.0,
        )
        _install_mock_client(backend, [_response()])
        out = await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        assert out.finish_reason == "end_turn"


# ── Cost ledger ──────────────────────────────────────────────────────────────


class TestCostLedger:
    async def test_ledger_record_called_per_turn(self, tmp_path: Path) -> None:
        ledger = MagicMock()
        ledger.record = MagicMock()
        backend = AgentLoopBackend(workspace_dir=str(tmp_path), ledger=ledger)
        (tmp_path / "hi").write_text("x")
        _install_mock_client(
            backend,
            [
                _response(
                    stop_reason="tool_use",
                    text=None,
                    tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "hi"}}],
                ),
                _response(text="done"),
            ],
        )
        await backend.complete(
            _user("hi"),
            model="claude-sonnet-4-6",
            repo="acme/foo",
            agent_id="task-1",
        )
        assert ledger.record.call_count == 2  # one per turn
        rec = ledger.record.call_args_list[0].args[0]
        assert rec.repo == "acme/foo"
        assert rec.agent_id == "task-1"
        assert rec.backend == "agent-loop"


# ── Tool registry integration ───────────────────────────────────────────────


class TestToolRegistryIntegration:
    async def test_tools_param_derived_from_registry(self, tmp_path: Path) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        create = _install_mock_client(backend, [_response()])
        await backend.complete(_user("hi"), model="claude-sonnet-4-6")
        tools = create.call_args.kwargs["tools"]
        names = {t["name"] for t in tools}
        assert {
            "read_file",
            "write_file",
            "edit_file",
            "glob_files",
            "grep_files",
            "run_bash",
        } <= names
        for t in tools:
            assert "input_schema" in t

    async def test_tool_invocation_goes_through_registry(self, tmp_path: Path) -> None:
        (tmp_path / "target.txt").write_text("payload-from-registry")
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        _install_mock_client(
            backend,
            [
                _response(
                    stop_reason="tool_use",
                    text=None,
                    tool_calls=[{"id": "t1", "name": "read_file", "input": {"path": "target.txt"}}],
                ),
                _response(text="got it"),
            ],
        )
        create = backend._client.messages.create  # type: ignore[attr-defined]
        await backend.complete(_user("read"), model="claude-sonnet-4-6")
        second_call = create.call_args_list[1].kwargs["messages"]
        tool_result = second_call[-1]["content"][0]
        assert tool_result["type"] == "tool_result"
        assert "payload-from-registry" in tool_result["content"]


class TestAsyncClient:
    def test_client_is_async(self, tmp_path: Path) -> None:
        """The underlying Anthropic client must be AsyncAnthropic, not Anthropic.

        The loop is an async function; the sync client would block the event loop.
        """
        import anthropic

        backend = AgentLoopBackend(workspace_dir=str(tmp_path))
        assert isinstance(backend._client, anthropic.AsyncAnthropic)


# ── Capabilities ─────────────────────────────────────────────────────────────


class TestCapabilities:
    def test_reports_tool_use(self, tmp_path: Path) -> None:
        caps = AgentLoopBackend(workspace_dir=str(tmp_path)).capabilities("claude-sonnet-4-6")
        assert caps.supports_tool_use is True
        assert caps.supports_system_prompt is True
