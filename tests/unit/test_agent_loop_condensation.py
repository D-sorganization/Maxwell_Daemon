"""Tests for AgentLoopBackend's integration with Condenser.

Condenser itself is tested in ``test_condensation.py``. Here we verify the
loop calls it at the right moment and threads the result back in.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from maxwell_daemon.backends.agent_loop import AgentLoopBackend
from maxwell_daemon.backends.base import Message, MessageRole
from maxwell_daemon.backends.condensation import Condenser


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _response(
    *,
    stop_reason: str = "end_turn",
    text: str | None = "done",
    input_tokens: int = 10_000,
    output_tokens: int = 100,
) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.model = "claude-sonnet-4-6"
    resp.usage = MagicMock()
    resp.usage.input_tokens = input_tokens
    resp.usage.output_tokens = output_tokens
    resp.usage.cache_read_input_tokens = 0
    if text is None:
        resp.content = []
    else:
        block = MagicMock()
        block.type = "text"
        block.text = text
        resp.content = [block]
    return resp


def _install_mock_client(
    backend: AgentLoopBackend, responses: list[MagicMock]
) -> AsyncMock:
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    backend._client = client
    return client.messages.create  # type: ignore[no-any-return]


class _FakeCondenser:
    """Test double — records ``should_condense`` calls and ``condense`` invocations."""

    def __init__(self, *, trigger_at_tokens: int) -> None:
        self._trigger = trigger_at_tokens
        self.should_condense_calls: list[int] = []
        self.condense_calls: int = 0
        self.last_output: list[dict[str, Any]] | None = None

    def should_condense(self, total_tokens: int) -> bool:
        self.should_condense_calls.append(total_tokens)
        return total_tokens >= self._trigger

    async def condense(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.condense_calls += 1
        shrunk: list[dict[str, Any]] = [{"role": "user", "content": "[condensed]"}]
        self.last_output = shrunk
        return shrunk


class TestCondenserIntegration:
    async def test_not_called_when_no_condenser_configured(
        self, tmp_path: Path
    ) -> None:
        backend = AgentLoopBackend(workspace_dir=str(tmp_path))  # no condenser
        _install_mock_client(backend, [_response()])
        await backend.complete(
            [Message(role=MessageRole.USER, content="hi")], model="claude-sonnet-4-6"
        )

    async def test_called_when_token_threshold_hit(self, tmp_path: Path) -> None:
        cond = _FakeCondenser(trigger_at_tokens=5_000)
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            condenser=cond,  # type: ignore[arg-type]
        )
        # Two turns: tool_use → end_turn. First turn reports 10k input_tokens
        # (> 5k trigger), so condense runs before turn 2.
        turn_a = _response(stop_reason="tool_use", text=None, input_tokens=10_000)
        turn_b = _response(stop_reason="end_turn", text="done", input_tokens=100)
        create = _install_mock_client(backend, [turn_a, turn_b])
        await backend.complete(
            [Message(role=MessageRole.USER, content="do it")],
            model="claude-sonnet-4-6",
        )
        assert cond.should_condense_calls, "condenser should have been consulted"
        # Trigger fires on turn 2 after turn 1's usage accumulated.
        assert cond.condense_calls >= 1
        # Turn 2's messages must include the condensed list.
        turn_2_msgs = create.call_args_list[1].kwargs["messages"]
        assert any(m.get("content") == "[condensed]" for m in turn_2_msgs)

    async def test_not_called_below_threshold(self, tmp_path: Path) -> None:
        cond = _FakeCondenser(trigger_at_tokens=100_000)
        backend = AgentLoopBackend(
            workspace_dir=str(tmp_path),
            condenser=cond,  # type: ignore[arg-type]
        )
        turn_a = _response(stop_reason="tool_use", text=None, input_tokens=1_000)
        turn_b = _response(stop_reason="end_turn", text="done", input_tokens=100)
        _install_mock_client(backend, [turn_a, turn_b])
        await backend.complete(
            [Message(role=MessageRole.USER, content="do it")],
            model="claude-sonnet-4-6",
        )
        # Consulted but never triggered.
        assert cond.should_condense_calls
        assert cond.condense_calls == 0


class TestCondenserWiring:
    def test_accepts_real_condenser(self, tmp_path: Path) -> None:
        async def summarizer(_: list[dict[str, object]]) -> str:
            return "summary"

        cond = Condenser(threshold_tokens=100_000, keep_recent=5, summarizer=summarizer)
        backend = AgentLoopBackend(workspace_dir=str(tmp_path), condenser=cond)
        assert backend._condenser is cond
