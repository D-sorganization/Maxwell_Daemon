"""Compact repository map — file/symbol outline for agent context.

Idea from Aider: instead of feeding the agent full files, give it a global
outline (file paths + top-level function and class names) that fits in
~2k tokens. The agent reads full files on demand via ``read_file``, but it
knows *where* things live without paying for that context every turn.

Scope: Python via stdlib ``ast`` (exact); JS/TS/Go/Rust/Java via regex
(best-effort). Zero extra deps — a future tree-sitter pass can replace
the regex extractors without touching the API.

DbC: ``build_repo_map(workspace)`` enforces workspace-is-a-directory.
Per-file parse failures are swallowed (one broken file shouldn't starve
the whole map).

LOD: each file parse is a pure function of its text. ``build_repo_map`` is
a thin coordinator — walk + parse + sort. No knowledge of prompts,
backends, or the agent loop.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from maxwell_daemon.contracts import require

__all__ = [
    "RepoMap",
    "RepoMapEntry",
    "build_repo_map",
]

#: Directories we never descend into — noise that dilutes the map.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".git",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".cache",
    }
)

#: File suffixes we extract symbols from. Anything else is ignored.
_SUPPORTED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
    }
)

_TRUNCATED_MARKER = "\n... (map truncated — read_file to drill in) ..."


@dataclass(slots=True, frozen=True)
class RepoMapEntry:
    """One file's outline: top-level functions + classes + class methods."""

    path: str
    functions: tuple[str, ...] = field(default_factory=tuple)
    classes: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        parts: list[str] = [f"- {self.path}"]
        for c in self.classes:
            parts.append(f"    class {c}")
        for f in self.functions:
            parts.append(f"    def {f}()")
        return "\n".join(parts)


@dataclass(slots=True, frozen=True)
class RepoMap:
    """Ordered collection of :class:`RepoMapEntry` for a workspace."""

    entries: tuple[RepoMapEntry, ...] = field(default_factory=tuple)

    def entry_count(self) -> int:
        return len(self.entries)

    def to_prompt(self, *, max_chars: int = 2000) -> str:
        """Render as a markdown outline with a bounded character budget.

        When rendering exceeds ``max_chars`` we stop early and emit a
        truncation marker so the agent knows the list is incomplete.
        """
        if not self.entries:
            return ""

        header = "## Repository map (file → symbols)\n\n"
        body_chunks: list[str] = []
        running_len = len(header)
        for entry in self.entries:
            chunk = entry.render() + "\n"
            if running_len + len(chunk) > max_chars:
                body_chunks.append(_TRUNCATED_MARKER.lstrip("\n"))
                break
            body_chunks.append(chunk)
            running_len += len(chunk)
        return header + "".join(body_chunks)


def build_repo_map(workspace: Path) -> RepoMap:
    """Walk ``workspace`` for source files and build a :class:`RepoMap`.

    One file's parse failure is swallowed so a single malformed module
    never disables the whole map.
    """
    require(
        workspace.is_dir(),
        f"build_repo_map: workspace {workspace} must be a directory",
    )
    entries: list[RepoMapEntry] = []
    for path in _walk_source_files(workspace):
        parser = _PARSERS.get(path.suffix)
        if parser is None:
            continue
        try:
            entry = parser(workspace, path)
        except Exception:
            # Belt-and-suspenders: per-file parsing must never kill the sweep.
            entry = None
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: e.path)
    return RepoMap(entries=tuple(entries))


def _walk_source_files(root: Path) -> list[Path]:
    """Return every supported source file under ``root`` with skip-dirs pruned."""

    def recurse(d: Path, out: list[Path]) -> None:
        try:
            children = sorted(d.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir():
                if child.name in _SKIP_DIRS or child.name.startswith("."):
                    continue
                recurse(child, out)
            elif child.is_file() and child.suffix in _SUPPORTED_SUFFIXES:
                out.append(child)

    gathered: list[Path] = []
    recurse(root, gathered)
    return gathered


def _read_text(path: Path) -> str | None:
    """Read a file as UTF-8 (replacing errors). ``None`` on I/O failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _relpath(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _finalize(
    root: Path,
    path: Path,
    functions: list[str],
    classes: list[str],
) -> RepoMapEntry | None:
    if not functions and not classes:
        return None
    return RepoMapEntry(
        path=_relpath(root, path),
        functions=tuple(functions),
        classes=tuple(classes),
    )


# ── Python (ast-based, exact) ────────────────────────────────────────────────


def _parse_python_file(root: Path, path: Path) -> RepoMapEntry | None:
    """Parse one Python file. Returns ``None`` on read / parse failure."""
    source = _read_text(path)
    if source is None:
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    functions: list[str] = []
    classes: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
            for sub in node.body:
                if isinstance(
                    sub, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not sub.name.startswith("_"):
                    functions.append(f"{node.name}.{sub.name}")

    return _finalize(root, path, functions, classes)


# ── JavaScript / TypeScript (regex, best-effort) ─────────────────────────────

# `function NAME(` — ordinary declarations.
_JS_FUNC_RE = re.compile(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", re.M)
# `export [default] [async] function NAME(` — exported declarations.
_JS_EXPORT_FUNC_RE = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
    re.M,
)
# `class NAME` — optionally exported, optionally extends.
_JS_CLASS_RE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?class\s+([A-Za-z_$][\w$]*)\b",
    re.M,
)
# Top-level `const NAME = ... =>` — arrow function bound to a const. The
# parameter list can carry TS return-type annotations (``): number``), so
# we accept any non-``>`` characters between the closing paren and ``=>``.
_JS_ARROW_CONST_RE = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)"
    r"(?:\s*:\s*[^=]+?)?\s*=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)"
    r"(?:\s*:\s*[^=>]+)?\s*=>",
    re.M,
)
# `interface NAME` — TS-only.
_TS_INTERFACE_RE = re.compile(
    r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)\b",
    re.M,
)
# `type NAME =` — TS-only alias (disambiguate from `typeof`).
_TS_TYPE_ALIAS_RE = re.compile(
    r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*(?:<[^=]*>)?\s*=",
    re.M,
)


def _js_extract(source: str) -> tuple[list[str], list[str]]:
    """Shared JS extractor. Returns (functions, classes) preserving order."""
    functions: list[str] = []
    classes: list[str] = []
    seen_funcs: set[str] = set()
    seen_classes: set[str] = set()

    def add_func(name: str) -> None:
        if name.startswith("_") or name in seen_funcs:
            return
        seen_funcs.add(name)
        functions.append(name)

    def add_class(name: str) -> None:
        if name.startswith("_") or name in seen_classes:
            return
        seen_classes.add(name)
        classes.append(name)

    for m in _JS_FUNC_RE.finditer(source):
        add_func(m.group(1))
    for m in _JS_EXPORT_FUNC_RE.finditer(source):
        add_func(m.group(1))
    for m in _JS_ARROW_CONST_RE.finditer(source):
        add_func(m.group(1))
    for m in _JS_CLASS_RE.finditer(source):
        add_class(m.group(1))
    return functions, classes


def _parse_javascript_file(root: Path, path: Path) -> RepoMapEntry | None:
    source = _read_text(path)
    if source is None:
        return None
    functions, classes = _js_extract(source)
    return _finalize(root, path, functions, classes)


def _parse_typescript_file(root: Path, path: Path) -> RepoMapEntry | None:
    source = _read_text(path)
    if source is None:
        return None
    functions, classes = _js_extract(source)
    seen_classes = set(classes)
    for m in _TS_INTERFACE_RE.finditer(source):
        name = m.group(1)
        if name.startswith("_") or name in seen_classes:
            continue
        seen_classes.add(name)
        classes.append(name)
    for m in _TS_TYPE_ALIAS_RE.finditer(source):
        name = m.group(1)
        if name.startswith("_") or name in seen_classes:
            continue
        seen_classes.add(name)
        classes.append(name)
    return _finalize(root, path, functions, classes)


# ── Go (regex, best-effort) ──────────────────────────────────────────────────

# `func NAME(` or `func (recv T) NAME(` — top-level functions and methods.
_GO_FUNC_RE = re.compile(
    r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^]]*\])?\s*\(",
    re.M,
)
# `type NAME struct { ... }`
_GO_TYPE_STRUCT_RE = re.compile(
    r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+struct\b",
    re.M,
)
# `type NAME interface { ... }`
_GO_TYPE_INTERFACE_RE = re.compile(
    r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+interface\b",
    re.M,
)
# `type NAME <other>` — aliases, enum-ish things.
_GO_TYPE_OTHER_RE = re.compile(
    r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?!struct\b|interface\b)\S",
    re.M,
)


def _parse_go_file(root: Path, path: Path) -> RepoMapEntry | None:
    source = _read_text(path)
    if source is None:
        return None
    functions: list[str] = []
    classes: list[str] = []
    seen_funcs: set[str] = set()
    seen_classes: set[str] = set()

    def is_exported(name: str) -> bool:
        return bool(name) and name[0].isupper()

    for m in _GO_FUNC_RE.finditer(source):
        name = m.group(1)
        if not is_exported(name) or name in seen_funcs:
            continue
        seen_funcs.add(name)
        functions.append(name)
    for regex in (_GO_TYPE_STRUCT_RE, _GO_TYPE_INTERFACE_RE, _GO_TYPE_OTHER_RE):
        for m in regex.finditer(source):
            name = m.group(1)
            if not is_exported(name) or name in seen_classes:
                continue
            seen_classes.add(name)
            classes.append(name)
    return _finalize(root, path, functions, classes)


# ── Rust (regex, best-effort) ────────────────────────────────────────────────

# Free `fn NAME(` at top level — we don't try to filter indentation because
# impl-bodies are handled by the dedicated impl scanner below.
_RUST_FN_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:const\s+)?(?:unsafe\s+)?"
    r"(?:extern\s+\"[^\"]+\"\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
_RUST_STRUCT_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
_RUST_ENUM_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
_RUST_TRAIT_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)
# `impl [<...>] [Trait for] Type` — capture the inherent/target type name.
_RUST_IMPL_RE = re.compile(
    r"impl\b(?:\s*<[^>]*>)?\s+" r"(?:[A-Za-z_][\w:]*(?:<[^>]*>)?\s+for\s+)?" r"([A-Za-z_][\w]*)",
)
_RUST_METHOD_RE = re.compile(
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:const\s+)?(?:unsafe\s+)?"
    r"fn\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.M,
)


def _rust_impl_blocks(source: str) -> list[tuple[str, int, int, str]]:
    """Return ``(type_name, body_start, body_end, body_text)`` for each impl block.

    Uses a lightweight brace-balanced scan — pyregex alone can't match
    balanced braces. We don't need perfection; strings/comments in bodies
    rarely derail simple method-signature extraction.
    """
    blocks: list[tuple[str, int, int, str]] = []
    for m in _RUST_IMPL_RE.finditer(source):
        name = m.group(1)
        brace = source.find("{", m.end())
        if brace == -1:
            continue
        depth = 1
        i = brace + 1
        while i < len(source) and depth > 0:
            c = source[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        if depth != 0:
            continue
        blocks.append((name, brace + 1, i - 1, source[brace + 1 : i - 1]))
    return blocks


def _mask_ranges(source: str, ranges: list[tuple[int, int]]) -> str:
    """Replace each ``[start, end)`` slice of ``source`` with spaces of equal length.

    Preserves offsets so line-anchored regex (``^``) keep behaving the same
    on the surviving text.
    """
    if not ranges:
        return source
    buf = list(source)
    for start, end in ranges:
        for i in range(start, min(end, len(buf))):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


def _parse_rust_file(root: Path, path: Path) -> RepoMapEntry | None:
    source = _read_text(path)
    if source is None:
        return None
    functions: list[str] = []
    classes: list[str] = []
    seen_funcs: set[str] = set()
    seen_classes: set[str] = set()

    def add_func(name: str) -> None:
        if name.startswith("_") or name in seen_funcs:
            return
        seen_funcs.add(name)
        functions.append(name)

    def add_class(name: str) -> None:
        if name.startswith("_") or name in seen_classes:
            return
        seen_classes.add(name)
        classes.append(name)

    impl_blocks = _rust_impl_blocks(source)
    for type_name, _start, _end, body in impl_blocks:
        add_class(type_name)
        for m in _RUST_METHOD_RE.finditer(body):
            method = m.group(1)
            if method.startswith("_"):
                continue
            qualified = f"{type_name}.{method}"
            if qualified in seen_funcs:
                continue
            seen_funcs.add(qualified)
            functions.append(qualified)

    # Blank out impl bodies so free-fn scanner doesn't double-count methods.
    masked = _mask_ranges(source, [(s, e) for _n, s, e, _b in impl_blocks])

    for m in _RUST_FN_RE.finditer(masked):
        add_func(m.group(1))
    for m in _RUST_STRUCT_RE.finditer(source):
        add_class(m.group(1))
    for m in _RUST_ENUM_RE.finditer(source):
        add_class(m.group(1))
    for m in _RUST_TRAIT_RE.finditer(source):
        add_class(m.group(1))

    return _finalize(root, path, functions, classes)


# ── Java (regex, best-effort) ────────────────────────────────────────────────

_JAVA_CLASS_RE = re.compile(
    r"\b(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?"
    r"class\s+([A-Za-z_][A-Za-z0-9_]*)",
)
_JAVA_INTERFACE_RE = re.compile(
    r"\b(?:public|private|protected)?\s*(?:static\s+)?interface\s+([A-Za-z_][A-Za-z0-9_]*)",
)
# Method: visibility + return type + name(  — avoid matching control
# keywords like `if (`, `for (`, `while (` by requiring a visibility modifier.
_JAVA_METHOD_RE = re.compile(
    r"\b(public|private|protected)\s+(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
    r"(?:<[^>]+>\s+)?"
    r"[A-Za-z_][\w<>\[\],\s?]*?\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(",
)

#: Java keywords that must never be confused with a method name.
_JAVA_NON_METHODS: frozenset[str] = frozenset(
    {
        "class",
        "interface",
        "enum",
        "if",
        "for",
        "while",
        "switch",
        "return",
        "new",
        "throw",
        "catch",
    }
)


def _parse_java_file(root: Path, path: Path) -> RepoMapEntry | None:
    source = _read_text(path)
    if source is None:
        return None
    functions: list[str] = []
    classes: list[str] = []
    seen_funcs: set[str] = set()
    seen_classes: set[str] = set()

    for m in _JAVA_CLASS_RE.finditer(source):
        name = m.group(1)
        # Classes conventionally start uppercase — skip lowercase-leading.
        if not name[:1].isupper() or name in seen_classes:
            continue
        seen_classes.add(name)
        classes.append(name)
    for m in _JAVA_INTERFACE_RE.finditer(source):
        name = m.group(1)
        if not name[:1].isupper() or name in seen_classes:
            continue
        seen_classes.add(name)
        classes.append(name)
    for m in _JAVA_METHOD_RE.finditer(source):
        name = m.group(2)
        if name in _JAVA_NON_METHODS or name in seen_classes or name in seen_funcs:
            continue
        seen_funcs.add(name)
        functions.append(name)

    return _finalize(root, path, functions, classes)


# ── Dispatcher ───────────────────────────────────────────────────────────────

_Parser = Callable[[Path, Path], "RepoMapEntry | None"]

_PARSERS: dict[str, _Parser] = {
    ".py": _parse_python_file,
    ".js": _parse_javascript_file,
    ".jsx": _parse_javascript_file,
    ".ts": _parse_typescript_file,
    ".tsx": _parse_typescript_file,
    ".go": _parse_go_file,
    ".rs": _parse_rust_file,
    ".java": _parse_java_file,
}
