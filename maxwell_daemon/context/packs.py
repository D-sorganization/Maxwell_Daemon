"""Deterministic local context-pack generation.

Context packs are a filesystem-only snapshot that can be handed to a later
prompt builder, CLI, or API without binding this module to those surfaces.
The pack records bounded file contents plus provider-rendered sections such
as contributor docs and a repository schematic.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maxwell_daemon.context.providers import (
    ContextProviderRegistry,
    ContextProviderResult,
    DocsProvider,
    RepoSchematicProvider,
)
from maxwell_daemon.contracts import require

__all__ = [
    "ContextPack",
    "ContextPackFileEntry",
    "ContextPackMetadata",
    "ContextPackPolicy",
    "ContextPackSection",
    "build_context_pack",
    "default_context_pack_registry",
]


_SCHEMA_VERSION = "context-pack.v1"
_DEFAULT_PROVIDER_NAMES: tuple[str, ...] = ("docs", "repo_schematic")
_DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".cache",
    }
)
_DEFAULT_TEXT_SUFFIXES: frozenset[str] = frozenset(
    {
        ".cfg",
        ".css",
        ".go",
        ".h",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".py",
        ".rs",
        ".rst",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_DEFAULT_TEXT_FILENAMES: frozenset[str] = frozenset(
    {
        "AGENTS",
        "AGENTS.md",
        "CHANGELOG",
        "CHANGELOG.md",
        "CONTRIBUTING",
        "CONTRIBUTING.md",
        "LICENSE",
        "README",
    }
)
_BINARY_SAMPLE_BYTES = 2048


@dataclass(frozen=True, slots=True)
class ContextPackPolicy:
    """Filesystem inclusion limits for a local context pack."""

    max_file_bytes: int = 64 * 1024
    max_total_bytes: int = 256 * 1024
    provider_budget_chars: int = 8_000
    skip_dirs: frozenset[str] = _DEFAULT_SKIP_DIRS
    text_suffixes: frozenset[str] = _DEFAULT_TEXT_SUFFIXES
    text_filenames: frozenset[str] = _DEFAULT_TEXT_FILENAMES


@dataclass(frozen=True, slots=True)
class ContextPackMetadata:
    """Stable metadata for a generated context pack."""

    schema_version: str
    repo_name: str
    file_count: int
    included_file_count: int
    total_included_bytes: int
    provider_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextPackFileEntry:
    """One scanned file and its inclusion decision."""

    path: str
    size_bytes: int
    sha256: str
    included: bool
    text: str = ""
    skip_reason: str | None = None

    def to_manifest(self, *, include_text: bool = False) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "included": self.included,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.skip_reason is not None:
            manifest["skip_reason"] = self.skip_reason
        if include_text and self.included:
            manifest["text"] = self.text
        return manifest


@dataclass(frozen=True, slots=True)
class ContextPackSection:
    """Rendered output from a context provider."""

    name: str
    size_chars: int
    sha256: str
    text: str

    @classmethod
    def from_provider_result(cls, result: ContextProviderResult) -> ContextPackSection:
        return cls(
            name=result.name,
            size_chars=result.size_chars,
            sha256=_sha256_text(result.text),
            text=result.text,
        )

    def to_manifest(self, *, include_text: bool = False) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "name": self.name,
            "sha256": self.sha256,
            "size_chars": self.size_chars,
        }
        if include_text:
            manifest["text"] = self.text
        return manifest


@dataclass(frozen=True, slots=True)
class ContextPack:
    """A deterministic, local context snapshot."""

    metadata: ContextPackMetadata
    files: tuple[ContextPackFileEntry, ...] = field(default_factory=tuple)
    sections: tuple[ContextPackSection, ...] = field(default_factory=tuple)

    def included_files(self) -> tuple[ContextPackFileEntry, ...]:
        return tuple(file for file in self.files if file.included)

    def to_manifest(self, *, include_text: bool = False) -> dict[str, Any]:
        return {
            "files": [file.to_manifest(include_text=include_text) for file in self.files],
            "metadata": {
                "file_count": self.metadata.file_count,
                "included_file_count": self.metadata.included_file_count,
                "provider_names": list(self.metadata.provider_names),
                "repo_name": self.metadata.repo_name,
                "schema_version": self.metadata.schema_version,
                "total_included_bytes": self.metadata.total_included_bytes,
            },
            "sections": [
                section.to_manifest(include_text=include_text) for section in self.sections
            ],
        }

    def stable_manifest_json(self, *, include_text: bool = False) -> str:
        return (
            json.dumps(
                self.to_manifest(include_text=include_text),
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )


def default_context_pack_registry(repo_root: Path) -> ContextProviderRegistry:
    """Return the built-in local providers used by context packs."""
    registry = ContextProviderRegistry()
    registry.register(DocsProvider(workspace=repo_root))
    registry.register(RepoSchematicProvider(workspace=repo_root))
    return registry


async def build_context_pack(
    repo_root: Path,
    *,
    query: str = "",
    policy: ContextPackPolicy | None = None,
    provider_names: Sequence[str] = _DEFAULT_PROVIDER_NAMES,
    registry: ContextProviderRegistry | None = None,
) -> ContextPack:
    """Build a bounded context pack from a local repository root."""
    require(
        repo_root.is_dir(),
        f"build_context_pack: repo_root {repo_root} must exist and be a directory",
    )
    selected_policy = policy or ContextPackPolicy()
    require(
        selected_policy.max_file_bytes > 0,
        "build_context_pack: max_file_bytes must be > 0",
    )
    require(
        selected_policy.max_total_bytes > 0,
        "build_context_pack: max_total_bytes must be > 0",
    )
    require(
        selected_policy.provider_budget_chars > 0,
        "build_context_pack: provider_budget_chars must be > 0",
    )

    resolved_root = repo_root.resolve()
    files = _collect_file_entries(resolved_root, selected_policy)
    provider_sections = await _render_provider_sections(
        registry or default_context_pack_registry(resolved_root),
        query=query,
        provider_names=tuple(provider_names),
        budget_chars=selected_policy.provider_budget_chars,
    )
    included_bytes = sum(file.size_bytes for file in files if file.included)
    metadata = ContextPackMetadata(
        schema_version=_SCHEMA_VERSION,
        repo_name=resolved_root.name,
        file_count=len(files),
        included_file_count=sum(1 for file in files if file.included),
        total_included_bytes=included_bytes,
        provider_names=tuple(provider_names),
    )
    return ContextPack(
        metadata=metadata,
        files=tuple(files),
        sections=tuple(provider_sections),
    )


async def _render_provider_sections(
    registry: ContextProviderRegistry,
    *,
    query: str,
    provider_names: tuple[str, ...],
    budget_chars: int,
) -> list[ContextPackSection]:
    sections: list[ContextPackSection] = []
    for name in provider_names:
        result = await registry.get(name).render(query=query, budget_chars=budget_chars)
        if result.text:
            sections.append(ContextPackSection.from_provider_result(result))
    return sections


def _collect_file_entries(root: Path, policy: ContextPackPolicy) -> list[ContextPackFileEntry]:
    entries: list[ContextPackFileEntry] = []
    running_bytes = 0
    for path in _iter_files(root, skip_dirs=policy.skip_dirs):
        entry = _file_entry(root, path, policy=policy, running_bytes=running_bytes)
        if entry.included:
            running_bytes += entry.size_bytes
        entries.append(entry)
    return entries


def _file_entry(
    root: Path,
    path: Path,
    *,
    policy: ContextPackPolicy,
    running_bytes: int,
) -> ContextPackFileEntry:
    relpath = _relative_path(root, path)
    try:
        stat = path.stat()
    except OSError:
        return ContextPackFileEntry(
            path=relpath,
            size_bytes=0,
            sha256="",
            included=False,
            skip_reason="stat_failed",
        )

    size_bytes = stat.st_size
    digest = _sha256_file(path)
    if not _is_text_candidate(path, policy):
        return _skipped_file(relpath, size_bytes, digest, "unsupported_file_type")
    if size_bytes > policy.max_file_bytes:
        return _skipped_file(relpath, size_bytes, digest, "file_size_limit")
    if running_bytes + size_bytes > policy.max_total_bytes:
        return _skipped_file(relpath, size_bytes, digest, "total_size_limit")

    try:
        data = path.read_bytes()
    except OSError:
        return _skipped_file(relpath, size_bytes, digest, "read_failed")
    if _looks_binary(data[:_BINARY_SAMPLE_BYTES]):
        return _skipped_file(relpath, size_bytes, digest, "binary_file")

    return ContextPackFileEntry(
        path=relpath,
        size_bytes=size_bytes,
        sha256=digest,
        included=True,
        text=data.decode("utf-8", errors="replace"),
    )


def _skipped_file(
    relpath: str,
    size_bytes: int,
    digest: str,
    reason: str,
) -> ContextPackFileEntry:
    return ContextPackFileEntry(
        path=relpath,
        size_bytes=size_bytes,
        sha256=digest,
        included=False,
        skip_reason=reason,
    )


def _iter_files(root: Path, *, skip_dirs: frozenset[str]) -> list[Path]:
    paths: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(directory.iterdir(), key=_sort_key)
        except OSError:
            return
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                if child.name in skip_dirs or child.name.startswith("."):
                    continue
                visit(child)
            elif child.is_file():
                paths.append(child)

    visit(root)
    return paths


def _sort_key(path: Path) -> str:
    return path.name.casefold()


def _is_text_candidate(path: Path, policy: ContextPackPolicy) -> bool:
    return path.name in policy.text_filenames or path.suffix.lower() in policy.text_suffixes


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _relative_path(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
