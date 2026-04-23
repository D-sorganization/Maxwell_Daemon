"""Local structured execution for Maxwell check definitions."""

from __future__ import annotations

import json
from pathlib import Path

from maxwell_daemon.checks.loader import load_checks
from maxwell_daemon.checks.models import CheckConclusion, CheckDefinition, CheckResult
from maxwell_daemon.core.artifacts import ArtifactKind, ArtifactStore


class LocalCheckRunner:
    """Run source-controlled checks without publishing to GitHub.

    This runner validates and records deterministic local results. Model-backed
    execution and GitHub check-run publishing are intentionally separate slices.
    """

    def __init__(self, repo: Path | str) -> None:
        self._repo = Path(repo)

    def list(self) -> tuple[CheckDefinition, ...]:
        return load_checks(self._repo / ".maxwell" / "checks")

    def run(
        self,
        *,
        changed_files: tuple[str, ...] = (),
        artifact_store: ArtifactStore | None = None,
        work_item_id: str | None = None,
    ) -> tuple[CheckResult, ...]:
        results = tuple(
            _evaluate_definition(definition, changed_files)
            for definition in load_checks(self._repo / ".maxwell" / "checks")
        )
        if artifact_store is not None:
            if work_item_id is None:
                raise ValueError("work_item_id is required when persisting check results")
            artifact_store.put_text(
                kind=ArtifactKind.CHECK_RESULT,
                name="Maxwell local check results",
                text=json.dumps(
                    [result.model_dump(mode="json") for result in results],
                    indent=2,
                    sort_keys=True,
                ),
                work_item_id=work_item_id,
                media_type="application/json",
                metadata={"repo": str(self._repo), "changed_files": list(changed_files)},
            )
        return results


def _evaluate_definition(
    definition: CheckDefinition,
    changed_files: tuple[str, ...],
) -> CheckResult:
    if changed_files and not definition.applies_to_paths(changed_files):
        return CheckResult(
            check_id=definition.id,
            name=definition.name,
            severity=definition.severity,
            conclusion=CheckConclusion.SKIPPED,
            summary="No changed files matched this check.",
            changed_files=changed_files,
            metadata={"source": str(definition.source) if definition.source else None},
        )
    return CheckResult(
        check_id=definition.id,
        name=definition.name,
        severity=definition.severity,
        conclusion=CheckConclusion.PASS,
        summary="Check definition loaded and matched local inputs.",
        changed_files=changed_files,
        metadata={
            "source": str(definition.source) if definition.source else None,
            "model_tier": definition.model_tier.value,
            "trigger_events": list(definition.trigger.events),
            "execution": "definition-validation",
        },
    )
