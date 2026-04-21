"""ClaudeBackend — configuration, system-prompt splitting, capabilities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from maxwell_daemon.backends.base import (
    BackendUnavailableError,
    Message,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.backends.claude import ClaudeBackend
from maxwell_daemon.backends.registry import registry


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


class TestConfiguration:
    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(BackendUnavailableError):
            ClaudeBackend()

    def test_accepts_explicit_key(self) -> None:
        backend = ClaudeBackend(api_key="sk-test-explicit")
        assert backend is not None


class TestSystemPromptSplit:
    def test_single_system_message_extracted(self) -> None:
        backend = ClaudeBackend()
        sys, msgs = backend._split_system(
            [
                Message(role=MessageRole.SYSTEM, content="be helpful"),
                Message(role=MessageRole.USER, content="hi"),
            ]
        )
        assert sys == "be helpful"
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_multiple_system_messages_concatenated(self) -> None:
        backend = ClaudeBackend()
        sys, msgs = backend._split_system(
            [
                Message(role=MessageRole.SYSTEM, content="rule 1"),
                Message(role=MessageRole.SYSTEM, content="rule 2"),
                Message(role=MessageRole.USER, content="hi"),
            ]
        )
        assert "rule 1" in sys
        assert "rule 2" in sys
        assert len(msgs) == 1

    def test_no_system_returns_none(self) -> None:
        backend = ClaudeBackend()
        sys, msgs = backend._split_system([Message(role=MessageRole.USER, content="hi")])
        assert sys is None
        assert len(msgs) == 1


class TestCapabilities:
    def test_opus_pricing(self) -> None:
        caps = ClaudeBackend().capabilities("claude-opus-4-7")
        assert caps.cost_per_1k_input_tokens == pytest.approx(0.015)
        assert caps.cost_per_1k_output_tokens == pytest.approx(0.075)
        assert caps.max_context_tokens == 1_000_000

    def test_haiku_cheaper_than_sonnet(self) -> None:
        haiku = ClaudeBackend().capabilities("claude-haiku-4-5")
        sonnet = ClaudeBackend().capabilities("claude-sonnet-4-6")
        assert haiku.cost_per_1k_input_tokens < sonnet.cost_per_1k_input_tokens

    def test_all_support_vision_and_tools(self) -> None:
        for model in ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"):
            caps = ClaudeBackend().capabilities(model)
            assert caps.supports_vision is True
            assert caps.supports_tool_use is True

    def test_unknown_model_has_safe_defaults(self) -> None:
        caps = ClaudeBackend().capabilities("claude-future-x")
        assert caps.cost_per_1k_input_tokens > 0
        assert caps.max_context_tokens >= 100_000


class TestCostEstimation:
    def test_cost_includes_both_directions(self) -> None:
        backend = ClaudeBackend()
        cost = backend.estimate_cost(
            TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
            "claude-sonnet-4-6",
        )
        # 1k input @ $3/M + 500 output @ $15/M = 0.003 + 0.0075 = 0.0105
        assert cost == pytest.approx(0.0105, rel=1e-3)


class _FakeClaudeStream:
    def __init__(self, values: list[str]) -> None:
        self.text_stream = self._iter(values)

    async def _iter(self, values: list[str]) -> object:
        for v in values:
            yield v

    async def __aenter__(self) -> _FakeClaudeStream:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class TestRequestPaths:
    @pytest.mark.asyncio
    async def test_complete_maps_text_blocks_and_usage(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[dict[str, object]] = []

        async def fake_create(**kwargs: object) -> object:
            calls.append(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="alpha"),
                    SimpleNamespace(type="image", text="skip"),
                    SimpleNamespace(type="text", text="beta"),
                ],
                stop_reason=None,
                usage=SimpleNamespace(
                    input_tokens=5,
                    output_tokens=8,
                    cache_read_input_tokens=None,
                ),
                model="claude-haiku-4-5",
                model_dump=lambda: {"ok": True},
            )

        fake_messages = SimpleNamespace(
            create=fake_create,
            stream=lambda **_: _FakeClaudeStream(["x", "y"]),
        )
        fake_client = SimpleNamespace(messages=fake_messages)
        monkeypatch.setattr(
            "maxwell_daemon.backends.claude.anthropic.AsyncAnthropic",
            lambda **_: fake_client,
        )

        backend = ClaudeBackend(api_key="x")
        out = await backend.complete(
            [
                Message(role=MessageRole.SYSTEM, content="policy"),
                Message(role=MessageRole.USER, content="hi"),
            ],
            model="claude-haiku-4-5",
            tools=[{"name": "x"}],
            metadata={"trace": "1"},
        )

        assert out.content == "alphabeta"
        assert out.finish_reason == "stop"
        assert out.usage.total_tokens == 13
        assert out.raw == {"ok": True}
        assert calls and calls[0]["tools"] == [{"name": "x"}]
        assert calls[0]["metadata"] == {"trace": "1"}

    @pytest.mark.asyncio
    async def test_stream_and_health_check_paths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def ok_create(**_: object) -> object:
            return SimpleNamespace()

        fake_messages = SimpleNamespace(
            create=ok_create,
            stream=lambda **_: _FakeClaudeStream(["part-1", "part-2"]),
        )
        fake_client = SimpleNamespace(messages=fake_messages)
        monkeypatch.setattr(
            "maxwell_daemon.backends.claude.anthropic.AsyncAnthropic",
            lambda **_: fake_client,
        )
        backend = ClaudeBackend(api_key="x")

        parts = [p async for p in backend.stream([], model="claude-haiku-4-5")]
        assert parts == ["part-1", "part-2"]
        assert await backend.health_check() is True

        async def broken_create(**_: object) -> object:
            raise RuntimeError("down")

        fake_client.messages = SimpleNamespace(create=broken_create, stream=fake_messages.stream)
        assert await backend.health_check() is False


class TestRegistry:
    def test_registered_under_claude(self) -> None:
        assert "claude" in registry.available()
