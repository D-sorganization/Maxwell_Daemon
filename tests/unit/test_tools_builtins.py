"""Unit tests for maxwell_daemon.tools.builtins — the sandboxed filesystem tools.

Every tool must:
 - refuse paths that escape the workspace (``..`` traversal, absolute paths outside root)
 - accept paths relative to the workspace
 - surface errors as ``ToolResult(is_error=True)`` rather than raising through the registry

The bash tool uses a bounded subprocess runner injected by the test so we never
touch the real shell here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.tools.builtins import (
    SandboxViolationError,
    build_default_registry,
    make_edit_file,
    make_glob_files,
    make_grep_files,
    make_read_file,
    make_run_bash,
    make_write_file,
)


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

    async def test_default_runner_strips_unexpected_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unexpected env vars set in the parent process must not leak to the child."""
        monkeypatch.setenv("SECRET_KEY", "hunter2")
        bash = make_run_bash(tmp_path)
        out = await bash(command="env | grep SECRET_KEY || true")
        assert "SECRET_KEY" not in out
        assert "hunter2" not in out

    async def test_default_runner_honours_allowlist_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MAXWELL_ALLOW_ENV names env vars that *may* pass through."""
        monkeypatch.setenv("SECRET_KEY", "hunter2")
        monkeypatch.setenv("MAXWELL_ALLOW_ENV", "SECRET_KEY")
        bash = make_run_bash(tmp_path)
        out = await bash(command="echo value=$SECRET_KEY")
        assert "value=hunter2" in out


def test_sandbox_cmd_linux_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    from maxwell_daemon.tools.builtins import get_sandboxed_bash_cmd

    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/bwrap" if cmd == "bwrap" else None)
    res = get_sandboxed_bash_cmd(["bash", "-c", "echo hi"], "/tmp/workspace")
    assert res[0] == "bwrap"
    assert "--bind" in res
    assert "/tmp/workspace" in res
    assert "--unshare-all" in res


def test_sandbox_cmd_mac_sandbox_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    from maxwell_daemon.tools.builtins import get_sandboxed_bash_cmd

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/usr/bin/sandbox-exec" if cmd == "sandbox-exec" else None
    )
    res = get_sandboxed_bash_cmd(["bash", "-c", "echo hi"], "/tmp/workspace")
    assert res[:3] == ["sandbox-exec", "-f", "/usr/share/sandbox/pure_computation.sb"]


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

    async def test_sandbox_violation_surfaces_as_tool_error(self, tmp_path: Path) -> None:
        reg = build_default_registry(tmp_path)
        result = await reg.invoke("read_file", {"path": "/etc/passwd"})
        assert result.is_error is True
        assert "Sandbox" in result.content or "sandbox" in result.content
