"""Backend router — routing rules and precedence.

Uses a fake backend registered with the global registry so we don't depend on any
real LLM SDK being installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from maxwell_daemon.core import BackendRouter


class _Fake(ILLMBackend):
    name = "fake"

    def __init__(self, **_: Any) -> None:
        pass

    async def complete(self, messages: list[Message], *, model: str, **_: Any) -> BackendResponse:
        return BackendResponse(
            content="",
            finish_reason="stop",
            usage=TokenUsage(),
            model=model,
            backend=self.name,
        )

    async def stream(self, messages: list[Message], *, model: str, **_: Any) -> AsyncIterator[str]:
        if False:
            yield ""

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities()


class _ClosingFake(_Fake):
    def __init__(self, **_: Any) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _ExplodingCloseFake(_Fake):
    def aclose(self) -> None:
        raise RuntimeError("close failed")


@pytest.fixture(autouse=True)
def _register_fakes() -> None:
    # Overwrite any existing registration so the test suite is deterministic.
    registry._factories["fake_a"] = _Fake
    registry._factories["fake_b"] = _Fake
    registry._factories["closing_fake"] = _ClosingFake
    registry._factories["exploding_close_fake"] = _ExplodingCloseFake
    yield
    registry._factories.pop("fake_a", None)
    registry._factories.pop("fake_b", None)
    registry._factories.pop("closing_fake", None)
    registry._factories.pop("exploding_close_fake", None)


@pytest.fixture
def config() -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "primary": {"type": "fake_a", "model": "model-a"},
                "local": {"type": "fake_b", "model": "model-b"},
            },
            "agent": {"default_backend": "primary"},
            "repos": [
                {
                    "name": "private-repo",
                    "path": "/tmp/private",
                    "backend": "local",
                    "model": "model-b",
                },
                {"name": "normal-repo", "path": "/tmp/normal"},
            ],
        }
    )


class TestRouter:
    def test_default_backend(self, config: MaxwellDaemonConfig) -> None:
        decision = BackendRouter(config).route()
        assert decision.backend_name == "primary"
        assert decision.model == "model-a"
        assert "default" in decision.reason

    def test_repo_override(self, config: MaxwellDaemonConfig) -> None:
        decision = BackendRouter(config).route(repo="private-repo")
        assert decision.backend_name == "local"
        assert decision.model == "model-b"
        assert "private-repo" in decision.reason

    def test_repo_without_backend_uses_default(self, config: MaxwellDaemonConfig) -> None:
        decision = BackendRouter(config).route(repo="normal-repo")
        assert decision.backend_name == "primary"

    def test_explicit_override_wins(self, config: MaxwellDaemonConfig) -> None:
        decision = BackendRouter(config).route(repo="private-repo", backend_override="primary")
        assert decision.backend_name == "primary"
        assert "override" in decision.reason

    def test_unknown_override_rejected(self, config: MaxwellDaemonConfig) -> None:
        with pytest.raises(ValueError, match="Unknown backend"):
            BackendRouter(config).route(backend_override="martian")

    def test_model_override(self, config: MaxwellDaemonConfig) -> None:
        decision = BackendRouter(config).route(model_override="custom-model")
        assert decision.model == "custom-model"

    def test_available_backends(self, config: MaxwellDaemonConfig) -> None:
        assert sorted(BackendRouter(config).available_backends()) == [
            "local",
            "primary",
        ]

    def test_instance_cached(self, config: MaxwellDaemonConfig) -> None:
        router = BackendRouter(config)
        a = router.route().backend
        b = router.route().backend
        assert a is b

    def test_disabled_backend_rejected(self, config: MaxwellDaemonConfig) -> None:
        config.backends["primary"].enabled = False
        with pytest.raises(RuntimeError, match="disabled"):
            BackendRouter(config).route()

    def test_passes_raw_api_key_to_backend(self) -> None:
        captured: dict[str, Any] = {}

        class _CapturingFake(_Fake):
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        registry._factories["capturing_fake"] = _CapturingFake
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {
                    "primary": {
                        "type": "capturing_fake",
                        "model": "model-a",
                        "api_key": "secret",
                    }
                },
                "agent": {"default_backend": "primary"},
            }
        )

        BackendRouter(cfg).route()

        assert captured["api_key"] == "secret"

    @pytest.mark.asyncio
    async def test_aclose_all_awaits_async_backend_close(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "closing_fake", "model": "m"}},
                "agent": {"default_backend": "primary"},
            }
        )
        router = BackendRouter(cfg)
        backend = router.route().backend

        await router.aclose_all()

        assert backend.closed is True

    @pytest.mark.asyncio
    async def test_aclose_all_suppresses_backend_close_errors(self) -> None:
        cfg = MaxwellDaemonConfig.model_validate(
            {
                "backends": {"primary": {"type": "exploding_close_fake", "model": "m"}},
                "agent": {"default_backend": "primary"},
            }
        )
        router = BackendRouter(cfg)
        router.route()

        await router.aclose_all()
