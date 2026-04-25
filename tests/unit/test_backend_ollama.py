"""OllamaBackend adapter — mocked HTTP transport via respx."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.backends.ollama import OllamaBackend


@pytest.fixture
def backend() -> OllamaBackend:
    return OllamaBackend(endpoint="http://fake:11434")


class TestComplete:
    def test_happy_path(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.post("/api/chat").respond(
                200,
                json={
                    "model": "llama3.1",
                    "message": {"role": "assistant", "content": "hello from ollama"},
                    "done": True,
                    "prompt_eval_count": 10,
                    "eval_count": 20,
                },
            )
            resp = asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="llama3.1",
                )
            )

        assert resp.content == "hello from ollama"
        assert resp.usage.prompt_tokens == 10
        assert resp.usage.completion_tokens == 20
        assert resp.usage.total_tokens == 30
        assert resp.finish_reason == "stop"
        assert resp.model == "llama3.1"
        assert resp.backend == "ollama"

    def test_not_done_means_length_finish(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.post("/api/chat").respond(
                200,
                json={
                    "model": "llama3.1",
                    "message": {"role": "assistant", "content": "partial"},
                    "done": False,
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
            resp = asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="llama3.1",
                )
            )

        assert resp.finish_reason == "length"

    def test_network_error_raises_backend_unavailable(
        self, backend: OllamaBackend
    ) -> None:
        from maxwell_daemon.backends.base import BackendUnavailableError

        with respx.mock(base_url="http://fake:11434") as mock:
            mock.post("/api/chat").mock(side_effect=httpx.ConnectError("refused"))
            with pytest.raises(BackendUnavailableError, match="request failed"):
                asyncio.run(
                    backend.complete(
                        [Message(role=MessageRole.USER, content="hi")],
                        model="llama3.1",
                    )
                )

    def test_system_message_passed_through(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            route = mock.post("/api/chat").respond(
                200,
                json={
                    "model": "llama3.1",
                    "message": {"role": "assistant", "content": "ok"},
                    "done": True,
                    "prompt_eval_count": 1,
                    "eval_count": 1,
                },
            )
            asyncio.run(
                backend.complete(
                    [
                        Message(role=MessageRole.SYSTEM, content="be terse"),
                        Message(role=MessageRole.USER, content="hi"),
                    ],
                    model="llama3.1",
                    temperature=0.3,
                )
            )

        body = json.loads(route.calls.last.request.content)
        assert body["model"] == "llama3.1"
        assert body["messages"][0]["role"] == "system"
        assert body["options"]["temperature"] == 0.3

    def test_max_tokens_maps_to_num_predict(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            route = mock.post("/api/chat").respond(
                200,
                json={
                    "model": "llama3.1",
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "prompt_eval_count": 0,
                    "eval_count": 0,
                },
            )
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="llama3.1",
                    max_tokens=256,
                )
            )
        body = json.loads(route.calls.last.request.content)
        assert body["options"]["num_predict"] == 256


class TestHealthCheck:
    def test_returns_true_when_endpoint_reachable(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.get("/api/tags").respond(200, json={"models": []})
            assert asyncio.run(backend.health_check()) is True

    def test_returns_false_on_network_error(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("no"))
            assert asyncio.run(backend.health_check()) is False

    def test_returns_false_on_non_200(self, backend: OllamaBackend) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.get("/api/tags").respond(500)
            assert asyncio.run(backend.health_check()) is False


class TestStream:
    def test_yields_message_content_from_streaming_lines(
        self, backend: OllamaBackend
    ) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.post("/api/chat").respond(
                200,
                content=(
                    '{"message": {"content": "hello"}, "done": false}\n'
                    '{"message": {"content": " world"}, "done": true}\n'
                ),
            )

            async def collect() -> list[str]:
                return [
                    chunk
                    async for chunk in backend.stream(
                        [Message(role=MessageRole.USER, content="hi")],
                        model="llama3.1",
                    )
                ]

            assert asyncio.run(collect()) == ["hello", " world"]

    def test_stream_skips_empty_lines_and_empty_messages(
        self, backend: OllamaBackend
    ) -> None:
        with respx.mock(base_url="http://fake:11434") as mock:
            mock.post("/api/chat").respond(
                200,
                content=(
                    "\n"
                    '{"message": {}, "done": false}\n'
                    '{"message": {"content": "kept"}, "done": true}\n'
                ),
            )

            async def collect() -> list[str]:
                return [
                    chunk
                    async for chunk in backend.stream(
                        [Message(role=MessageRole.USER, content="hi")],
                        model="llama3.1",
                    )
                ]

            assert asyncio.run(collect()) == ["kept"]


class TestCapabilities:
    def test_llama3_has_tool_use(self, backend: OllamaBackend) -> None:
        caps = backend.capabilities("llama3.1:70b")
        assert caps.supports_tool_use is True
        assert caps.is_local is True
        assert caps.cost_per_1k_input_tokens == 0.0
        assert caps.cost_per_1k_output_tokens == 0.0

    def test_llava_has_vision(self, backend: OllamaBackend) -> None:
        caps = backend.capabilities("llava:13b")
        assert caps.supports_vision is True

    def test_unknown_model_has_safe_defaults(self, backend: OllamaBackend) -> None:
        caps = backend.capabilities("obscure-model")
        assert caps.supports_vision is False
        assert caps.supports_tool_use is False
        assert caps.is_local is True


class TestEndpointDefaults:
    def test_uses_localhost_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        b = OllamaBackend()
        assert b._endpoint == "http://localhost:11434"

    def test_strips_trailing_slash(self) -> None:
        b = OllamaBackend(endpoint="http://x:1/")
        assert b._endpoint == "http://x:1"

    def test_adds_scheme_to_schemeless_endpoint(self) -> None:
        b = OllamaBackend(endpoint="0.0.0.0:11434")
        assert b._endpoint == "http://0.0.0.0:11434"

    def test_env_var_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OLLAMA_HOST", "http://custom:9999")
        assert OllamaBackend()._endpoint == "http://custom:9999"
