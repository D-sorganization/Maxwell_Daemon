"""Pure mappers from parsed GAAI metadata into Maxwell-friendly DTOs."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from maxwell_daemon.core.artifacts import ArtifactKind
from maxwell_daemon.core.work_items import AcceptanceCriterion, ScopeBoundary, WorkItem
from maxwell_daemon.gaai.models import GaaiArtifactReference, GaaiBacklogItem


class MaxwellArtifactImport(BaseModel):
    """Non-persisting DTO describing an artifact Maxwell may import later."""

    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(..., min_length=1)
    source_path: PurePosixPath
    kind: ArtifactKind
    name: str
    media_type: str
    sha256: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def map_gaai_item_to_work_item(item: GaaiBacklogItem) -> WorkItem:
    """Convert a parsed GAAI backlog item into a draft Maxwell work item."""

    return WorkItem(
        id=item.id,
        title=item.title,
        body=item.body,
        repo=item.repo,
        source="gaai",
        source_url=item.source_url,
        acceptance_criteria=tuple(
            AcceptanceCriterion(
                id=criterion.id,
                text=criterion.text,
                verification=criterion.verification,
            )
            for criterion in item.acceptance_criteria
        ),
        scope=ScopeBoundary(
            allowed_paths=item.scope.allowed_paths,
            denied_paths=item.scope.denied_paths,
            allowed_commands=item.scope.allowed_commands,
            risk_level=item.scope.risk_level,
        ),
        required_checks=item.required_checks,
        priority=item.priority,
    )


def map_gaai_artifacts(item: GaaiBacklogItem) -> tuple[MaxwellArtifactImport, ...]:
    """Convert declared GAAI artifact references into Maxwell import DTOs."""

    return tuple(_map_artifact(item.id, reference) for reference in item.artifacts)


def _map_artifact(
    work_item_id: str, reference: GaaiArtifactReference
) -> MaxwellArtifactImport:
    media_type = reference.media_type or _media_type_for_path(reference.path)
    return MaxwellArtifactImport(
        work_item_id=work_item_id,
        source_path=reference.path,
        kind=_artifact_kind(reference.kind),
        name=reference.name or reference.path.name,
        media_type=media_type,
        sha256=reference.sha256,
        metadata={"gaai": True} | reference.metadata,
    )


def _artifact_kind(kind: str) -> ArtifactKind:
    if kind == "other":
        return ArtifactKind.METADATA
    return ArtifactKind(kind)


def _media_type_for_path(path: PurePosixPath) -> str:
    suffix = path.suffix.lower()
    return {
        ".json": "application/json",
        ".md": "text/markdown",
        ".markdown": "text/markdown",
        ".txt": "text/plain",
        ".diff": "text/x-diff",
        ".patch": "text/x-diff",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suffix, "application/octet-stream")
