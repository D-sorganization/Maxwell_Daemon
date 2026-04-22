"""Filesystem-safe local loader for GAAI backlog YAML and Markdown metadata."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml import YAMLError

from maxwell_daemon.gaai.models import GaaiBacklogItem

SUPPORTED_EXTENSIONS = frozenset({".yaml", ".yml", ".md", ".markdown"})


class GaaiLoadError(ValueError):
    """Raised when local GAAI metadata cannot be loaded safely."""


def load_gaai_item(path: Path | str, *, root: Path | str | None = None) -> GaaiBacklogItem:
    """Load one local GAAI backlog item file.

    ``root`` constrains reads to a known directory. If omitted, the item's parent
    directory is used as the containment root.
    """

    item_path = Path(path)
    root_path = Path(root) if root is not None else item_path.parent
    safe_path = _safe_file_path(item_path, root_path)
    if safe_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise GaaiLoadError(f"unsupported GAAI metadata extension: {safe_path.suffix}")

    try:
        if safe_path.suffix.lower() in {".md", ".markdown"}:
            data = _load_markdown_metadata(safe_path)
        else:
            data = _load_yaml_metadata(safe_path)
        return GaaiBacklogItem.model_validate(data)
    except (OSError, YAMLError, ValidationError, TypeError, ValueError) as exc:
        raise GaaiLoadError(f"failed to load GAAI metadata from {safe_path}: {exc}") from exc


def load_gaai_items(root: Path | str) -> list[GaaiBacklogItem]:
    """Load all supported GAAI backlog files under ``root`` in deterministic order."""

    root_path = Path(root)
    safe_root = _safe_root(root_path)
    items: list[GaaiBacklogItem] = []
    for path in _iter_supported_files(safe_root):
        items.append(load_gaai_item(path, root=safe_root))
    return items


def _safe_root(root: Path) -> Path:
    try:
        safe_root = root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GaaiLoadError(f"GAAI metadata root does not exist: {root}") from exc
    if not safe_root.is_dir():
        raise GaaiLoadError(f"GAAI metadata root is not a directory: {root}")
    return safe_root


def _safe_file_path(path: Path, root: Path) -> Path:
    safe_root = _safe_root(root)
    candidate = path if path.is_absolute() else safe_root / path
    try:
        safe_path = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        raise GaaiLoadError(f"GAAI metadata file does not exist: {path}") from exc
    if not safe_path.is_file():
        raise GaaiLoadError(f"GAAI metadata path is not a file: {path}")
    if safe_path != safe_root and safe_root not in safe_path.parents:
        raise GaaiLoadError(f"GAAI metadata path escapes root: {path}")
    return safe_path


def _iter_supported_files(root: Path) -> Iterable[Path]:
    paths = (
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS
    )
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _load_yaml_metadata(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError("GAAI YAML metadata must be a mapping")
    return raw


def _load_markdown_metadata(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("Markdown GAAI metadata requires YAML front matter")
    end_index = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), -1)
    if end_index < 0:
        raise ValueError("Markdown GAAI metadata front matter is not closed")
    front_matter = "\n".join(lines[1:end_index])
    raw = yaml.safe_load(front_matter) or {}
    if not isinstance(raw, dict):
        raise TypeError("Markdown GAAI front matter must be a mapping")
    body = "\n".join(lines[end_index + 1 :]).strip()
    raw.setdefault("body", body)
    return raw
