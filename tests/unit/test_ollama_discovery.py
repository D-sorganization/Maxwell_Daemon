"""Tests for Ollama local-model discovery."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from maxwell_daemon.backends.ollama_discovery import (
    OllamaModelInfo,
    adiscover_ollama_models,
    ais_ollama_available,
    discover_ollama_models,
    is_ollama_available,
)

_FAKE_BASE = "http://fake-ollama:11434"

_TAGS_RESPONSE = {
    "models": [
        {
            "name": "llama3.1:8b",
            "size": 4_800_000_000,
            "digest": "sha256:abc123",
            "details": {"family": "llama", "parameter_size": "8B"},
        },
        {
            "name": "mistral:7b",
            "size": 4_100_000_000,
            "digest": "sha256:def456",
            "details": {"family": "mistral", "parameter_size": "7B"},
        },
    ]
}


class TestDiscoverOllamaModels:
    def test_returns_list_of_model_info(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert len(models) == 2
        assert all(isinstance(m, OllamaModelInfo) for m in models)

    def test_model_names_parsed(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert models[0].name == "llama3.1:8b"
        assert models[1].name == "mistral:7b"

    def test_size_bytes_parsed(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert models[0].size_bytes == 4_800_000_000

    def test_size_gb_property(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        # 4_800_000_000 / 1_073_741_824 ~ 4.47 GB
        assert 4.0 < models[0].size_gb < 5.0

    def test_family_property_strips_tag(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert models[0].family == "llama3.1"
        assert models[1].family == "mistral"

    def test_digest_parsed(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert models[0].digest == "sha256:abc123"

    def test_details_parsed(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE)
        assert models[0].details["family"] == "llama"

    def test_empty_models_list(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json={"models": []})
            models = discover_ollama_models(_FAKE_BASE)
        assert models == []

    def test_network_error_returns_empty_list(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
            models = discover_ollama_models(_FAKE_BASE)
        assert models == []

    def test_non_200_returns_empty_list(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(500)
            models = discover_ollama_models(_FAKE_BASE)
        assert models == []

    def test_trailing_slash_on_endpoint_handled(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = discover_ollama_models(_FAKE_BASE + "/")
        assert len(models) == 2


class TestIsOllamaAvailable:
    def test_returns_true_on_200(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json={"models": []})
            assert is_ollama_available(_FAKE_BASE) is True

    def test_returns_false_on_network_error(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
            assert is_ollama_available(_FAKE_BASE) is False

    def test_returns_false_on_non_200(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(503)
            assert is_ollama_available(_FAKE_BASE) is False


class TestAsyncDiscover:
    def test_async_returns_same_results(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json=_TAGS_RESPONSE)
            models = asyncio.run(adiscover_ollama_models(_FAKE_BASE))
        assert len(models) == 2
        assert models[0].name == "llama3.1:8b"

    def test_async_network_error_returns_empty(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
            models = asyncio.run(adiscover_ollama_models(_FAKE_BASE))
        assert models == []

    def test_async_is_available_true_on_200(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").respond(200, json={"models": []})
            result = asyncio.run(ais_ollama_available(_FAKE_BASE))
        assert result is True

    def test_async_is_available_false_on_error(self) -> None:
        with respx.mock(base_url=_FAKE_BASE) as mock:
            mock.get("/api/tags").mock(side_effect=httpx.ConnectError("refused"))
            result = asyncio.run(ais_ollama_available(_FAKE_BASE))
        assert result is False


class TestOllamaModelInfo:
    def test_family_no_tag_returns_full_name(self) -> None:
        m = OllamaModelInfo(name="devstral")
        assert m.family == "devstral"

    def test_size_gb_zero_when_no_size(self) -> None:
        m = OllamaModelInfo(name="tiny-model")
        assert m.size_gb == 0.0

    def test_default_digest_is_empty_string(self) -> None:
        m = OllamaModelInfo(name="x")
        assert m.digest == ""

    def test_env_var_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OLLAMA_HOST", _FAKE_BASE)
        from maxwell_daemon.backends.ollama_discovery import _endpoint

        assert _endpoint() == _FAKE_BASE
