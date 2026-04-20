"""AzureOpenAIBackend — configuration semantics and registry hookup.

Azure uses the same wire protocol as OpenAI, so the adapter exists mainly to
validate Azure-specific config (endpoint, api_version, deployment name) and
route through ``AsyncAzureOpenAI`` instead of the vanilla client.
"""

from __future__ import annotations

import pytest

from maxwell_daemon.backends.azure import AzureOpenAIBackend
from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.registry import registry


class TestConfiguration:
    def test_requires_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        with pytest.raises(BackendUnavailableError, match="endpoint"):
            AzureOpenAIBackend(api_version="2024-10-21")

    def test_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
        with pytest.raises(BackendUnavailableError, match="api_key"):
            AzureOpenAIBackend(endpoint="https://x.openai.azure.com", api_version="2024-10-21")

    def test_reads_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        b = AzureOpenAIBackend(api_version="2024-10-21")
        assert b is not None


class TestRegistryIntegration:
    def test_registered_under_azure(self) -> None:
        assert "azure" in registry.available()

    def test_capabilities_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com")
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
        b = AzureOpenAIBackend(api_version="2024-10-21")
        caps = b.capabilities("gpt-4o")
        assert caps.supports_streaming is True
        assert caps.is_local is False
