import pytest

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    MessageRole,
    TokenUsage,
)
from maxwell_daemon.core.roles import Job, Role, RolePlayer


class DummyBackend:
    name = "dummy"

    def __init__(self, can_use_tools: bool = True):
        self._can_use_tools = can_use_tools

    def capabilities(self, model: str) -> BackendCapabilities:
        return BackendCapabilities(supports_tool_use=self._can_use_tools)

    async def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        return BackendResponse(
            content="dummy response",
            finish_reason="stop",
            usage=TokenUsage(),
            model="dummy-model",
            backend="dummy",
            raw={},
        )


@pytest.mark.asyncio
async def test_role_player_executes_job_orthogonally():  # type: ignore[no-untyped-def]
    architect_role = Role(
        name="Architect", requires_tool_use=False, system_prompt="You are an architect."
    )
    backend = DummyBackend(can_use_tools=False)

    player = RolePlayer(role=architect_role, backend=backend, model="dummy")  # type: ignore[arg-type]
    job = Job(instructions="Design a system.")

    result = await player.execute(job)
    assert result.content == "dummy response"
    assert len(player.last_messages) == 2
    assert player.last_messages[0].role == MessageRole.SYSTEM
    assert player.last_messages[0].content == "You are an architect."
    assert player.last_messages[1].role == MessageRole.USER
    assert player.last_messages[1].content == "Design a system."


def test_role_player_enforces_capabilities():  # type: ignore[no-untyped-def]
    coder_role = Role(name="Coder", requires_tool_use=True, system_prompt="You write code.")
    backend = DummyBackend(can_use_tools=False)

    with pytest.raises(
        ValueError,
        match="Backend dummy does not support required tool use for role Coder",
    ):
        RolePlayer(role=coder_role, backend=backend, model="dummy")  # type: ignore[arg-type]
