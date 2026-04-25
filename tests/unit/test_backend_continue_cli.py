"""Continue CLI backend — shells out to `cn ask <prompt>` for the Continue.dev CLI."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.continue_cli import ContinueCLIBackend, _default_runner


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout: bytes = b""
        self._stderr: bytes = b""
        self._rc: int = 0

    def respond(
        self, *, rc: int = 0, stdout: bytes | str = b"", stderr: bytes | str = b""
    ) -> None:
        self._rc = rc
        self._stdout = stdout.encode() if isinstance(stdout, str) else stdout
        self._stderr = stderr.encode() if isinstance(stderr, str) else stderr

    async def __call__(
        self, *argv: str, cwd: str | None = None, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        return self._rc, self._stdout, self._stderr


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"stdout", b"stderr"


class TestDefaultRunner:
    def test_default_runner_invokes_subprocess(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(
            "maxwell_daemon.backends.continue_cli.asyncio.create_subprocess_exec",
            fake_exec,
        )

        rc, stdout, stderr = asyncio.run(_default_runner("cn", "ask", "hi", cwd="repo"))

        assert rc == 0
        assert stdout == b"stdout"
        assert stderr == b"stderr"
        assert captured["argv"] == ("cn", "ask", "hi")
        assert captured["kwargs"]["cwd"] == "repo"


class TestDefaults:
    def test_default_binary_is_cn(self) -> None:
        backend = ContinueCLIBackend()
        assert backend._binary == "cn"


class TestComplete:
    def test_shells_to_cn_ask_with_prompt(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"the reply")
        backend = ContinueCLIBackend(runner=r)
        resp = asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="say hi")],
                model="auto",
            )
        )
        argv = r.calls[-1]
        assert argv[0] == "cn"
        assert argv[1] == "ask"
        assert argv[2] == "say hi"
        assert resp.content == "the reply"
        assert resp.backend == "continue-cli"

    def test_assistant_flag_is_passed_through(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"ok")
        backend = ContinueCLIBackend(runner=r, assistant="team-assistant")
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="auto",
            )
        )
        argv = r.calls[-1]
        assert "--assistant" in argv
        idx = argv.index("--assistant")
        assert argv[idx + 1] == "team-assistant"

    def test_assistant_absent_when_not_configured(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"ok")
        backend = ContinueCLIBackend(runner=r)
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="auto",
            )
        )
        argv = r.calls[-1]
        assert "--assistant" not in argv

    def test_system_message_prepended_to_user_message(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"ok")
        backend = ContinueCLIBackend(runner=r)
        asyncio.run(
            backend.complete(
                [
                    Message(role=MessageRole.SYSTEM, content="be terse"),
                    Message(role=MessageRole.SYSTEM, content="no emojis"),
                    Message(role=MessageRole.USER, content="say hi"),
                ],
                model="auto",
            )
        )
        argv = r.calls[-1]
        # The prompt should contain both system prefix pieces and the user turn.
        prompt = argv[2]
        assert "be terse" in prompt
        assert "no emojis" in prompt
        assert "say hi" in prompt
        # System parts come before user parts.
        assert prompt.index("be terse") < prompt.index("say hi")
        assert prompt.index("no emojis") < prompt.index("say hi")

    def test_stdout_returned_as_response_content(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="hello from continue\n")
        backend = ContinueCLIBackend(runner=r)
        resp = asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="auto",
            )
        )
        assert "hello from continue" in resp.content

    def test_error_exit_raises_with_truncated_stderr(self) -> None:
        r = _Runner()
        long_err = "x" * 1000
        r.respond(rc=2, stderr=long_err)
        backend = ContinueCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError) as exc_info:
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="auto",
                )
            )
        # Stderr should be truncated to ~500 chars.
        assert len(str(exc_info.value)) < 700
        assert "rc=2" in str(exc_info.value)

    def test_missing_binary_wrapped_as_unavailable(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("cn")

        backend = ContinueCLIBackend(runner=runner)
        with pytest.raises(BackendUnavailableError) as exc_info:
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="auto",
                )
            )
        assert "cn CLI unreachable" in str(exc_info.value)


class TestStream:
    def test_stream_yields_complete_content_once(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"streamed reply")
        backend = ContinueCLIBackend(runner=r)

        async def collect() -> list[str]:
            chunks: list[str] = []
            async for chunk in backend.stream(
                [Message(role=MessageRole.USER, content="hi")],
                model="auto",
            ):
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(collect())
        assert chunks == ["streamed reply"]


class TestHealthCheck:
    def test_healthy_when_rc_zero(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="cn 1.0.0\n")
        backend = ContinueCLIBackend(runner=r)
        assert asyncio.run(backend.health_check()) is True

    def test_unhealthy_when_binary_missing(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("cn")

        backend = ContinueCLIBackend(runner=runner)
        assert asyncio.run(backend.health_check()) is False


class TestCapabilities:
    def test_no_tool_use_and_unknown_cost(self) -> None:
        backend = ContinueCLIBackend(runner=_Runner())
        caps = backend.capabilities("auto")
        assert caps.supports_tool_use is False
        assert caps.supports_streaming is False
        assert caps.supports_system_prompt is True
        # Continue.dev CLI uses the user's own subscription — cost is unknown
        # to the daemon, represented as None rather than a misleading 0.0.
        assert caps.cost_per_1k_input_tokens is None
        assert caps.cost_per_1k_output_tokens is None
        assert caps.max_context_tokens == 128_000
        assert caps.is_local is False


class TestRegistry:
    def test_continue_cli_is_registered(self) -> None:
        from maxwell_daemon.backends.registry import registry

        assert "continue-cli" in registry.available()
