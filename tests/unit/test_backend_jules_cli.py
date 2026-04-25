import json

import pytest

from maxwell_daemon.backends.base import BackendUnavailableError, Message, MessageRole
from maxwell_daemon.backends.jules_cli import JulesCLIBackend


@pytest.mark.asyncio
async def test_jules_cli_complete_success():  # type: ignore[no-untyped-def]
    async def mock_runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        return (
            0,
            json.dumps(
                {
                    "result": "jules says hello",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            ).encode(),
            b"",
        )

    backend = JulesCLIBackend(runner=mock_runner)
    messages = [Message(role=MessageRole.USER, content="Hello")]

    response = await backend.complete(messages, model="jules-default")
    assert response.content == "jules says hello"
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 15
    assert response.backend == "jules-cli"


@pytest.mark.asyncio
async def test_jules_cli_unavailable():  # type: ignore[no-untyped-def]
    async def mock_runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("jules not found")

    backend = JulesCLIBackend(runner=mock_runner)
    messages = [Message(role=MessageRole.USER, content="Hello")]

    with pytest.raises(BackendUnavailableError, match="jules CLI unreachable"):
        await backend.complete(messages, model="jules-default")


@pytest.mark.asyncio
async def test_jules_cli_formatting():  # type: ignore[no-untyped-def]
    async def mock_runner(*args, **kwargs):  # type: ignore[no-untyped-def]
        assert "System instruction" in args[2]
        assert "User message" in args[2]
        return 0, json.dumps({"result": "ok"}).encode(), b""

    backend = JulesCLIBackend(runner=mock_runner)
    messages = [
        Message(role=MessageRole.SYSTEM, content="System instruction"),
        Message(role=MessageRole.USER, content="User message"),
    ]
    await backend.complete(messages, model="jules-default")
