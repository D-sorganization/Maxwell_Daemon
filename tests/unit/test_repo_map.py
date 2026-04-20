"""Tests for RepoMap — compact function/class signature index for large repos.

Aider's insight: instead of dumping whole files into the agent's context,
give it a global ~2k-token outline of what's where. The agent reads full
files only when it needs to.

All tests use ``tmp_path`` — no git, no network.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from maxwell_daemon.gh.repo_map import RepoMap, RepoMapEntry, build_repo_map


def _w(root: Path, relpath: str, body: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body).lstrip())
    return p


# ── Shape ────────────────────────────────────────────────────────────────────


class TestRepoMapShape:
    def test_empty_map_is_empty_string(self) -> None:
        rm = RepoMap()
        assert rm.to_prompt() == ""
        assert rm.entry_count() == 0

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        e = RepoMapEntry(path="a.py", functions=("foo",), classes=("Bar",))
        with pytest.raises(FrozenInstanceError):
            e.path = "b.py"  # type: ignore[misc]


# ── Python symbol extraction ────────────────────────────────────────────────


class TestPythonExtraction:
    def test_extracts_top_level_functions(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "pkg/core.py",
            """
            def first(x: int) -> int:
                return x

            def second() -> None:
                pass
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "pkg/core.py")
        assert set(entry.functions) == {"first", "second"}

    def test_extracts_classes_with_methods(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "pkg/m.py",
            """
            class Foo:
                def method_a(self) -> None: ...
                def method_b(self) -> int: return 1
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "pkg/m.py")
        assert "Foo" in entry.classes
        assert "Foo.method_a" in entry.functions
        assert "Foo.method_b" in entry.functions

    def test_skips_private_functions_by_default(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "pkg/m.py",
            """
            def public(): ...
            def _private(): ...
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "pkg/m.py")
        assert "public" in entry.functions
        assert "_private" not in entry.functions

    def test_async_function_captured(self, tmp_path: Path) -> None:
        _w(tmp_path, "a.py", "async def fetch() -> None: ...\n")
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "a.py")
        assert "fetch" in entry.functions

    def test_syntax_error_file_skipped(self, tmp_path: Path) -> None:
        _w(tmp_path, "good.py", "def good(): ...\n")
        _w(tmp_path, "bad.py", "def bad(:\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert "good.py" in paths
        # Malformed file is silently skipped so the whole map doesn't break.
        assert "bad.py" not in paths


# ── File discovery and exclusions ───────────────────────────────────────────


class TestFileDiscovery:
    def test_non_python_files_ignored(self, tmp_path: Path) -> None:
        _w(tmp_path, "readme.md", "# hi\n")
        _w(tmp_path, "a.py", "def a(): ...\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert paths == {"a.py"}

    def test_hidden_dirs_skipped(self, tmp_path: Path) -> None:
        _w(tmp_path, ".venv/lib.py", "def v(): ...\n")
        _w(tmp_path, "src/a.py", "def a(): ...\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert "src/a.py" in paths
        assert ".venv/lib.py" not in paths

    def test_pycache_skipped(self, tmp_path: Path) -> None:
        _w(tmp_path, "pkg/__pycache__/x.py", "def x(): ...\n")
        _w(tmp_path, "pkg/real.py", "def real(): ...\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert "pkg/real.py" in paths
        assert "pkg/__pycache__/x.py" not in paths

    def test_node_modules_skipped(self, tmp_path: Path) -> None:
        _w(tmp_path, "node_modules/foo/a.py", "def a(): ...\n")
        _w(tmp_path, "main.py", "def main(): ...\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert paths == {"main.py"}


# ── Rendering ────────────────────────────────────────────────────────────────


class TestPrompt:
    def test_section_header_present(self, tmp_path: Path) -> None:
        _w(tmp_path, "a.py", "def foo(): ...\n")
        rm = build_repo_map(tmp_path)
        prompt = rm.to_prompt()
        assert "Repository map" in prompt or "Repo map" in prompt

    def test_files_shown_with_symbols(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "pkg/core.py",
            """
            class Widget: ...
            def build() -> None: ...
            """,
        )
        rm = build_repo_map(tmp_path)
        prompt = rm.to_prompt()
        assert "pkg/core.py" in prompt
        assert "Widget" in prompt
        assert "build" in prompt

    def test_budget_truncation_preserves_structure(self, tmp_path: Path) -> None:
        for i in range(200):
            _w(tmp_path, f"m{i}.py", f"def f{i}(): ...\n")
        rm = build_repo_map(tmp_path)
        short = rm.to_prompt(max_chars=500)
        assert len(short) <= 600  # allow small header/truncation overhead
        assert "truncated" in short.lower()


# ── DbC + preconditions ─────────────────────────────────────────────────────


class TestPreconditions:
    def test_rejects_missing_workspace(self, tmp_path: Path) -> None:
        from maxwell_daemon.contracts import PreconditionError

        with pytest.raises(PreconditionError, match="workspace"):
            build_repo_map(tmp_path / "nope")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _entry_for(rm: RepoMap, path: str) -> RepoMapEntry:
    for e in rm.entries:
        if e.path == path:
            return e
    raise AssertionError(f"no entry for {path!r} in map")
