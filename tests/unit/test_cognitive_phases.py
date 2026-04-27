from __future__ import annotations

import typing

import pytest

from maxwell_daemon.core.cognitive_phases import CognitivePipeline, PhaseDecision
from maxwell_daemon.core.roles import Job, Role, RolePlayer


class DummyRolePlayer:
    def __init__(self, role: Role, content: str):
        self.role = role
        self._content = content

    async def execute(self, job: Job, tools=None):  # type: ignore[no-untyped-def]
        from maxwell_daemon.backends.base import BackendResponse, TokenUsage

        return BackendResponse(
            content=self._content,
            finish_reason="stop",
            usage=TokenUsage(),
            model="dummy",
            backend="dummy",
            raw={},
        )


@pytest.mark.asyncio
async def test_cognitive_pipeline_execution() -> None:
    strategist = DummyRolePlayer(Role("Strategist", "plan", False), content="Here is the plan.")
    implementer = DummyRolePlayer(Role("Implementer", "code", False), content="Here is the code.")
    validator = DummyRolePlayer(Role("Validator", "check", False), content="PASS")

    pipeline = CognitivePipeline(
        strategist=typing.cast(RolePlayer, strategist),
        implementer=typing.cast(RolePlayer, implementer),
        validator=typing.cast(RolePlayer, validator),
    )

    initial_job = Job(instructions="Fix the bug")
    result = await pipeline.run(initial_job)

    assert result.status == PhaseDecision.SUCCESS
    assert "Here is the code." in result.final_artifact
    assert len(result.history) == 3


@pytest.mark.asyncio
async def test_cognitive_pipeline_failure() -> None:
    strategist = DummyRolePlayer(Role("Strategist", "plan", False), content="Here is the plan.")
    implementer = DummyRolePlayer(Role("Implementer", "code", False), content="Here is bad code.")
    validator = DummyRolePlayer(
        Role("Validator", "check", False), content="FAIL: Did not follow TDD."
    )

    pipeline = CognitivePipeline(
        strategist=typing.cast(RolePlayer, strategist),
        implementer=typing.cast(RolePlayer, implementer),
        validator=typing.cast(RolePlayer, validator),
    )

    initial_job = Job(instructions="Fix the bug")
    result = await pipeline.run(initial_job)

    assert result.status == PhaseDecision.FAILURE
