from __future__ import annotations

from unittest.mock import patch

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.gemini import GeminiBackend
from maxwell_daemon.backends.groq import GroqBackend
from maxwell_daemon.backends.mistral import MistralBackend


def test_gemini_backend_reports_missing_sdk() -> None:
    with (
        patch(
            "maxwell_daemon.backends.gemini.import_module",
            side_effect=ModuleNotFoundError("google.generativeai"),
        ),
        pytest.raises(
            BackendUnavailableError, match="google-generativeai SDK not installed"
        ),
    ):
        GeminiBackend(api_key="test-key")


def test_groq_backend_reports_missing_sdk() -> None:
    with (
        patch(
            "maxwell_daemon.backends.groq.import_module",
            side_effect=ModuleNotFoundError("groq"),
        ),
        pytest.raises(BackendUnavailableError, match="groq SDK not installed"),
    ):
        GroqBackend(api_key="test-key")


def test_mistral_backend_reports_missing_sdk() -> None:
    with (
        patch(
            "maxwell_daemon.backends.mistral.import_module",
            side_effect=ModuleNotFoundError("mistralai"),
        ),
        pytest.raises(BackendUnavailableError, match="mistralai SDK not installed"),
    ):
        MistralBackend(api_key="test-key")
