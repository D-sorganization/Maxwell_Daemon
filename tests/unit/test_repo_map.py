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


# ── JavaScript symbol extraction ────────────────────────────────────────────


class TestJavaScriptExtraction:
    def test_extracts_function_class_export_and_arrow(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "app/core.js",
            """
            function foo() { return 1; }

            class Foo {
              method() { return 2; }
            }

            export default function bar() { return 3; }

            const baz = (x) => x + 1;
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "app/core.js")
        assert "foo" in entry.functions
        assert "bar" in entry.functions
        assert "baz" in entry.functions
        assert "Foo" in entry.classes

    def test_skips_underscore_prefixed(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "a.js",
            """
            function _hidden() {}
            function visible() {}
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "a.js")
        assert "visible" in entry.functions
        assert "_hidden" not in entry.functions

    def test_jsx_also_parsed(self, tmp_path: Path) -> None:
        _w(tmp_path, "ui/Button.jsx", "export function Button() { return null; }\n")
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "ui/Button.jsx")
        assert "Button" in entry.functions


# ── TypeScript symbol extraction ────────────────────────────────────────────


class TestTypeScriptExtraction:
    def test_extracts_js_and_ts_constructs(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "app/core.ts",
            """
            function foo(): number { return 1; }

            class Foo {
              method(): number { return 2; }
            }

            export default function bar(): number { return 3; }

            const baz = (x: number): number => x + 1;

            interface IFoo {
              value: number;
            }

            export type TFoo = {
              value: number;
            };
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "app/core.ts")
        assert "foo" in entry.functions
        assert "bar" in entry.functions
        assert "baz" in entry.functions
        assert "Foo" in entry.classes
        assert "IFoo" in entry.classes
        assert "TFoo" in entry.classes

    def test_tsx_also_parsed(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "ui/Btn.tsx",
            """
            interface Props {
              label: string;
            }

            export function Btn(props: Props) { return null; }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "ui/Btn.tsx")
        assert "Btn" in entry.functions
        assert "Props" in entry.classes


# ── Go symbol extraction ────────────────────────────────────────────────────


class TestGoExtraction:
    def test_extracts_exported_only(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "svc/handler.go",
            """
            package svc

            func Foo() int { return 1 }

            func foo() int { return 2 }

            type Foo struct {
                Name string
            }

            type ifoo interface {
                do()
            }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "svc/handler.go")
        assert "Foo" in entry.functions
        assert "foo" not in entry.functions
        assert "Foo" in entry.classes
        assert "ifoo" not in entry.classes

    def test_method_receiver_captured(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "svc/m.go",
            """
            package svc

            type Thing struct{}

            func (t *Thing) Describe() string { return "" }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "svc/m.go")
        assert "Describe" in entry.functions
        assert "Thing" in entry.classes


# ── Rust symbol extraction ──────────────────────────────────────────────────


class TestRustExtraction:
    def test_extracts_fn_struct_impl_methods(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "src/lib.rs",
            """
            pub fn foo() -> i32 { 1 }

            fn _private() {}

            pub struct Bar {
                pub value: i32,
            }

            impl Bar {
                pub fn baz(&self) -> i32 { self.value }
                fn _internal(&self) {}
            }

            pub enum Color { Red, Blue }

            pub trait Drawable {
                fn draw(&self);
            }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "src/lib.rs")
        assert "foo" in entry.functions
        assert "_private" not in entry.functions
        assert "Bar.baz" in entry.functions
        assert "Bar._internal" not in entry.functions
        # The inner fn should not leak as a free fn:
        assert "baz" not in entry.functions
        assert "Bar" in entry.classes
        assert "Color" in entry.classes
        assert "Drawable" in entry.classes


# ── Java symbol extraction ──────────────────────────────────────────────────


class TestJavaExtraction:
    def test_extracts_class_and_methods(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "src/Foo.java",
            """
            public class Foo {
                private int counter;

                public int bump() {
                    counter++;
                    return counter;
                }

                private String describe() {
                    return "foo";
                }
            }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "src/Foo.java")
        assert "Foo" in entry.classes
        assert "bump" in entry.functions
        assert "describe" in entry.functions

    def test_interface_captured(self, tmp_path: Path) -> None:
        _w(
            tmp_path,
            "src/Doable.java",
            """
            public interface Doable {
                void doIt();
            }
            """,
        )
        rm = build_repo_map(tmp_path)
        entry = _entry_for(rm, "src/Doable.java")
        assert "Doable" in entry.classes


# ── Polyglot sweep ──────────────────────────────────────────────────────────


class TestPolyglotSweep:
    def test_mixed_workspace(self, tmp_path: Path) -> None:
        _w(tmp_path, "py/a.py", "def py_func(): ...\nclass PyCls: ...\n")
        _w(
            tmp_path,
            "js/a.js",
            """
            function js_func() {}
            class JsCls {}
            """,
        )
        _w(
            tmp_path,
            "ts/a.ts",
            """
            interface ITs {}
            export function ts_func(): void {}
            """,
        )
        _w(
            tmp_path,
            "go/a.go",
            """
            package go_pkg
            func GoFunc() {}
            type GoCls struct{}
            """,
        )
        _w(
            tmp_path,
            "rs/a.rs",
            """
            pub fn rs_func() {}
            pub struct RsCls;
            """,
        )
        _w(
            tmp_path,
            "java/A.java",
            """
            public class JavaCls {
                public int javaMethod() { return 0; }
            }
            """,
        )
        rm = build_repo_map(tmp_path)
        by_path = {e.path: e for e in rm.entries}

        assert "py_func" in by_path["py/a.py"].functions
        assert "PyCls" in by_path["py/a.py"].classes

        assert "js_func" in by_path["js/a.js"].functions
        assert "JsCls" in by_path["js/a.js"].classes

        assert "ts_func" in by_path["ts/a.ts"].functions
        assert "ITs" in by_path["ts/a.ts"].classes

        assert "GoFunc" in by_path["go/a.go"].functions
        assert "GoCls" in by_path["go/a.go"].classes

        assert "rs_func" in by_path["rs/a.rs"].functions
        assert "RsCls" in by_path["rs/a.rs"].classes

        assert "JavaCls" in by_path["java/A.java"].classes
        assert "javaMethod" in by_path["java/A.java"].functions


# ── Unsupported file types ──────────────────────────────────────────────────


class TestUnsupportedFilesSkipped:
    def test_md_txt_yaml_ignored(self, tmp_path: Path) -> None:
        _w(tmp_path, "README.md", "# Hello\n")
        _w(tmp_path, "notes.txt", "not code\n")
        _w(tmp_path, "config.yaml", "key: value\n")
        _w(tmp_path, "data.json", '{"key": "value"}\n')
        _w(tmp_path, "real.py", "def real(): ...\n")
        rm = build_repo_map(tmp_path)
        paths = {e.path for e in rm.entries}
        assert paths == {"real.py"}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _entry_for(rm: RepoMap, path: str) -> RepoMapEntry:
    for e in rm.entries:
        if e.path == path:
            return e
    raise AssertionError(f"no entry for {path!r} in map")
