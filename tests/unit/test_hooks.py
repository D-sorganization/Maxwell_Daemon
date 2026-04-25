"""Tests for HookRunner — deterministic, LLM-bypass-proof code-standards gates.

Hooks fire at well-defined moments in the agent lifecycle:
  * ``pre_tool``  — before a tool invocation; non-zero exit aborts the call
  * ``post_tool`` — after a tool invocation; non-zero exit surfaces as a
                    ``ToolResult(is_error=True)`` so the agent can recover
  * ``pre_commit`` — gate PR open on linters / types / tests passing
  * ``on_prompt`` — inject extra context before the first turn
  * ``on_stop`` — housekeeping when a session ends

All tests inject a recorder runner so no real subprocesses spawn.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.hooks import (
    HookConfig,
    HookOutcome,
    HookRunner,
    HookSpec,
    HookViolationError,
    _default_runner,
    _matches,
    _parse_specs,
    _parse_strings,
    load_hook_config,
)

# ── Test doubles ─────────────────────────────────────────────────────────────


class _Runner:
    """Recorder with canned responses keyed by command prefix."""

    def __init__(self, canned: dict[str, tuple[int, str]] | None = None) -> None:
        self._canned = canned or {}
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, command: str, *, cwd: str, env: dict[str, str], timeout: float
    ) -> tuple[int, str]:
        self.calls.append({"command": command, "cwd": cwd, "env": env, "timeout": timeout})
        for prefix, resp in self._canned.items():
            if command.startswith(prefix):
                return resp
        return 0, ""


# ── Config shape ────────────────────────────────────────────────────────────


class TestHookConfigShape:
    def test_empty_config(self) -> None:
        cfg = HookConfig()
        assert cfg.pre_tool == ()
        assert cfg.post_tool == ()
        assert cfg.pre_commit == ()
        assert cfg.on_prompt == ()
        assert cfg.on_stop == ()

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        cfg = HookConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.pre_tool = (HookSpec(command="x"),)  # type: ignore[misc]


class TestLoadHookConfig:
    def test_loads_empty_when_no_file(self, tmp_path: Path) -> None:
        cfg = load_hook_config(tmp_path / "missing.yaml")
        assert cfg == HookConfig()

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            """
hooks:
  pre_tool:
    - match: run_bash
      command: "echo blocking"
  post_tool:
    - match: write_file
      command: "ruff format --check {{path}}"
  pre_commit:
    - "ruff check ."
  on_prompt:
    - "scripts/warmup.sh"
  on_stop:
    - "scripts/summary.sh"
"""
        )
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert len(cfg.pre_tool) == 1
        assert cfg.pre_tool[0].match == "run_bash"
        assert cfg.post_tool[0].command == "ruff format --check {{path}}"
        assert len(cfg.pre_commit) == 1
        assert len(cfg.on_prompt) == 1
        assert len(cfg.on_stop) == 1

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("not: [valid")
        with pytest.raises(Exception, match="hook"):
            load_hook_config(tmp_path / "h.yaml")

    def test_root_must_be_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(HookViolationError, match="must be a mapping"):
            load_hook_config(tmp_path / "h.yaml")

    def test_hooks_section_must_be_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks: [1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(HookViolationError, match="non-mapping `hooks:` section"):
            load_hook_config(tmp_path / "h.yaml")

    def test_pre_tool_specs_must_be_string_or_mapping(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_tool:\n    - 123\n", encoding="utf-8")
        with pytest.raises(HookViolationError, match="string or mapping"):
            load_hook_config(tmp_path / "h.yaml")

    def test_hook_spec_must_have_string_command(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            "hooks:\n  pre_tool:\n    - match: run_bash\n      command: 123\n",
            encoding="utf-8",
        )
        with pytest.raises(HookViolationError, match="missing `command:`"):
            load_hook_config(tmp_path / "h.yaml")

    def test_pre_commit_entries_must_be_strings(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_commit:\n    - true\n", encoding="utf-8")
        with pytest.raises(HookViolationError, match="hook entry must be a string"):
            load_hook_config(tmp_path / "h.yaml")


# ── pre_tool ─────────────────────────────────────────────────────────────────


class TestPreToolHook:
    async def test_matching_hook_blocks_tool_call(self, tmp_path: Path) -> None:
        runner = _Runner({"block.sh": (1, "blocked by policy")})
        cfg = HookConfig(pre_tool=(HookSpec(match="run_bash", command="block.sh"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_pre_tool("run_bash", {"command": "rm -rf /"})
        assert out.blocked is True
        assert "blocked by policy" in out.detail

    async def test_non_matching_hook_does_not_run(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(pre_tool=(HookSpec(match="write_file", command="echo no"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_pre_tool("run_bash", {})
        assert out.blocked is False
        assert runner.calls == []

    async def test_hook_env_includes_tool_context(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(pre_tool=(HookSpec(match="run_bash", command="echo"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_pre_tool("run_bash", {"command": "ls", "timeout_seconds": 5})
        assert runner.calls[0]["env"]["MAXWELL_TOOL_NAME"] == "run_bash"
        # Arguments are JSON-serialised so hook scripts can parse them.
        assert "command" in runner.calls[0]["env"]["MAXWELL_TOOL_INPUT"]

    async def test_wildcard_match_fires_for_all_tools(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(pre_tool=(HookSpec(match="*", command="audit.sh"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_pre_tool("read_file", {"path": "x"})
        await hr.run_pre_tool("write_file", {"path": "y", "content": "..."})
        assert len(runner.calls) == 2

    async def test_zero_exit_allows_call(self, tmp_path: Path) -> None:
        runner = _Runner({"verify": (0, "ok")})
        cfg = HookConfig(pre_tool=(HookSpec(match="run_bash", command="verify"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_pre_tool("run_bash", {})
        assert out.blocked is False


# ── post_tool ────────────────────────────────────────────────────────────────


class TestPostToolHook:
    async def test_non_zero_flags_error(self, tmp_path: Path) -> None:
        runner = _Runner({"ruff": (1, "file would be reformatted")})
        cfg = HookConfig(post_tool=(HookSpec(match="write_file", command="ruff check {{path}}"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_post_tool("write_file", {"path": "a.py"}, tool_output="wrote 10 bytes")
        assert out.errored is True
        assert "reformatted" in out.detail

    async def test_placeholder_substitution(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="write_file", command="check {{path}}"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_post_tool("write_file", {"path": "a.py"}, tool_output="")
        # Safe strings pass through shlex.quote unchanged.
        assert runner.calls[0]["command"] == "check a.py"

    async def test_placeholder_substitution_quotes_shell_metacharacters(
        self, tmp_path: Path
    ) -> None:
        """Values with shell metacharacters must be ``shlex.quote``d so the
        shell sees them as a single token rather than a command separator."""
        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="*", command="echo {{path}}"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_post_tool("write_file", {"path": "x; evil"}, tool_output="")
        cmd = runner.calls[0]["command"]
        # The unsafe value is single-quoted; no unescaped semicolon slips in.
        assert "'x; evil'" in cmd
        assert "echo x; evil" not in cmd

    async def test_placeholder_substitution_serialises_structured_values(
        self, tmp_path: Path
    ) -> None:
        """Dict / list values serialise to JSON and are a single shell token."""
        import json
        import shlex

        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="*", command="lint {{config}}"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_post_tool("write_file", {"config": {"foo": "bar"}}, tool_output="")
        cmd = runner.calls[0]["command"]
        # Expect a single shlex-quoted JSON string — parseable by a hook script.
        expected = shlex.quote(json.dumps({"foo": "bar"}))
        assert cmd == f"lint {expected}"

    async def test_env_carries_tool_output(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="*", command="audit"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_post_tool("run_bash", {}, tool_output="hello")
        assert runner.calls[0]["env"]["MAXWELL_TOOL_OUTPUT"] == "hello"

    async def test_non_matching_post_tool_does_not_run(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="write_file", command="never"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_post_tool("run_bash", {}, tool_output="")
        assert out.passed is True
        assert runner.calls == []


# ── pre_commit ───────────────────────────────────────────────────────────────


class TestPreCommitHook:
    async def test_all_passing_is_allowed(self, tmp_path: Path) -> None:
        runner = _Runner()  # defaults to 0
        cfg = HookConfig(pre_commit=("ruff check .", "mypy maxwell_daemon"))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_pre_commit()
        assert out.passed is True
        assert len(runner.calls) == 2

    async def test_first_failure_aborts_rest(self, tmp_path: Path) -> None:
        runner = _Runner({"mypy": (1, "16 errors")})
        cfg = HookConfig(pre_commit=("ruff check .", "mypy maxwell_daemon", "pytest"))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_pre_commit()
        assert out.passed is False
        assert "mypy" in out.failing_command
        # pytest never ran because mypy failed first.
        commands = [c["command"] for c in runner.calls]
        assert "pytest" not in commands

    async def test_raises_on_violation_helper(self, tmp_path: Path) -> None:
        runner = _Runner({"ruff": (1, "lint error")})
        cfg = HookConfig(pre_commit=("ruff check .",))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        with pytest.raises(HookViolationError, match="ruff"):
            await hr.raise_if_pre_commit_fails()


# ── on_prompt / on_stop ──────────────────────────────────────────────────────


class TestOnPromptAndOnStop:
    async def test_on_prompt_runs_in_order(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(on_prompt=("a.sh", "b.sh"))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        outputs = await hr.run_on_prompt()
        assert [c["command"] for c in runner.calls] == ["a.sh", "b.sh"]
        assert len(outputs) == 2

    async def test_on_stop_runs_once(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(on_stop=("cleanup.sh",))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_on_stop(exit_reason="end_turn")
        assert runner.calls[0]["env"]["MAXWELL_EXIT_REASON"] == "end_turn"


# ── Glob matching (issue #162) ───────────────────────────────────────────────


class TestGlobMatching:
    """``_matches`` must delegate to :mod:`fnmatch` so hook authors get real
    glob syntax (``*``, ``?``, ``[seq]``) — not just exact/wildcard as the
    pre-fix implementation supported. See issue #162."""

    def test_exact_match_still_works(self) -> None:
        assert _matches("Bash", "Bash") is True
        assert _matches("Bash", "Read") is False

    def test_bare_wildcard_matches_every_tool(self) -> None:
        assert _matches("*", "Bash") is True
        assert _matches("*", "Read") is True
        assert _matches("*", "") is True

    def test_star_suffix_glob(self) -> None:
        # Bash* matches Bash and BashOutput but not Read.
        assert _matches("Bash*", "Bash") is True
        assert _matches("Bash*", "BashOutput") is True
        assert _matches("Bash*", "Read") is False

    def test_character_class_glob(self) -> None:
        # [RW]* matches Read and Write but not Bash.
        assert _matches("[RW]*", "Read") is True
        assert _matches("[RW]*", "Write") is True
        assert _matches("[RW]*", "Bash") is False

    def test_question_mark_glob(self) -> None:
        # ?ead matches Read (single-char wildcard) but not Reading.
        assert _matches("?ead", "Read") is True
        assert _matches("?ead", "Reading") is False

    async def test_star_suffix_filters_runner_calls(self, tmp_path: Path) -> None:
        """End-to-end: a glob in HookSpec.match fires for matching tools only."""
        runner = _Runner()
        cfg = HookConfig(pre_tool=(HookSpec(match="Bash*", command="audit.sh"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        await hr.run_pre_tool("Bash", {})
        await hr.run_pre_tool("BashOutput", {})
        await hr.run_pre_tool("Read", {})
        # Only Bash and BashOutput should have fired the hook.
        assert len(runner.calls) == 2


# ── Outcome shape ───────────────────────────────────────────────────────────


class TestHookOutcome:
    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        o = HookOutcome(blocked=False, errored=False, passed=True, detail="ok", failing_command="")
        with pytest.raises(FrozenInstanceError):
            o.blocked = True  # type: ignore[misc]


class TestDefaultRunner:
    @pytest.mark.asyncio
    async def test_timeout_kills_process(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _Proc:
            def __init__(self) -> None:
                self.returncode = 0
                self.killed = False
                self.waited = False

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

        rc, output = await _default_runner("echo hi", cwd=str(tmp_path), env={}, timeout=0.01)
        assert rc == 124
        assert "timeout after" in output
        assert proc.killed is True
        assert proc.waited is True

    @pytest.mark.asyncio
    async def test_success_returns_decoded_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _Proc:
            returncode = None

            async def communicate(self) -> tuple[bytes, bytes]:
                return (b"ok\xff", b"")

        async def _fake_create(*_: object, **__: object) -> _Proc:
            return _Proc()

        monkeypatch.setattr("maxwell_daemon.hooks.asyncio.create_subprocess_shell", _fake_create)
        rc, output = await _default_runner("echo hi", cwd=str(tmp_path), env={}, timeout=1.0)
        assert rc == 0
        assert output.startswith("ok")


class TestParseHelpers:
    def test_parse_specs_supports_none_and_strings(self) -> None:
        assert _parse_specs(None) == []
        out = _parse_specs(["echo one"])
        assert len(out) == 1
        assert out[0].command == "echo one"

    def test_parse_strings_supports_none_and_type_errors(self) -> None:
        assert _parse_strings(None) == []
        with pytest.raises(HookViolationError, match="expected a list of commands"):
            _parse_strings("not-a-list")


class TestLoadHookConfigEdgeCases:
    def test_non_mapping_root_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("- list item\n")
        with pytest.raises(HookViolationError, match="mapping"):
            load_hook_config(tmp_path / "h.yaml")

    def test_non_mapping_hooks_section_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  - list_not_mapping\n")
        with pytest.raises(HookViolationError, match="non-mapping"):
            load_hook_config(tmp_path / "h.yaml")


class TestParseSpecsEdgeCases:
    def test_string_item_creates_spec(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_tool:\n    - 'echo hello'\n")
        cfg = load_hook_config(tmp_path / "h.yaml")
        assert cfg.pre_tool[0].command == "echo hello"
        assert cfg.pre_tool[0].match == "*"

    def test_non_list_pre_tool_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_tool: not_a_list\n")
        with pytest.raises(HookViolationError, match="list"):
            load_hook_config(tmp_path / "h.yaml")

    def test_non_dict_non_string_item_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_tool:\n    - 42\n")
        with pytest.raises(HookViolationError, match="mapping"):
            load_hook_config(tmp_path / "h.yaml")

    def test_missing_command_key_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_tool:\n    - match: foo\n")
        with pytest.raises(HookViolationError, match="command"):
            load_hook_config(tmp_path / "h.yaml")

    def test_non_string_match_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text(
            "hooks:\n  pre_tool:\n    - command: echo\n      match: 123\n"
        )
        with pytest.raises(HookViolationError, match="match"):
            load_hook_config(tmp_path / "h.yaml")


class TestParseStringsEdgeCases:
    def test_non_list_pre_commit_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_commit: not_a_list\n")
        with pytest.raises(HookViolationError, match="list"):
            load_hook_config(tmp_path / "h.yaml")

    def test_non_string_item_raises(self, tmp_path: Path) -> None:
        (tmp_path / "h.yaml").write_text("hooks:\n  pre_commit:\n    - 42\n")
        with pytest.raises(HookViolationError, match="string"):
            load_hook_config(tmp_path / "h.yaml")


class TestPostToolNonMatching:
    async def test_non_matching_post_tool_skipped(self, tmp_path: Path) -> None:
        runner = _Runner()
        cfg = HookConfig(post_tool=(HookSpec(match="write_file", command="check.sh"),))
        hr = HookRunner(cfg, workspace=tmp_path, runner=runner)
        out = await hr.run_post_tool("read_file", {}, tool_output="data")
        assert out.passed is True
        assert runner.calls == []
