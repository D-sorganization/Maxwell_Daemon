"""Claude Code CLI backend — shells out to `claude -p` and parses JSON output."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.claude_code import (
    ClaudeCodeCLIBackend,
    TemporaryPromptFiles,
    _default_runner,
)


@pytest.fixture(autouse=True)
def mock_mcp_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    @contextlib.asynccontextmanager
    async def fake_start_server(config_path: Any = None) -> AsyncIterator[tuple[Path, str]]:
        yield tmp_path / "mcp-config.json", "http://127.0.0.1:12345/mcp"

    monkeypatch.setattr(
        "maxwell_daemon.mcp.server.start_mcp_http_server",
        fake_start_server,
    )


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._stdout: bytes = b""
        self._stderr: bytes = b""
        self._rc: int = 0

    def respond(self, *, rc: int = 0, stdout: bytes | str = b"", stderr: bytes | str = b"") -> None:
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
    def test_default_runner_invokes_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProcess()

        monkeypatch.setattr(
            "maxwell_daemon.backends.claude_code.asyncio.create_subprocess_exec",
            fake_exec,
        )

        rc, stdout, stderr = asyncio.run(_default_runner("claude", "-p", "hi", cwd="repo"))

        assert rc == 0
        assert stdout == b"stdout"
        assert stderr == b"stderr"
        if os.name == "nt":
            assert captured["argv"] == ("cmd", "/c", "claude", "-p", "hi")
        else:
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


class TestTemporaryPromptFiles:
    @pytest.mark.asyncio
    async def test_writes_prompts_and_sets_permissions(self) -> None:
        prompts = ["system prompt 1", "system prompt 2"]
        async with TemporaryPromptFiles(prompts) as (path1, path2):
            assert path1 is not None
            assert path2 is not None
            assert os.path.exists(path1)
            assert os.path.exists(path2)

            with open(path1, encoding="utf-8") as f:
                assert f.read() == "system prompt 1"
            with open(path2, encoding="utf-8") as f:
                assert f.read() == "system prompt 2"

            if os.name != "nt":
                assert stat.S_IMODE(os.stat(path1).st_mode) == 0o600
                assert stat.S_IMODE(os.stat(path2).st_mode) == 0o600

        assert not os.path.exists(path1)
        assert not os.path.exists(path2)

    @pytest.mark.asyncio
    async def test_handles_empty_prompts(self) -> None:
        async with TemporaryPromptFiles([]) as (path1, path2):
            assert path1 is None
            assert path2 is None


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

        argv = r.calls[-1]
        assert "--model" in argv
        assert "claude-sonnet-4-6" in argv
        assert "--output-format" in argv
        assert "stream-json" in argv

    def test_read_only_mode_arguments(self) -> None:
        r = _Runner()
        r.respond(
            rc=0,
            stdout=json.dumps(
                {
                    "result": "read only response",
                    "usage": {"input_tokens": 5, "output_tokens": 5},
                }
            ),
        )
        backend = ClaudeCodeCLIBackend(runner=r)
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="claude-sonnet-4-6",
                mode="read-only",
            )
        )
        argv = r.calls[-1]
        assert "--permission-mode" in argv
        assert "dontAsk" in argv
        assert "--disallowed-tools" in argv
        assert "Edit,Write,NotebookEdit" in argv
        assert "--allowedTools" in argv
        assert "Read" in argv
        assert "Bash(git status)" in argv

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

    def test_parses_stream_events_successfully(self) -> None:
        r = _Runner()
        events = [
            {
                "type": "assistant",
                "message": {"id": "msg1", "content": [{"type": "text", "text": "hello "}]},
            },
            {
                "type": "assistant",
                "message": {"id": "msg1", "content": [{"type": "text", "text": "hello world"}]},
            },
            {
                "type": "result",
                "duration_ms": 1500,
                "total_cost_usd": 0.0025,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        ]
        stdout_bytes = b"\n".join(json.dumps(e).encode() for e in events)
        r.respond(rc=0, stdout=stdout_bytes)

        backend = ClaudeCodeCLIBackend(runner=r)
        resp = asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="claude-sonnet-4-6",
            )
        )
        assert resp.content == "hello world"
        assert resp.usage.prompt_tokens == 100
        assert resp.usage.completion_tokens == 50
        assert resp.raw["duration_ms"] == 1500
        assert resp.raw["total_cost_usd"] == 0.0025

    def test_error_event_raises(self) -> None:
        r = _Runner()
        events = [{"type": "error", "error": "something bad happened"}]
        stdout_bytes = b"\n".join(json.dumps(e).encode() for e in events)
        r.respond(rc=0, stdout=stdout_bytes)

        backend = ClaudeCodeCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError, match="something bad happened"):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="claude-sonnet-4-6",
                )
            )

    def test_result_error_raises(self) -> None:
        r = _Runner()
        events = [
            {
                "type": "result",
                "is_error": True,
                "errors": ["compilation failed", "syntax error"],
            }
        ]
        stdout_bytes = b"\n".join(json.dumps(e).encode() for e in events)
        r.respond(rc=0, stdout=stdout_bytes)

        backend = ClaudeCodeCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError, match="compilation failed; syntax error"):
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

    def test_stream_yields_deltas(self) -> None:
        r = _Runner()
        events = [
            {
                "type": "assistant",
                "message": {"id": "msg1", "content": [{"type": "text", "text": "hello "}]},
            },
            {
                "type": "assistant",
                "message": {"id": "msg1", "content": [{"type": "text", "text": "hello world"}]},
            },
            {
                "type": "result",
                "duration_ms": 1500,
                "total_cost_usd": 0.0025,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            },
        ]
        stdout_bytes = b"\n".join(json.dumps(e).encode() for e in events)
        r.respond(rc=0, stdout=stdout_bytes)

        backend = ClaudeCodeCLIBackend(runner=r)

        async def collect() -> list[str]:
            chunks = []
            async for chunk in backend.stream(
                [Message(role=MessageRole.USER, content="hi")],
                model="claude-sonnet-4-6",
            ):
                chunks.append(chunk)
            return chunks

        assert asyncio.run(collect()) == ["hello ", "world"]


class TestCapabilities:
    def test_marked_as_non_local(self) -> None:
        r = _Runner()
        backend = ClaudeCodeCLIBackend(runner=r)
        caps = backend.capabilities("claude-sonnet-4-6")
        assert caps.is_local is False
        assert caps.supports_tool_use is True
        assert caps.supports_streaming is True


class TestRegistry:
    def test_registered(self) -> None:
        from maxwell_daemon.backends.registry import registry

        assert "claude-code-cli" in registry.available()
