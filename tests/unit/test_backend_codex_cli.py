"""Codex CLI backend — shells out to `codex exec` for headless one-shot usage.

The OpenAI `codex` CLI is a Rust binary; we never call it for real from tests
and inject a fake runner to capture argv / stdin instead.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from maxwell_daemon.backends import Message, MessageRole
from maxwell_daemon.backends.base import BackendUnavailableError
from maxwell_daemon.backends.codex_cli import CodexCLIBackend, _default_runner


class _Runner:
    """Records argv + stdin and returns a pre-programmed response."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdins: list[bytes | None] = []
        self.cwds: list[str | None] = []
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
        self.stdins.append(stdin)
        self.cwds.append(cwd)
        return self._rc, self._stdout, self._stderr


class _FakeProcess:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdin_seen: bytes | None = None

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_seen = input
        return b"stdout", b"stderr"


# ---------------------------------------------------------------- construction


class TestConstruction:
    def test_defaults_binary_and_approval(self) -> None:
        backend = CodexCLIBackend(runner=_Runner())
        assert backend._binary == "codex"
        assert backend._approval == "suggest"
        assert backend._profile is None


class TestDefaultRunner:
    def test_default_runner_invokes_subprocess_with_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        process = _FakeProcess()
        captured: dict[str, Any] = {}

        async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProcess:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return process

        monkeypatch.setattr(
            "maxwell_daemon.backends.codex_cli.asyncio.create_subprocess_exec",
            fake_exec,
        )

        rc, stdout, stderr = asyncio.run(
            _default_runner("codex", "exec", cwd="repo", stdin=b"prompt")
        )

        assert rc == 0
        assert stdout == b"stdout"
        assert stderr == b"stderr"
        assert process.stdin_seen == b"prompt"
        assert captured["argv"] == ("codex", "exec")
        assert captured["kwargs"]["cwd"] == "repo"


# ---------------------------------------------------------------- complete()


class TestComplete:
    def test_argv_contains_exec_approval_and_model(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="hello")
        backend = CodexCLIBackend(runner=r)
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="ping")],
                model="gpt-5-codex",
            )
        )
        argv = list(r.calls[-1])
        assert argv[0] == "codex"
        assert argv[1] == "exec"
        assert "--approval" in argv
        assert argv[argv.index("--approval") + 1] == "suggest"
        assert "--model" in argv
        assert argv[argv.index("--model") + 1] == "gpt-5-codex"
        # Profile not set → flag absent.
        assert "--profile" not in argv

    def test_prompt_piped_via_stdin(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="ok")
        backend = CodexCLIBackend(runner=r)
        asyncio.run(
            backend.complete(
                [
                    Message(role=MessageRole.SYSTEM, content="be terse"),
                    Message(role=MessageRole.USER, content="say hi"),
                ],
                model="gpt-5-codex",
            )
        )
        piped = r.stdins[-1]
        assert piped is not None
        text = piped.decode()
        # System block before user block, joined by blank lines.
        assert "be terse" in text
        assert "say hi" in text
        assert text.index("be terse") < text.index("say hi")

    def test_full_auto_approval_forwarded(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="x")
        backend = CodexCLIBackend(runner=r, approval="full-auto")
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="go")],
                model="gpt-5-codex",
            )
        )
        argv = list(r.calls[-1])
        assert argv[argv.index("--approval") + 1] == "full-auto"

    def test_profile_flag_added_when_set(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="x")
        backend = CodexCLIBackend(runner=r, profile="team")
        asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="go")],
                model="gpt-5-codex",
            )
        )
        argv = list(r.calls[-1])
        assert "--profile" in argv
        assert argv[argv.index("--profile") + 1] == "team"

    def test_stdout_returned_as_content(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="codex says hi\n")
        backend = CodexCLIBackend(runner=r)
        resp = asyncio.run(
            backend.complete(
                [Message(role=MessageRole.USER, content="hi")],
                model="gpt-5-codex",
            )
        )
        assert resp.content == "codex says hi"
        assert resp.backend == "codex-cli"
        assert resp.model == "gpt-5-codex"

    def test_nonzero_rc_raises_with_truncated_stderr(self) -> None:
        r = _Runner()
        # 2000-char stderr should be truncated to 500 in the error message.
        r.respond(rc=2, stderr="q" * 2000)
        backend = CodexCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError) as ei:
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="gpt-5-codex",
                )
            )
        msg = str(ei.value)
        assert "rc=2" in msg
        # Only the first 500 q's survive the slice — the other 1500 are dropped.
        assert "q" * 500 in msg
        assert "q" * 501 not in msg

    def test_missing_binary_raises_unavailable(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("codex")

        backend = CodexCLIBackend(runner=runner)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="gpt-5-codex",
                )
            )

    def test_timeout_raises_unavailable(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise TimeoutError("too slow")

        backend = CodexCLIBackend(runner=runner)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(
                backend.complete(
                    [Message(role=MessageRole.USER, content="hi")],
                    model="gpt-5-codex",
                )
            )

    def test_empty_prompt_raises(self) -> None:
        r = _Runner()
        backend = CodexCLIBackend(runner=r)
        with pytest.raises(BackendUnavailableError):
            asyncio.run(backend.complete([], model="gpt-5-codex"))


# ---------------------------------------------------------------- stream()


class TestStream:
    def test_yields_once_with_complete_content(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="streamed once")
        backend = CodexCLIBackend(runner=r)

        async def collect() -> list[str]:
            chunks: list[str] = []
            async for c in backend.stream(
                [Message(role=MessageRole.USER, content="hi")],
                model="gpt-5-codex",
            ):
                chunks.append(c)
            return chunks

        chunks = asyncio.run(collect())
        assert chunks == ["streamed once"]


# ---------------------------------------------------------------- health_check


class TestHealthCheck:
    def test_returns_true_when_version_exits_zero(self) -> None:
        r = _Runner()
        r.respond(rc=0, stdout="codex 0.5.0\n")
        backend = CodexCLIBackend(runner=r)
        assert asyncio.run(backend.health_check()) is True
        # health_check runs `codex --version`.
        assert r.calls[-1] == ("codex", "--version")

    def test_returns_false_on_file_not_found(self) -> None:
        async def runner(*a: Any, **kw: Any) -> tuple[int, bytes, bytes]:
            raise FileNotFoundError("codex")

        backend = CodexCLIBackend(runner=runner)
        assert asyncio.run(backend.health_check()) is False


# ---------------------------------------------------------------- capabilities


class TestCapabilities:
    def test_non_local_with_tool_use_and_unknown_cost(self) -> None:
        backend = CodexCLIBackend(runner=_Runner())
        caps = backend.capabilities("gpt-5-codex")
        assert caps.is_local is False
        assert caps.supports_tool_use is True
        assert caps.supports_streaming is False
        assert caps.supports_vision is False
        assert caps.supports_system_prompt is True
        assert caps.max_context_tokens == 128_000
        # Codex CLI rides the user's own OpenAI subscription — cost is unknown
        # to the daemon, so the adapter returns None rather than a misleading 0.0.
        assert caps.cost_per_1k_input_tokens is None
        assert caps.cost_per_1k_output_tokens is None


# ---------------------------------------------------------------- registry


class TestRegistry:
    def test_registered_under_codex_cli(self) -> None:
        # Import side-effect registers the backend.
        import maxwell_daemon.backends.codex_cli  # noqa: F401
        from maxwell_daemon.backends.registry import registry

        assert "codex-cli" in registry.available()
