"""Compact repository map — file/symbol outline for agent context.

Idea from Aider: instead of feeding the agent full files, give it a global
outline (file paths + top-level function and class names) that fits in
~2k tokens. The agent reads full files on demand via ``read_file``, but it
knows *where* things live without paying for that context every turn.

Scope: Python only for now — we use stdlib ``ast``, zero extra deps. A
follow-up can add JS/TS/Rust via tree-sitter; the API is stable.

DbC: ``build_repo_map(workspace)`` enforces workspace-is-a-directory.
Per-file parse failures are swallowed (one broken file shouldn't starve
the whole map).

LOD: each file parse is a pure function of its text. ``build_repo_map`` is
a thin coordinator — walk + parse + sort. No knowledge of prompts,
backends, or the agent loop.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from conductor.contracts import require

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
    """Walk ``workspace`` for ``.py`` files and build a :class:`RepoMap`.

    One file's parse failure is swallowed so a single malformed module
    never disables the whole map.
    """
    require(
        workspace.is_dir(),
        f"build_repo_map: workspace {workspace} must be a directory",
    )
    entries: list[RepoMapEntry] = []
    for path in _walk_python_files(workspace):
        entry = _parse_file(workspace, path)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda e: e.path)
    return RepoMap(entries=tuple(entries))


def _walk_python_files(root: Path) -> list[Path]:
    """Return every ``.py`` file under ``root`` with skip-dirs pruned."""

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
            elif child.is_file() and child.suffix == ".py":
                out.append(child)

    gathered: list[Path] = []
    recurse(root, gathered)
    return gathered


def _parse_file(root: Path, path: Path) -> RepoMapEntry | None:
    """Parse one Python file. Returns ``None`` on read / parse failure."""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
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

    if not functions and not classes:
        return None

    try:
        rel = str(path.relative_to(root))
    except ValueError:
        rel = str(path)

    return RepoMapEntry(
        path=rel,
        functions=tuple(functions),
        classes=tuple(classes),
    )
