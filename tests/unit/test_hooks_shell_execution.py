"""Tests for the split-runner shell execution model (issue #897).

Covers:
  * _needs_shell() — auto-detection of shell metacharacters
  * HookSpec.shell field — default False, settable to True
  * YAML loading with shell: true / shell: false
  * HookRunner dispatches to shell_runner when spec.shell=True
  * HookRunner dispatches to exec_runner when spec.shell=False
  * Lifecycle hook auto-detection: pipes → shell_runner; plain cmd → exec_runner
  * _exec_default_runner and _shell_default_runner are callable (smoke tests)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.hooks import (
    HookConfig,
    HookRunner,
    HookSpec,
    _exec_default_runner,
    _needs_shell,
    _shell_default_runner,
    load_hook_config,
)

# ── Test doubles ─────────────────────────────────────────────────────────────


class _TrackingRunner:
    """Records every invocation and returns canned (rc, output)."""

    def __init__(self, name: str, rc: int = 0, output: str = "") -> None:
        self.name = name
        self._rc = rc
        self._output = output
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, command: str, *, cwd: str, env: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        return self._rc, self._output


# ── _needs_shell ─────────────────────────────────────────────────────────────


class TestNeedsShell:
    """_needs_shell(cmd) returns True iff cmd contains shell metacharacters."""

    def test_pipe_requires_shell(self) -> None:
        assert _needs_shell("cat file | grep foo") is True

    def test_background_ampersand_requires_shell(self) -> None:
        assert _needs_shell("long_job &") is True

    def test_semicolon_requires_shell(self) -> None:
        assert _needs_shell("echo a; echo b") is True

    def test_redirect_gt_requires_shell(self) -> None:
        assert _needs_shell("echo hi > out.txt") is True

    def test_redirect_lt_requires_shell(self) -> None:
        assert _needs_shell("sort < input.txt") is True

    def test_backtick_requires_shell(self) -> None:
        assert _needs_shell("echo `date`") is True

    def test_dollar_paren_requires_shell(self) -> None:
        assert _needs_shell("echo $(date)") is True

    def test_dollar_brace_requires_shell(self) -> None:
        assert _needs_shell("echo ${HOME}") is True

    def test_simple_command_does_not_require_shell(self) -> None:
        assert _needs_shell("ruff check .") is False

    def test_command_with_flag_does_not_require_shell(self) -> None:
        assert _needs_shell("mypy maxwell_daemon --strict") is False

    def test_empty_string_does_not_require_shell(self) -> None:
        assert _needs_shell("") is False

    def test_path_with_slash_does_not_require_shell(self) -> None:
        assert _needs_shell("scripts/warmup.sh") is False

    def test_double_ampersand_requires_shell(self) -> None:
        # && contains & which is a shell metacharacter
        assert _needs_shell("make all && make test") is True

    def test_glob_pattern_requires_shell(self) -> None:
        assert _needs_shell("pytest tests/unit/test_*.py") is True

    def test_tilde_expansion_requires_shell(self) -> None:
        assert _needs_shell("cat ~/.config/maxwell-daemon/config.toml") is True


# ── HookSpec.shell field ──────────────────────────────────────────────────────


class TestHookSpecShellField:
    def test_shell_defaults_to_false(self) -> None:
        spec = HookSpec(command="echo hi")
        assert spec.shell is False

    def test_shell_can_be_set_to_true(self) -> None:
        spec = HookSpec(command="cat f | wc -l", shell=True)
        assert spec.shell is True

    def test_shell_false_explicit(self) -> None:
        spec = HookSpec(command="ruff check .", shell=False)
        assert spec.shell is False

    def test_hookspec_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        spec = HookSpec(command="echo hi")
        with pytest.raises(FrozenInstanceError):
            spec.shell = True  # type: ignore[misc]


# ── YAML loading with shell: ──────────────────────────────────────────────────


class TestLoadHookConfigShell:
    def test_shell_true_parsed_from_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - command: "cat log | grep ERROR"
      match: "*"
      shell: true
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.pre_tool[0].shell is True

    def test_shell_false_parsed_from_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - command: "ruff check ."
      match: "*"
      shell: false
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.pre_tool[0].shell is False

    def test_shell_defaults_to_false_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - command: "ruff check ."
      match: "*"
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.pre_tool[0].shell is False

    def test_string_shorthand_spec_defaults_shell_false(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - "ruff check ."
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.pre_tool[0].shell is False

    def test_shell_non_bool_raises(self, tmp_path: Path) -> None:
        from maxwell_daemon.hooks import HookViolationError

        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - command: "echo hi"
      shell: "yes"
"""
        )
        with pytest.raises(HookViolationError, match="shell"):
            load_hook_config(tmp_path / "h.yaml")

    def test_post_tool_shell_true_parsed(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  post_tool:
    - command: "cat {{path}} | wc -l"
      match: "write_file"
      shell: true
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.post_tool[0].shell is True


# ── HookRunner dispatch for HookSpec hooks ───────────────────────────────────


class TestHookRunnerShellDispatch:
    """HookRunner uses shell_runner when spec.shell=True, exec_runner otherwise."""

    async def test_pre_tool_shell_true_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(
            pre_tool=(HookSpec(command="cat log | grep ERROR", match="*", shell=True),)
        )
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_tool("run_bash", {})
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_pre_tool_shell_false_uses_exec_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(pre_tool=(HookSpec(command="ruff check .", match="*", shell=False),))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_tool("run_bash", {})
        assert len(exec_runner.calls) == 1
        assert len(shell_runner.calls) == 0

    async def test_post_tool_shell_true_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(
            post_tool=(HookSpec(command="cat {{path}} | wc -l", match="write_file", shell=True),)
        )
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_post_tool("write_file", {"path": "a.py"}, tool_output="")
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_post_tool_shell_false_uses_exec_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(
            post_tool=(HookSpec(command="ruff format --check {{path}}", match="write_file"),)
        )
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_post_tool("write_file", {"path": "a.py"}, tool_output="")
        assert len(exec_runner.calls) == 1
        assert len(shell_runner.calls) == 0

    async def test_backward_compat_runner_kwarg_used_as_exec_runner(self, tmp_path: Path) -> None:
        """runner= kwarg maps to exec_runner for backward compatibility."""
        compat_runner = _TrackingRunner("compat")
        cfg = HookConfig(pre_tool=(HookSpec(command="ruff check .", match="*", shell=False),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=compat_runner)
        await hr.run_pre_tool("run_bash", {})
        assert len(compat_runner.calls) == 1


# ── Lifecycle hook auto-detection ────────────────────────────────────────────


class TestLifecycleHookAutoDetect:
    """Lifecycle hooks (pre_commit, on_prompt, on_stop) auto-detect via _needs_shell."""

    async def test_pre_commit_with_pipe_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(pre_commit=("cat log | grep FAIL",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_commit()
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_pre_commit_simple_uses_exec_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(pre_commit=("ruff check .",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_commit()
        assert len(exec_runner.calls) == 1
        assert len(shell_runner.calls) == 0

    async def test_pre_commit_with_glob_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(pre_commit=("pytest tests/unit/test_*.py",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_commit()
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_on_prompt_with_pipe_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(on_prompt=("cat context | head -20",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_on_prompt()
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_on_prompt_simple_uses_exec_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(on_prompt=("scripts/warmup.sh",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_on_prompt()
        assert len(exec_runner.calls) == 1
        assert len(shell_runner.calls) == 0

    async def test_on_stop_with_semicolon_uses_shell_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(on_stop=("echo done; scripts/summary.sh",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_on_stop(exit_reason="end_turn")
        assert len(shell_runner.calls) == 1
        assert len(exec_runner.calls) == 0

    async def test_on_stop_simple_uses_exec_runner(self, tmp_path: Path) -> None:
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(on_stop=("scripts/summary.sh",))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_on_stop(exit_reason="end_turn")
        assert len(exec_runner.calls) == 1
        assert len(shell_runner.calls) == 0

    async def test_mixed_lifecycle_commands_route_independently(self, tmp_path: Path) -> None:
        """Multiple pre_commit commands each route based on their own metachar content."""
        exec_runner = _TrackingRunner("exec")
        shell_runner = _TrackingRunner("shell")
        cfg = HookConfig(pre_commit=("ruff check .", "cat log | grep ERROR", "mypy ."))
        hr = HookRunner(
            cfg,
            workspace=tmp_path,
            exec_runner=exec_runner,
            shell_runner=shell_runner,
        )
        await hr.run_pre_commit()
        assert len(exec_runner.calls) == 2  # ruff and mypy
        assert len(shell_runner.calls) == 1  # cat | grep


# ── Smoke tests for default runners ─────────────────────────────────────────


class TestDefaultRunnersCallable:
    def test_exec_default_runner_is_callable(self) -> None:
        import inspect

        assert callable(_exec_default_runner)
        assert inspect.iscoroutinefunction(_exec_default_runner)

    def test_shell_default_runner_is_callable(self) -> None:
        import inspect

        assert callable(_shell_default_runner)
        assert inspect.iscoroutinefunction(_shell_default_runner)

    @pytest.mark.asyncio
    async def test_exec_default_runner_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_exec_default_runner handles asyncio.TimeoutError like _default_runner."""

        class _Proc:
            returncode = 0
            killed = False
            waited = False

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        proc = _Proc()

        async def _fake_create(*_: object, **__: object) -> _Proc:
            return proc

        async def _fake_wait_for(awaitable: object, **__: object) -> object:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError

        monkeypatch.setattr("maxwell_daemon.hooks.asyncio.create_subprocess_exec", _fake_create)
        monkeypatch.setattr("maxwell_daemon.hooks.asyncio.wait_for", _fake_wait_for)

        rc, output = await _exec_default_runner("echo hi", cwd=str(tmp_path), env={}, timeout=0.01)
        assert rc == 124
        assert "timeout after" in output
        assert proc.killed is True

    @pytest.mark.asyncio
    async def test_shell_default_runner_timeout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """_shell_default_runner handles asyncio.TimeoutError."""

        class _Proc:
            returncode = 0
            killed = False
            waited = False

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"", b"")

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> None:
                self.waited = True

        proc = _Proc()

        async def _fake_create(*_: object, **__: object) -> _Proc:
            return proc

        async def _fake_wait_for(awaitable: object, **__: object) -> object:
            close = getattr(awaitable, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError

        monkeypatch.setattr("maxwell_daemon.hooks.asyncio.create_subprocess_shell", _fake_create)
        monkeypatch.setattr("maxwell_daemon.hooks.asyncio.wait_for", _fake_wait_for)

        rc, output = await _shell_default_runner("echo hi", cwd=str(tmp_path), env={}, timeout=0.01)
        assert rc == 124
        assert "timeout after" in output
        assert proc.killed is True
