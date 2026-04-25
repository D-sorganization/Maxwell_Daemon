"""Integration tests for ToolRegistry + HookRunner.

When a registry has a hook runner attached, every ``invoke()`` passes
through pre_tool and post_tool gates. The gates see the tool name and
arguments; pre_tool returning ``blocked`` aborts the call;
post_tool returning ``errored`` turns the handler's success into an
agent-visible error.

Without a hook runner (the default), ``invoke()`` behaves byte-for-byte
as before — covered by the existing suite.
"""

from __future__ import annotations

from pathlib import Path

from maxwell_daemon.hooks import HookConfig, HookRunner, HookSpec
from maxwell_daemon.tools.mcp import (
    ToolParam,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class _Runner:
    """Canned subprocess runner for HookRunner."""

    def __init__(self, canned: dict[str, tuple[int, str]] | None = None) -> None:
        self._canned = canned or {}
        self.calls: list[str] = []

    async def __call__(
        self, command: str, *, cwd: str, env: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append(command)
        for prefix, resp in self._canned.items():
            if command.startswith(prefix):
                return resp
        return 0, ""


def _echo_spec() -> ToolSpec:
    return ToolSpec(
        name="echo",
        description="d",
        params=[ToolParam(name="text", type="string", description="")],
        handler=lambda text: f"echo:{text}",
    )


class TestPreToolBlocks:
    async def test_blocked_pre_tool_returns_error_result(self, tmp_path: Path) -> None:
        runner = _Runner({"block.sh": (1, "not allowed")})
        hook_runner = HookRunner(
            HookConfig(pre_tool=(HookSpec(match="echo", command="block.sh"),)),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())

        result = await reg.invoke("echo", {"text": "hi"})
        assert result.is_error is True
        assert "pre_tool" in result.content
        assert "not allowed" in result.content

    async def test_non_matching_pre_tool_allows_call(self, tmp_path: Path) -> None:
        runner = _Runner({"block.sh": (1, "not allowed")})
        hook_runner = HookRunner(
            HookConfig(pre_tool=(HookSpec(match="read_file", command="block.sh"),)),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())

        result = await reg.invoke("echo", {"text": "hi"})
        assert result == ToolResult(content="echo:hi", is_error=False)


class TestPostToolErrors:
    async def test_post_tool_failure_marks_result_error(self, tmp_path: Path) -> None:
        runner = _Runner({"lint": (1, "style error at line 5")})
        hook_runner = HookRunner(
            HookConfig(post_tool=(HookSpec(match="echo", command="lint"),)),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())

        result = await reg.invoke("echo", {"text": "hi"})
        # Handler succeeded, but post_tool surfaced an error.
        assert result.is_error is True
        assert "style error" in result.content
        assert "echo:hi" in result.content  # original output still visible

    async def test_post_tool_passing_leaves_result_intact(self, tmp_path: Path) -> None:
        runner = _Runner()  # default exit 0
        hook_runner = HookRunner(
            HookConfig(post_tool=(HookSpec(match="echo", command="lint"),)),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())

        result = await reg.invoke("echo", {"text": "hi"})
        assert result == ToolResult(content="echo:hi", is_error=False)


class TestPostToolSkippedOnHandlerError:
    async def test_handler_exception_skips_post_tool(self, tmp_path: Path) -> None:
        """If the handler itself raises, post_tool doesn't fire — there's nothing to audit."""

        def bad(text: str) -> str:
            raise ValueError("kaboom")

        runner = _Runner()
        hook_runner = HookRunner(
            HookConfig(post_tool=(HookSpec(match="bad", command="lint-never-fires"),)),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(
            ToolSpec(
                name="bad",
                description="",
                params=[ToolParam(name="text", type="string", description="")],
                handler=bad,
            )
        )

        result = await reg.invoke("bad", {"text": "x"})
        assert result.is_error is True
        assert "ValueError" in result.content
        assert runner.calls == []  # post_tool never ran


class TestNoHookRunner:
    async def test_registry_without_runner_behaves_like_before(
        self, tmp_path: Path
    ) -> None:
        reg = ToolRegistry()  # no hook runner
        reg.register(_echo_spec())
        result = await reg.invoke("echo", {"text": "hi"})
        assert result == ToolResult(content="echo:hi", is_error=False)


class TestPreToolRunsBeforePostTool:
    async def test_pre_tool_block_means_post_tool_does_not_run(
        self, tmp_path: Path
    ) -> None:
        runner = _Runner({"block.sh": (1, "blocked")})
        hook_runner = HookRunner(
            HookConfig(
                pre_tool=(HookSpec(match="echo", command="block.sh"),),
                post_tool=(HookSpec(match="echo", command="audit.sh"),),
            ),
            workspace=tmp_path,
            runner=runner,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())

        await reg.invoke("echo", {"text": "hi"})
        # Only the pre_tool hook command ran; audit.sh never fired.
        assert runner.calls == ["block.sh"]


class TestToolContextInEnv:
    async def test_tool_input_json_available_to_hook(self, tmp_path: Path) -> None:
        captured: dict[str, str] = {}

        async def recorder(
            command: str, *, cwd: str, env: dict[str, str], timeout: float
        ) -> tuple[int, str]:
            captured.update(env)
            return 0, ""

        hook_runner = HookRunner(
            HookConfig(pre_tool=(HookSpec(match="*", command="check"),)),
            workspace=tmp_path,
            runner=recorder,
        )
        reg = ToolRegistry(hook_runner=hook_runner)  # type: ignore[arg-type]
        reg.register(_echo_spec())
        await reg.invoke("echo", {"text": "audit-me"})
        assert captured["MAXWELL_TOOL_NAME"] == "echo"
        assert "audit-me" in captured["MAXWELL_TOOL_INPUT"]
