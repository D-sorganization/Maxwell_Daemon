"""Typed models for source-controlled ``.maxwell/checks`` definitions."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.core.model_selector import ModelTier

__all__ = [
    "CheckApplicability",
    "CheckDefinition",
    "CheckSeverity",
    "CheckTrigger",
]


class CheckSeverity(str, Enum):
    """How strongly a check should gate delivery."""

    ADVISORY = "advisory"
    REQUIRED = "required"
    BLOCKING = "blocking"


class CheckApplicability(BaseModel):
    """Path-based applicability rules for a check."""

    model_config = ConfigDict(extra="forbid")

    globs: tuple[str, ...] = ()

    @field_validator("globs", mode="before")
    @classmethod
    def _coerce_globs(cls, value: object) -> tuple[str, ...]:
        return _coerce_str_tuple(value)

    def matches_path(self, path: str | Path) -> bool:
        """Return ``True`` when any configured glob matches ``path``."""

        normalized = _normalize_repo_path(path)
        return any(_glob_matches(normalized, pattern) for pattern in self.globs)

    def matches_any(self, paths: Iterable[str | Path]) -> bool:
        """Return ``True`` when any path in ``paths`` matches the globs."""

        return any(self.matches_path(path) for path in paths)


class CheckTrigger(BaseModel):
    """Event triggers that can activate a check."""

    model_config = ConfigDict(extra="forbid")

    events: tuple[str, ...] = ()

    @field_validator("events", mode="before")
    @classmethod
    def _coerce_events(cls, value: object) -> tuple[str, ...]:
        return _coerce_str_tuple(value)

    def matches_event(self, event: str) -> bool:
        return event in self.events


class CheckDefinition(BaseModel):
    """One source-controlled Maxwell check definition."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    severity: CheckSeverity = CheckSeverity.REQUIRED
    applies_to: CheckApplicability = Field(default_factory=CheckApplicability)
    trigger: CheckTrigger = Field(default_factory=CheckTrigger)
    model_tier: ModelTier = ModelTier.MODERATE
    body: str = Field(..., min_length=1)
    source: Path | None = None

    @field_validator("id", "name", "body", mode="before")
    @classmethod
    def _strip_and_reject_blank(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must be non-empty")
        return stripped

    @field_validator("source", mode="before")
    @classmethod
    def _coerce_source(cls, value: object) -> Path | None:
        if value is None:
            return None
        if isinstance(value, Path):
            return value
        if isinstance(value, str):
            return Path(value).expanduser()
        raise TypeError(f"source must be str or Path, got {type(value).__name__!r}")

    @model_validator(mode="after")
    def _validate_contract(self) -> CheckDefinition:
        if not self.applies_to.globs:
            raise ValueError("check applies_to.globs must contain at least one glob")
        if not self.trigger.events:
            raise ValueError("check trigger.events must contain at least one event")
        if not self.body.strip():
            raise ValueError("check body must be non-empty")
        return self

    def applies_to_paths(self, paths: Iterable[str | Path]) -> bool:
        """Return ``True`` when any changed path matches the check globs."""

        return self.applies_to.matches_any(paths)

    def triggers_on(self, event: str) -> bool:
        """Return ``True`` when the check listens for ``event``."""

        return self.trigger.matches_event(event)


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values: tuple[str, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(str(item) for item in value)
    else:
        raise TypeError("value must be a string or list/tuple of strings")

    cleaned = tuple(item.strip() for item in values)
    if any(not item for item in cleaned):
        raise ValueError("entries must be non-empty")
    return cleaned


def _normalize_repo_path(path: str | Path) -> str:
    normalized = path.as_posix() if isinstance(path, Path) else str(path).replace("\\", "/")
    return normalized.lstrip("./")


def _glob_matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )
