from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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
from maxwell_daemon.core.cross_audit import CrossAuditService
from maxwell_daemon.core.roles import Role
from maxwell_daemon.core.router import BackendRouter


class _AuditBackend(ILLMBackend):
    name = "audit"
    response_text = "audit ok"

    def __init__(self, **_: Any) -> None:
        pass

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **_: Any,
    ) -> BackendResponse:
        system_prompt = messages[0].content
        return BackendResponse(
            content=f"{self.response_text}: {system_prompt}",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
            model=model,
            backend=self.name,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        **_: Any,
    ) -> AsyncIterator[str]:
        if False:
            yield ""

    async def health_check(self) -> bool:
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities()


class _SecondAuditBackend(_AuditBackend):
    response_text = "second ok"


class _FailingAuditBackend(_AuditBackend):
    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        **_: Any,
    ) -> BackendResponse:
        raise RuntimeError("backend exploded")


@pytest.fixture(autouse=True)
def _register_audit_backends() -> Iterator[None]:
    registry._factories["audit_a"] = _AuditBackend
    registry._factories["audit_b"] = _SecondAuditBackend
    registry._factories["audit_fail"] = _FailingAuditBackend
    yield
    registry._factories.pop("audit_a", None)
    registry._factories.pop("audit_b", None)
    registry._factories.pop("audit_fail", None)


def _config() -> MaxwellDaemonConfig:
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "one": {"type": "audit_a", "model": "model-one"},
                "two": {"type": "audit_b", "model": "model-two"},
            },
            "agent": {"default_backend": "one"},
        }
    )


@pytest.mark.asyncio
async def test_cross_audit_runs_role_backend_matrix() -> None:
    roles = [
        Role(name="Validator", system_prompt="validate this"),
        Role(name="Security", system_prompt="secure this"),
    ]
    service = CrossAuditService(BackendRouter(_config()))

    report = await service.run("review task", roles=roles, backend_names=["one", "two"])

    assert report.succeeded is True
    assert len(report.results) == 4
    assert {result.role_name for result in report.results} == {"Validator", "Security"}
    assert {result.backend_name for result in report.results} == {"one", "two"}
    assert report.total_usage.total_tokens == 40
    assert "4 succeeded, 0 failed" in report.summary
    assert "divergence" in report.summary


@pytest.mark.asyncio
async def test_cross_audit_preserves_successes_when_one_backend_fails() -> None:
    cfg = MaxwellDaemonConfig.model_validate(
        {
            "backends": {
                "ok": {"type": "audit_a", "model": "model-ok"},
                "bad": {"type": "audit_fail", "model": "model-bad"},
            },
            "agent": {"default_backend": "ok"},
        }
    )
    service = CrossAuditService(BackendRouter(cfg))

    report = await service.run("review task", backend_names=["ok", "bad"])

    assert report.succeeded is True
    assert len(report.results) == 2
    assert [result.backend_name for result in report.results if result.succeeded] == ["ok"]
    failed = [result for result in report.results if not result.succeeded]
    assert failed[0].backend_name == "bad"
    assert failed[0].error == "RuntimeError: backend exploded"
    assert "1 succeeded, 1 failed" in report.summary


@pytest.mark.asyncio
async def test_cross_audit_rejects_empty_target_set() -> None:
    service = CrossAuditService(BackendRouter(_config()))

    with pytest.raises(ValueError, match="at least one target"):
        await service.run("review task", targets=[])
