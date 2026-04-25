"""Typed GAAI governed backlog metadata models."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maxwell_daemon.core.work_items import REPO_PATTERN

RiskLevel = Literal["low", "medium", "high", "critical"]


class GaaiAcceptanceCriterion(BaseModel):
    """One governed acceptance criterion imported from GAAI metadata."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    verification: str | None = None


class GaaiArtifactReference(BaseModel):
    """A local artifact reference declared by a GAAI backlog item."""

    model_config = ConfigDict(extra="forbid")

    path: PurePosixPath
    name: str | None = None
    kind: Literal[
        "plan",
        "diff",
        "command_log",
        "test_result",
        "check_result",
        "screenshot",
        "transcript",
        "handoff",
        "pr_body",
        "metadata",
        "other",
    ] = "metadata"
    media_type: str | None = None
    sha256: str | None = Field(default=None, min_length=64, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("path", mode="before")
    @classmethod
    def _coerce_contained_posix_path(cls, value: object) -> PurePosixPath:
        if not isinstance(value, str):
            raise TypeError("artifact path must be a string")
        normalized = value.replace("\\", "/").strip()
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or ".." in path.parts
            or ":" in normalized
        ):
            raise ValueError(f"artifact path must be relative and contained: {value}")
        return path

    @field_validator("sha256")
    @classmethod
    def _sha256_is_hex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        int(value, 16)
        return value.lower()


class GaaiScope(BaseModel):
    """Governance boundaries declared with a GAAI backlog item."""

    model_config = ConfigDict(extra="forbid")

    allowed_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    allowed_commands: tuple[str, ...] = ()
    risk_level: RiskLevel = "medium"

    @field_validator("allowed_paths", "denied_paths", mode="before")
    @classmethod
    def _coerce_paths(cls, value: object) -> tuple[str, ...]:
        values = _coerce_str_tuple(value)
        for item in values:
            normalized = item.replace("\\", "/")
            path = PurePosixPath(normalized)
            if path.is_absolute() or ".." in path.parts or ":" in normalized:
                raise ValueError(f"scope path must be relative and contained: {item}")
        return values

    @field_validator("allowed_commands", mode="before")
    @classmethod
    def _coerce_commands(cls, value: object) -> tuple[str, ...]:
        return _coerce_str_tuple(value)


class GaaiBacklogItem(BaseModel):
    """Governed backlog item parsed from local GAAI YAML or Markdown metadata."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    body: str = ""
    repo: str | None = Field(default=None, pattern=REPO_PATTERN)
    source_url: str | None = None
    labels: tuple[str, ...] = ()
    acceptance_criteria: tuple[GaaiAcceptanceCriterion, ...] = ()
    required_checks: tuple[str, ...] = ()
    scope: GaaiScope = Field(default_factory=GaaiScope)
    priority: int = Field(default=100, ge=0, le=1000)
    artifacts: tuple[GaaiArtifactReference, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_gaai_aliases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        _copy_alias(normalized, "id", ("key", "story_id", "item_id"))
        _copy_alias(normalized, "body", ("description", "summary"))
        _copy_alias(normalized, "source_url", ("url", "source"))
        _copy_alias(normalized, "required_checks", ("checks", "validation"))
        _copy_alias(normalized, "acceptance_criteria", ("acceptance", "criteria"))
        _copy_alias(normalized, "artifacts", ("artifact_refs", "artifact_references"))
        if "acceptance_criteria" in normalized:
            normalized["acceptance_criteria"] = _normalize_acceptance(
                normalized["acceptance_criteria"]
            )
        if "artifacts" in normalized:
            normalized["artifacts"] = _normalize_artifacts(normalized["artifacts"])
        return normalized

    @field_validator("labels", "required_checks", mode="before")
    @classmethod
    def _coerce_named_strings(cls, value: object) -> tuple[str, ...]:
        return _coerce_str_tuple(value)


def _copy_alias(data: dict[str, Any], canonical: str, aliases: tuple[str, ...]) -> None:
    if canonical in data:
        return
    for alias in aliases:
        if alias in data:
            data[canonical] = data[alias]
            return


def _normalize_acceptance(value: object) -> object:
    if not isinstance(value, list | tuple):
        return value
    normalized: list[object] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            normalized.append({"id": f"AC{index}", "text": item})
        else:
            normalized.append(item)
    return normalized


def _normalize_artifacts(value: object) -> object:
    if not isinstance(value, list | tuple):
        return value
    normalized: list[object] = []
    for item in value:
        if isinstance(item, str):
            normalized.append({"path": item})
        else:
            normalized.append(item)
    return normalized


def _coerce_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    values: tuple[str, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list | tuple):
        values = tuple(str(item) for item in value)
    else:
        raise TypeError("value must be a string or list of strings")
    if any(not item.strip() for item in values):
        raise ValueError("entries must be non-empty")
    return tuple(item.strip() for item in values)
