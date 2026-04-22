"""Unit tests for maxwell_daemon.tools.builtins — the sandboxed filesystem tools.

Every tool must:
 - refuse paths that escape the workspace (``..`` traversal, absolute paths outside root)
 - accept paths relative to the workspace
 - surface errors as ``ToolResult(is_error=True)`` rather than raising through the registry

The bash tool uses a bounded subprocess runner injected by the test so we never
touch the real shell here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from maxwell_daemon.core.action_policy import ActionPolicy, ApprovalMode
from maxwell_daemon.core.action_service import ActionService
from maxwell_daemon.core.action_store import ActionStore
from maxwell_daemon.tools.builtins import (
    SandboxViolationError,
    _build_run_bash_env,
    build_default_registry,
    make_edit_file,
    make_glob_files,
    make_grep_files,
    make_read_file,
    make_run_bash,
    make_write_file,
)
from maxwell_daemon.tools.mcp import ToolInvocationStore, ToolPolicy


# ── read_file ────────────────────────────────────────────────────────────────
class TestReadFile:
    def test_reads_file(self, tmp_path: Path) -> None:
        (tmp_path / "hello.txt").write_text("hi there")
        read = make_read_file(tmp_path)
        assert read(path="hello.txt") == "hi there"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        read = make_read_file(tmp_path)
        with pytest.raises(FileNotFoundError):
            read(path="missing.txt")

    def test_absolute_path_outside_root_rejected(self, tmp_path: Path) -> None:
        read = make_read_file(tmp_path)
        with pytest.raises(SandboxViolationError):
            read(path="/etc/passwd")

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        (tmp_path.parent / "secret.txt").write_text("nope")
        read = make_read_file(tmp_path)
        with pytest.raises(SandboxViolationError):
            read(path="../secret.txt")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Symlinks require admin privileges on Windows",
    )
    def test_symlink_escape_rejected(self, tmp_path: Path) -> None:
        target = tmp_path.parent / "outside.txt"
        target.write_text("nope")
        (tmp_path / "link.txt").symlink_to(target)
        read = make_read_file(tmp_path)
        with pytest.raises(SandboxViolationError):
            read(path="link.txt")


# ── write_file ───────────────────────────────────────────────────────────────
class TestWriteFile:
    def test_writes_new_file(self, tmp_path: Path) -> None:
        write = make_write_file(tmp_path)
        write(path="a/b/c.txt", content="hello")
        assert (tmp_path / "a/b/c.txt").read_text() == "hello"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        (tmp_path / "x").write_text("before")
        write = make_write_file(tmp_path)
        write(path="x", content="after")
        assert (tmp_path / "x").read_text() == "after"

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        write = make_write_file(tmp_path)
        with pytest.raises(SandboxViolationError):
            write(path="../escape.txt", content="x")

    def test_suggest_mode_records_action_without_writing(self, tmp_path: Path) -> None:
        service = ActionService(
            ActionStore(tmp_path / "actions.db"),
            policy=ActionPolicy(mode=ApprovalMode.SUGGEST, workspace_root=tmp_path),
        )
        write = make_write_file(tmp_path, action_service=service, task_id="task-1")

        result = write(path="pending.txt", content="hello")

        actions = service.list_for_task("task-1")
        assert "pending approval" in result
        assert len(actions) == 1
        assert actions[0].status.value == "proposed"
        assert not (tmp_path / "pending.txt").exists()

    def test_full_auto_records_and_applies_write(self, tmp_path: Path) -> None:
        service = ActionService(
            ActionStore(tmp_path / "actions.db"),
            policy=ActionPolicy(mode=ApprovalMode.FULL_AUTO, workspace_root=tmp_path),
        )
        write = make_write_file(tmp_path, action_service=service, task_id="task-1")

        write(path="applied.txt", content="hello")

        actions = service.list_for_task("task-1")
        assert (tmp_path / "applied.txt").read_text() == "hello"
        assert actions[0].status.value == "applied"


# ── edit_file ────────────────────────────────────────────────────────────────
class TestEditFile:
    def test_single_replacement(self, tmp_path: Path) -> None:
        (tmp_path / "f").write_text("alpha beta gamma")
        edit = make_edit_file(tmp_path)
        edit(path="f", old_string="beta", new_string="DELTA")
        assert (tmp_path / "f").read_text() == "alpha DELTA gamma"

    def test_ambiguous_match_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "f").write_text("x x")
        edit = make_edit_file(tmp_path)
        with pytest.raises(ValueError, match="appears 2 times"):
            edit(path="f", old_string="x", new_string="y")

    def test_old_string_not_found(self, tmp_path: Path) -> None:
        (tmp_path / "f").write_text("abc")
        edit = make_edit_file(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            edit(path="f", old_string="zzz", new_string="y")

    def test_traversal_rejected(self, tmp_path: Path) -> None:
        edit = make_edit_file(tmp_path)
        with pytest.raises(SandboxViolationError):
            edit(path="../x", old_string="a", new_string="b")


# ── run_bash ─────────────────────────────────────────────────────────────────
class TestRunBash:
    async def test_runs_and_returns_stdout(self, tmp_path: Path) -> None:
        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            assert cwd == str(tmp_path)
            assert cmd[-1] == "echo hi"
            # Must invoke ``bash -c`` (not ``-lc``) so login-profile files
            # (``~/.bash_profile`` etc.) do not run — they can mutate PATH and
            # inject arbitrary environment the operator did not opt into.
            assert cmd[:2] == ["bash", "-c"]
            return 0, b"hi\n", b""

        bash = make_run_bash(tmp_path, runner=runner)
        out = await bash(command="echo hi")
        assert "hi" in out

    async def test_non_zero_exit_reports_rc(self, tmp_path: Path) -> None:
        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            return 2, b"", b"oops"

        bash = make_run_bash(tmp_path, runner=runner)
        out = await bash(command="false")
        assert "exit 2" in out
        assert "oops" in out

    async def test_timeout_honoured(self, tmp_path: Path) -> None:
        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            assert timeout == 5
            return 0, b"", b""

        bash = make_run_bash(tmp_path, runner=runner, default_timeout=10)
        await bash(command="true", timeout_seconds=5)

    async def test_default_timeout_applied(self, tmp_path: Path) -> None:
        seen: list[float] = []

        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            seen.append(timeout)
            return 0, b"", b""

        bash = make_run_bash(tmp_path, runner=runner, default_timeout=42)
        await bash(command="true")
        assert seen == [42]

    async def test_output_truncated(self, tmp_path: Path) -> None:
        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            return 0, b"x" * 10_000, b""

        bash = make_run_bash(tmp_path, runner=runner, max_output_bytes=100)
        out = await bash(command="yes")
        assert "truncated" in out.lower()

    async def test_command_records_failed_action(self, tmp_path: Path) -> None:
        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            return 2, b"", b"oops"

        service = ActionService(
            ActionStore(tmp_path / "actions.db"),
            policy=ActionPolicy(mode=ApprovalMode.FULL_AUTO, workspace_root=tmp_path),
        )
        bash = make_run_bash(
            tmp_path,
            runner=runner,
            action_service=service,
            task_id="task-1",
        )

        out = await bash(command="false")

        actions = service.list_for_task("task-1")
        assert "exit 2" in out
        assert actions[0].status.value == "failed"

    async def test_default_runner_strips_unexpected_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unexpected env vars set in the parent process must not leak to the child."""
        monkeypatch.setenv("SECRET_KEY", "hunter2")
        bash = make_run_bash(tmp_path)
        out = await bash(command="env | grep SECRET_KEY || true")
        assert "SECRET_KEY" not in out
        assert "hunter2" not in out

    def test_default_runner_honours_allowlist_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAXWELL_ALLOW_ENV names env vars that *may* pass through.

        Tests the pure ``_build_run_bash_env()`` function directly rather than
        round-tripping through a real bash subprocess, making the test
        platform-agnostic (bash may not be in PATH on Windows CI).
        """
        monkeypatch.setenv("SECRET_KEY", "hunter2")
        monkeypatch.setenv("MAXWELL_ALLOW_ENV", "SECRET_KEY")
        env = _build_run_bash_env()
        assert os.environ.get("SECRET_KEY") == "hunter2"
        assert env.get("SECRET_KEY") == "hunter2", (
            f"expected SECRET_KEY in run_bash env; got keys: {sorted(env)}"
        )


# ── glob_files ───────────────────────────────────────────────────────────────
class TestGlobFiles:
    def test_finds_matches(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").touch()
        (tmp_path / "b.py").touch()
        (tmp_path / "c.txt").touch()
        glob = make_glob_files(tmp_path)
        result = glob(pattern="*.py")
        assert sorted(result.splitlines()) == ["a.py", "b.py"]

    def test_recursive(self, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub/nested.py").touch()
        glob = make_glob_files(tmp_path)
        result = glob(pattern="**/*.py")
        assert "sub/nested.py" in result

    def test_empty_is_explicit(self, tmp_path: Path) -> None:
        glob = make_glob_files(tmp_path)
        result = glob(pattern="*.nonesuch")
        assert "no matches" in result.lower()


# ── grep_files ───────────────────────────────────────────────────────────────
class TestGrepFiles:
    def test_finds_line(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("def foo():\n    pass\n")
        grep = make_grep_files(tmp_path)
        result = grep(pattern=r"def foo")
        assert "f.py" in result

    def test_scoped_glob(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("needle")
        (tmp_path / "a.md").write_text("needle")
        grep = make_grep_files(tmp_path)
        result = grep(pattern="needle", glob="*.py")
        assert "a.py" in result
        assert "a.md" not in result

    def test_no_match_is_explicit(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("unrelated")
        grep = make_grep_files(tmp_path)
        result = grep(pattern="zzz")
        assert "no match" in result.lower()


# ── registry assembly ───────────────────────────────────────────────────────
class TestBuildDefaultRegistry:
    def test_registers_all_six_tools(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        assert set(reg.names()) == {
            "read_file",
            "write_file",
            "edit_file",
            "run_bash",
            "glob_files",
            "grep_files",
        }

    def test_each_tool_has_description_and_params(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        for name in reg.names():
            spec = reg.get(name)
            assert spec.description, f"{name} missing description"
            # Every real tool takes at least one argument.
            assert spec.params, f"{name} declared zero params"

    def test_each_tool_has_governance_metadata(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        expected = {
            "read_file": ({"file_read", "repo_read"}, "read_only", False),
            "glob_files": ({"file_read", "repo_read"}, "read_only", False),
            "grep_files": ({"file_read", "repo_read"}, "read_only", False),
            "write_file": ({"file_write", "repo_write"}, "local_write", True),
            "edit_file": ({"file_read", "file_write", "repo_write"}, "local_write", True),
            "run_bash": ({"shell_read", "shell_write"}, "command_execution", True),
        }

        for name, (capabilities, risk_level, requires_approval) in expected.items():
            spec = reg.get(name)
            assert spec.capabilities == frozenset(capabilities)
            assert spec.risk_level == risk_level
            assert spec.requires_approval is requires_approval

    def test_emits_valid_anthropic_schemas(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        schemas = reg.to_anthropic()
        for s in schemas:
            assert set(s.keys()) == {"name", "description", "input_schema"}
            assert s["input_schema"]["type"] == "object"

    async def test_invoke_through_registry_end_to_end(self, tmp_path: Path) -> None:
        (tmp_path / "hi.txt").write_text("payload")
        reg = build_default_registry(tmp_path)
        result = await reg.invoke("read_file", {"path": "hi.txt"})
        assert result.content == "payload"
        assert result.is_error is False

    async def test_readonly_policy_denies_default_write_tool(self, tmp_path: Path) -> None:
        store = ToolInvocationStore()
        reg = build_default_registry(
            tmp_path,
            policy=ToolPolicy.readonly_default(),
            invocation_store=store,
        )

        result = await reg.invoke("write_file", {"path": "out.txt", "content": "nope"})

        assert result.is_error is True
        assert "denied by policy" in result.content
        assert not (tmp_path / "out.txt").exists()
        [record] = store.records
        assert record.status == "denied"
        assert "unallowed capabilities" in (record.error or "")

    async def test_readonly_policy_denies_default_shell_tool(self, tmp_path: Path) -> None:
        ran: list[bool] = []

        async def runner(cmd: list[str], cwd: str, timeout: float) -> tuple[int, bytes, bytes]:
            ran.append(True)
            return 0, b"ran", b""

        reg = build_default_registry(
            tmp_path,
            bash_runner=runner,
            policy=ToolPolicy.readonly_default(),
        )

        result = await reg.invoke("run_bash", {"command": "echo ran"})

        assert result.is_error is True
        assert "denied by policy" in result.content
        assert ran == []

    async def test_sandbox_violation_surfaces_as_tool_error(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        result = await reg.invoke("read_file", {"path": "/etc/passwd"})
        assert result.is_error is True
        assert "Sandbox" in result.content or "sandbox" in result.content
