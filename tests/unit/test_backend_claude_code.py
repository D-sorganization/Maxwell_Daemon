"""Claude Code CLI backend — shells out to `claude -p` and parses JSON output."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.claude_code import ClaudeCodeCLIBackend, _default_runner


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
            "maxwell_daemon.backends.claude_code.asyncio.create_subprocess_exec",
            fake_exec,
        )

        rc, stdout, stderr = asyncio.run(
            _default_runner("claude", "-p", "hi", cwd="repo")
        )

        assert rc == 0
        assert stdout == b"stdout"
        assert stderr == b"stderr"
        assert captured["argv"] == ("claude", "-p", "hi")
        assert captured["kwargs"]["cwd"] == "repo"


class TestAuth:
    def test_missing_binary_reports_unavailable(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("claude")

        backend = ClaudeCodeCLIBackend(runner=runner)
        assert asyncio.run(backend.health_check()) is False

    def test_healthy_when_cli_reports_version(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="claude 0.10.0\n")
        backend = ClaudeCodeCLIBackend(runner=r)
        assert asyncio.run(backend.health_check()) is True


class TestComplete:
    def test_passes_prompt_and_parses_json(self) -> None:
        r = _Runner()
        r.respond(
            rc=0,
            stdout=json.dumps(
                {
                    "result": "hello world",
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 3,
                    },
                    "model": "claude-sonnet-4-6",
                }
            ),
        )
        backend = ClaudeCodeCLIBackend(runner=r)
        resp = asyncio.run(
            backend.complete(
                [
                    Message(role=MessageRole.SYSTEM, content="be terse"),
                    Message(role=MessageRole.USER, content="say hi"),
                ],
                model="claude-sonnet-4-6",
            )
        )
        assert resp.content == "hello world"
        assert resp.usage.prompt_tokens == 12
        assert resp.usage.completion_tokens == 3
        assert resp.backend == "claude-code-cli"
        # The CLI must have been given --model and --output-format json.
        argv = r.calls[-1]
        assert "--model" in argv
        assert "json" in argv or "--output-format" in argv

    def test_error_exit_raises(self) -> None:
        r = _Runner()
        r.respond(rc=1, stderr=b"something broke")
        backend = ClaudeCodeCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="claude-sonnet-4-6",
                )
            )

    def test_missing_binary_raises_unavailable(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("claude")

        backend = ClaudeCodeCLIBackend(runner=runner)
        with pytest.raises(BackendUnavailableError, match="claude CLI unreachable"):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="claude-sonnet-4-6",
                )
            )

    def test_malformed_json_raises(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=b"not json")
        backend = ClaudeCodeCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="claude-sonnet-4-6",
                )
            )


class TestStream:
    def test_stream_yields_complete_content_once(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout=json.dumps({"result": "streamed"}))
        backend = ClaudeCodeCLIBackend(runner=r)

        async def collect() -> list[str]:
            return [
                chunk
                async for chunk in backend.stream(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="claude-sonnet-4-6",
                )
            ]

        assert asyncio.run(collect()) == ["streamed"]


class TestCapabilities:
    def test_marked_as_non_local(self) -> None:
        r = _Runner()
        backend = ClaudeCodeCLIBackend(runner=r)
        caps = backend.capabilities("claude-sonnet-4-6")
        assert caps.is_local is False
        assert caps.supports_tool_use is True


class TestRegistry:
    def test_registered(self) -> None:
        from maxwell_daemon.backends.registry import registry

        assert "claude-code-cli" in registry.available()
