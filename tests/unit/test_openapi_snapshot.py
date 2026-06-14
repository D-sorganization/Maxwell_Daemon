"""Producer-owned OpenAPI snapshot contract checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "generate_openapi_snapshot.py"
DEFAULT_OUTPUT = Path("docs/reference/openapi.json")


RUNNER_DASHBOARD_CONTRACT_PATHS = {
    "/api/connection-profile",
    "/api/dispatch",
    "/api/status",
    "/api/tasks",
    "/api/tasks/{task_id}",
    "/api/version",
    "/api/v2/status",
}

RUNNER_DASHBOARD_CONTRACT_SCHEMAS = {
    "ConnectionProfile",
    "DispatchRequest",
    "DispatchResponse",
    "StatusResponse",
    "StatusV2Response",
    "TaskDetail",
    "TaskListResponse",
    "VersionResponse",
}


def _snapshot() -> dict[str, object]:
    return json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))


def test_checked_in_openapi_snapshot_matches_live_schema() -> None:
    result = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH), "--check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_snapshot_contains_runner_dashboard_contract_paths() -> None:
    snapshot = _snapshot()
    paths = snapshot["paths"]
    assert isinstance(paths, dict)

    assert set(paths) >= RUNNER_DASHBOARD_CONTRACT_PATHS


def test_snapshot_contains_runner_dashboard_contract_schemas() -> None:
    snapshot = _snapshot()
    components = snapshot["components"]
    assert isinstance(components, dict)
    schemas = components["schemas"]
    assert isinstance(schemas, dict)

    assert set(schemas) >= RUNNER_DASHBOARD_CONTRACT_SCHEMAS


def test_release_uploads_openapi_snapshot_as_artifact() -> None:
    release_workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    assert isinstance(release_workflow, dict)
    create_release = release_workflow["jobs"]["build-and-publish"]["steps"][-1]

    assert "docs/reference/openapi.json" in create_release["with"]["files"]


def test_release_sbom_uses_supported_cyclonedx_cli_flag() -> None:
    release_workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    assert isinstance(release_workflow, dict)
    steps = release_workflow["jobs"]["build-and-publish"]["steps"]
    sbom_steps = [step for step in steps if step.get("name") == "Generate SBOM (CycloneDX)"]
    assert len(sbom_steps) == 1

    sbom_command = sbom_steps[0]["run"]
    assert "cyclonedx-py environment --output-file dist/sbom.json" in sbom_command
    assert "--outfile" not in sbom_command


def test_release_sigstore_action_uses_job_python() -> None:
    release_workflow = yaml.safe_load(Path(".github/workflows/release.yml").read_text())
    assert isinstance(release_workflow, dict)
    steps = release_workflow["jobs"]["build-and-publish"]["steps"]
    sigstore_steps = [
        step for step in steps if step.get("name") == "Sign distributions with Sigstore"
    ]
    assert len(sigstore_steps) == 1

    # v3.4.0 bootstraps its own Python 3.14 env and currently hits a cffi
    # resolver conflict before signing; v3.0.1 uses this job's pinned 3.12.
    assert sigstore_steps[0]["uses"] == "sigstore/gh-action-sigstore-python@v3.0.1"
