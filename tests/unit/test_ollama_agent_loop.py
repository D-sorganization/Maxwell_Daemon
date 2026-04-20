"""Tests for OllamaAgentLoopBackend — multi-turn tool-use against a local model.

Ollama's OpenAI-compatible endpoint (``/v1/chat/completions``) speaks the
same JSON shape as OpenAI: we use ``registry.to_openai()`` for tool
schemas and drive a tool_use loop the same way as the Anthropic agent
loop.

All HTTP is mocked via an injected async client so tests never touch
localhost:11434.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends.base import Message, MessageRole
from maxwell_daemon.backends.ollama_agent_loop import OllamaAgentLoopBackend

# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Records every POST and returns canned JSON payloads in order."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = list(payloads)
        self.posts: list[dict[str, Any]] = []

    async def post(
        self, url: str, json: dict[str, Any], headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.posts.append({"url": url, "json": json, "headers": headers or {}})
        if not self._payloads:
            raise AssertionError("unexpected extra POST — ran out of canned responses")
        return _FakeResponse(self._payloads.pop(0))

    async def aclose(self) -> None:
        pass


def _chat_completion(
    *,
    content: str | None = "done",
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 20,
    completion_tokens: int = 5,
) -> dict[str, Any]:
    """Construct a canned OpenAI-shaped chat.completions response."""
    message: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-x",
        "model": "devstral",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _user(text: str) -> list[Message]:
    return [Message(role=MessageRole.USER, content=text)]


# ── Construction ────────────────────────────────────────────────────────────


class TestInit:
    def test_default_base_url(self, tmp_path: Path) -> None:
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path))
        assert backend.base_url == "http://localhost:11434/v1"

    def test_custom_base_url(self, tmp_path: Path) -> None:
        backend = OllamaAgentLoopBackend(
            workspace_dir=str(tmp_path), base_url="http://node.local:22222/v1"
        )
        assert backend.base_url == "http://node.local:22222/v1"

    def test_default_model_is_devstral(self, tmp_path: Path) -> None:
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path))
        assert backend.default_model == "devstral"


# ── Loop control ────────────────────────────────────────────────────────────


class TestLoopControl:
    async def test_ends_on_finish_reason_stop(self, tmp_path: Path) -> None:
        client = _FakeClient([_chat_completion(content="all done")])
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path), client=client)
        out = await backend.complete(_user("hi"))
        assert out.content == "all done"
        assert out.finish_reason == "stop"

    async def test_tool_use_continues_loop(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("world")
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "hello.txt"}'},
        }
        client = _FakeClient(
            [
                _chat_completion(
                    content=None,
                    tool_calls=[tool_call],
                    finish_reason="tool_calls",
                ),
                _chat_completion(content="I read it."),
            ]
        )
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path), client=client)
        out = await backend.complete(_user("read hello.txt"))
        assert out.content == "I read it."
        # Second POST must carry a tool result for call_1.
        assert len(client.posts) == 2
        follow_up_msgs = client.posts[1]["json"]["messages"]
        tool_msg = follow_up_msgs[-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "call_1"
        assert "world" in tool_msg["content"]

    async def test_max_turns_raises_when_stuck_in_tool_loop(self, tmp_path: Path) -> None:
        (tmp_path / "x").write_text("x")
        looping_call = {
            "id": "call_x",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "x"}'},
        }
        client = _FakeClient(
            [
                _chat_completion(
                    content=None, tool_calls=[looping_call], finish_reason="tool_calls"
                ),
                _chat_completion(
                    content=None, tool_calls=[looping_call], finish_reason="tool_calls"
                ),
                _chat_completion(
                    content=None, tool_calls=[looping_call], finish_reason="tool_calls"
                ),
            ]
        )
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path), client=client, max_turns=3)
        with pytest.raises(RuntimeError, match="max_turns"):
            await backend.complete(_user("hi"))


# ── Tool schema format ──────────────────────────────────────────────────────


class TestToolSchemas:
    async def test_uses_openai_function_calling_shape(self, tmp_path: Path) -> None:
        client = _FakeClient([_chat_completion(content="ok")])
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path), client=client)
        await backend.complete(_user("hi"))
        tools = client.posts[0]["json"]["tools"]
        # OpenAI shape: {"type": "function", "function": {"name", "parameters", ...}}
        for t in tools:
            assert t["type"] == "function"
            assert "name" in t["function"]
            assert "parameters" in t["function"]
        names = {t["function"]["name"] for t in tools}
        assert {
            "read_file",
            "write_file",
            "edit_file",
            "glob_files",
            "grep_files",
            "run_bash",
        } <= names


# ── Capabilities ────────────────────────────────────────────────────────────


class TestCapabilities:
    def test_is_local_true(self, tmp_path: Path) -> None:
        caps = OllamaAgentLoopBackend(workspace_dir=str(tmp_path)).capabilities("devstral")
        assert caps.is_local is True
        assert caps.cost_per_1k_input_tokens == 0.0
        assert caps.cost_per_1k_output_tokens == 0.0
        assert caps.supports_tool_use is True


class TestAclose:
    """``aclose`` must close the injected HTTP client — otherwise a daemon
    that recreates backends leaks TCP connections."""

    async def test_aclose_awaits_underlying_client(self, tmp_path: Path) -> None:
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.aclose = AsyncMock()
        backend = OllamaAgentLoopBackend(workspace_dir=str(tmp_path), client=client)
        await backend.aclose()
        client.aclose.assert_awaited_once()
