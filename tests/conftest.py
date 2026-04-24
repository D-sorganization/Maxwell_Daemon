"""Shared test fixtures.

Reusable building blocks kept here so individual test modules stay focused on
behavior rather than scaffolding.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends import (
    BackendCapabilities,
    BackendResponse,
    ILLMBackend,
    Message,
    TokenUsage,
    registry,
)
from maxwell_daemon.config import MaxwellDaemonConfig


class RecordingBackend(ILLMBackend):
    """Test double that records calls and returns canned responses."""

    name = "recording"

    def __init__(
        self,
        *,
        response_text: str = "ok",
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
        healthy: bool = True,
        raise_on_complete: Exception | None = None,
        **_: Any,
    ) -> None:
        self.response_text = response_text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._healthy = healthy
        self._raise = raise_on_complete
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **kwargs: Any,
    ) -> BackendResponse:
        self.calls.append({"messages": messages, "model": model, "kwargs": kwargs})
        if self._raise is not None:
            raise self._raise
        return BackendResponse(
            content=self.response_text,
            finish_reason="stop",
            usage=TokenUsage(
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
                total_tokens=self._prompt_tokens + self._completion_tokens,
            ),
            model=model,
            backend=self.name,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        for chunk in self.response_text:
            yield chunk

    async def health_check(self) -> bool:
        return self._healthy

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(
            cost_per_1k_input_tokens=0.001,
            cost_per_1k_output_tokens=0.002,
        )


@pytest.fixture
def register_recording_backend() -> Iterator[None]:
    registry._factories["recording"] = RecordingBackend
    yield
    registry._factories.pop("recording", None)


@pytest.fixture
def minimal_config(register_recording_backend: None) -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "test-model"},
            },
            "agent": {"default_backend": "primary"},
        }
    )


@pytest.fixture
def dual_backend_config(register_recording_backend: None) -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "recording", "model": "model-primary"},
                "local": {"type": "recording", "model": "model-local"},
            },
            "agent": {"default_backend": "primary"},
        }
    )


@pytest.fixture
def isolated_ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"




import structlog
import logging
from collections.abc import Iterator
import pytest

@pytest.fixture(autouse=True)
def _structlog_test_config() -> Iterator[None]:
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    yield
    structlog.reset_defaults()
    logging.getLogger().handlers.clear()
