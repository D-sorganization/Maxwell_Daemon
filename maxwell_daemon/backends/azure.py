"""Azure OpenAI backend — inherits from OpenAIBackend and overrides the client.

Azure uses the same wire protocol as OpenAI; the only differences are:
* a deployment-specific endpoint URL
* an API-version header
* a different SDK client class (``AsyncAzureOpenAI``)

So we reuse every behavior from :class:`OpenAIBackend` and only override what's
Azure-specific. That's the payoff of an interface-driven design: new providers
are additions, not rewrites.
"""

from __future__ import annotations

import os

import openai

from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.openai import OpenAIBackend
from maxwell_daemon.backends.registry import registry


class AzureOpenAIBackend(OpenAIBackend):
    name = "azure"

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str = "2024-10-21",
        timeout: float = 120.0,
    ) -> None:
        # Skip OpenAIBackend.__init__ — we wire a different client class.
        ep = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not ep:
            raise BackendUnavailableError(
                "Azure OpenAI requires an endpoint (AZURE_OPENAI_ENDPOINT or endpoint kwarg)."
            )
        key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not key:
            raise BackendUnavailableError(
                "Azure OpenAI requires an api_key (AZURE_OPENAI_API_KEY or api_key kwarg)."
            )
        self._client = openai.AsyncAzureOpenAI(
            api_key=key,
            azure_endpoint=ep,
            api_version=api_version,
            timeout=timeout,
        )


registry.register("azure", AzureOpenAIBackend)
