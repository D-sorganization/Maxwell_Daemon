"""Loader for source-controlled ``.maxwell/checks/*.md`` files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from maxwell_daemon.checks.models import CheckDefinition

__all__ = ["CheckLoadError", "load_check", "load_checks", "select_checks"]


_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n?(?P<body>.*)\Z",
    re.DOTALL,
)


class CheckLoadError(ValueError):
    """Raised when a check file is structurally invalid."""


def load_check(path: Path | str) -> CheckDefinition:
    """Load one check file from disk."""

    check_path = Path(path).expanduser()
    if not check_path.is_file():
        raise CheckLoadError(f"{check_path}: check file does not exist")
    if check_path.suffix.lower() != ".md":
        raise CheckLoadError(
            f"{check_path}: unsupported check extension {check_path.suffix!r}"
        )

    try:
        text = check_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CheckLoadError(f"{check_path}: could not read check file: {exc}") from exc

    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise CheckLoadError(
            f"{check_path}: no YAML frontmatter block (expected --- ... --- at top)"
        )

    try:
        parsed: Any = yaml.safe_load(match.group("yaml")) or {}
    except yaml.YAMLError as exc:
        raise CheckLoadError(
            f"{check_path}: YAML frontmatter is invalid: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise CheckLoadError(f"{check_path}: YAML frontmatter must be a mapping")

    payload = dict(parsed)
    payload["body"] = match.group("body").strip()
    payload["source"] = check_path

    try:
        return CheckDefinition.model_validate(payload)
    except ValidationError as exc:
        raise CheckLoadError(f"{check_path}: invalid check definition: {exc}") from exc


def load_checks(directory: Path | str) -> tuple[CheckDefinition, ...]:
    """Load every ``*.md`` check under ``directory`` in deterministic order."""

    check_dir = Path(directory).expanduser()
    if not check_dir.is_dir():
        return ()

    checks: list[CheckDefinition] = []
    seen_ids: dict[str, Path] = {}
    for path in sorted(check_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix.lower() != ".md":
            continue
        check = load_check(path)
        previous = seen_ids.get(check.id)
        if previous is not None:
            raise CheckLoadError(
                f"{path}: duplicate check id {check.id!r} also defined in {previous}"
            )
        seen_ids[check.id] = path
        checks.append(check)
    return tuple(checks)


def select_checks(
    checks: tuple[CheckDefinition, ...],
    *,
    touched_paths: tuple[str | Path, ...],
) -> tuple[CheckDefinition, ...]:
    """Return the subset of checks that apply to at least one touched path."""

    return tuple(check for check in checks if check.applies_to_paths(touched_paths))
