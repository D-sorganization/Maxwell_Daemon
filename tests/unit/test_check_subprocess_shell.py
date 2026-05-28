"""Unit tests for .github/scripts/check_subprocess_shell.py (Phase 1.3, epic #896).

The CI guard ensures ``asyncio.create_subprocess_shell`` is only ever called
from ``_shell_default_runner`` in ``maxwell_daemon/hooks.py``.  Any other
usage would bypass the sandboxed exec path and introduce shell-injection risk.

Design (DbC):
  * ``_actual_uses(source) -> list[int]`` — pure function: returns 1-based
    line numbers where the symbol appears as a NAME token (not in a comment
    or string literal).  Postcondition: every returned line number is a real
    call site.
  * ``check(repo_root) -> list[str]`` — returns an empty list iff no
    violations exist.  Postcondition: empty list ⟹ CI passes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

# The script lives at .github/scripts/, not inside the maxwell_daemon package.
# Load it dynamically so we can test its functions without running main().
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "check_subprocess_shell.py"
)


def _load_module() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_subprocess_shell", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_subprocess_shell"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard() -> object:
    return _load_module()


# ── _actual_uses ─────────────────────────────────────────────────────────────


class TestActualUses:
    """``_actual_uses(source)`` returns only real call-site line numbers."""

    def test_finds_name_token(self, guard: object) -> None:
        source = "proc = await asyncio.create_subprocess_shell(cmd)\n"
        result = guard._actual_uses(source)  # type: ignore[attr-defined]
        assert result == [1]

    def test_ignores_comment(self, guard: object) -> None:
        source = "# asyncio.create_subprocess_shell is not used here\n"
        result = guard._actual_uses(source)  # type: ignore[attr-defined]
        assert result == []

    def test_ignores_string_literal(self, guard: object) -> None:
        source = 'doc = "Use asyncio.create_subprocess_shell for shells"\n'
        result = guard._actual_uses(source)  # type: ignore[attr-defined]
        assert result == []

    def test_multiple_occurrences(self, guard: object) -> None:
        source = dedent("""\
            proc = await asyncio.create_subprocess_shell(cmd1)
            # comment
            proc2 = await asyncio.create_subprocess_shell(cmd2)
        """)
        result = guard._actual_uses(source)  # type: ignore[attr-defined]
        assert result == [1, 3]

    def test_empty_source(self, guard: object) -> None:
        result = guard._actual_uses("")  # type: ignore[attr-defined]
        assert result == []


# ── check ────────────────────────────────────────────────────────────────────


class TestCheck:
    """``check(repo_root)`` returns violations or an empty list."""

    def test_empty_repo_has_no_violations(self, tmp_path: Path, guard: object) -> None:
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert violations == []

    def test_violation_detected_in_arbitrary_file(self, tmp_path: Path, guard: object) -> None:
        bad = tmp_path / "maxwell_daemon" / "bad.py"
        bad.parent.mkdir(parents=True)
        bad.write_text("proc = await asyncio.create_subprocess_shell(cmd)\n")
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert len(violations) == 1
        assert "bad.py" in violations[0]

    def test_call_in_comment_not_flagged(self, tmp_path: Path, guard: object) -> None:
        ok = tmp_path / "maxwell_daemon" / "ok.py"
        ok.parent.mkdir(parents=True)
        ok.write_text("# create_subprocess_shell is intentionally avoided here\n")
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert violations == []

    def test_allowed_location_is_clean(self, tmp_path: Path, guard: object) -> None:
        """The one allowed call site (hooks.py / _shell_default_runner) is clean."""
        hooks = tmp_path / "maxwell_daemon" / "hooks.py"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            dedent("""\
            async def _shell_default_runner(command, *, cwd, env, timeout):
                proc = await asyncio.create_subprocess_shell(command)
                return 0, ""
        """)
        )
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert violations == []

    def test_wrong_function_in_allowed_file_is_violation(
        self, tmp_path: Path, guard: object
    ) -> None:
        """Even in hooks.py, calls outside _shell_default_runner are violations."""
        hooks = tmp_path / "maxwell_daemon" / "hooks.py"
        hooks.parent.mkdir(parents=True)
        hooks.write_text(
            dedent("""\
            async def some_other_function(command):
                # Calling shell here is not allowed — wrong function name.
                proc = await asyncio.create_subprocess_shell(command)
                return 0, ""
        """)
        )
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert len(violations) == 1

    def test_test_files_are_skipped(self, tmp_path: Path, guard: object) -> None:
        """Test files are allowed to monkeypatch the symbol without triggering CI."""
        test_file = tmp_path / "tests" / "test_hooks.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            'monkeypatch.setattr("maxwell_daemon.hooks.asyncio.create_subprocess_shell", _fake)\n'
        )
        violations = guard.check(tmp_path)  # type: ignore[attr-defined]
        assert violations == []


# ── Live repo smoke ──────────────────────────────────────────────────────────


class TestLiveRepo:
    """Smoke: the CI guard passes against the actual repo."""

    def test_repo_passes_guard(self, guard: object) -> None:
        """The live codebase must have no violations of the subprocess-shell guard.

        If this test fails, a ``create_subprocess_shell`` call has been added
        outside the one allowed location.  Remove it or add shell semantics
        via HookSpec.shell=True instead of calling the asyncio primitive directly.
        """
        repo_root = Path(__file__).resolve().parents[2]
        violations = guard.check(repo_root)  # type: ignore[attr-defined]
        assert violations == [], (
            "create_subprocess_shell found outside _shell_default_runner:\n" + "\n".join(violations)
        )
