"""Workflow contract: Rust jobs must not share ~/.rustup or ~/.cargo (RM#2021).

Persistent self-hosted runners run jobs concurrently. A shared RUSTUP_HOME means
one job's toolchain install can delete binaries out from under another job, so
every job that installs or uses Rust needs workspace-local homes.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(".github/workflows")
RUST_PATTERN = re.compile(r"rust-toolchain|rustup|\bcargo\b|maturin|tauri", re.IGNORECASE)
WORKSPACE_PREFIX = "${{ github.workspace }}/"


def _workflows() -> list[Path]:
    return sorted([*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")])


def _rust_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    """Return (workflow name, job id, job) for every job whose steps touch Rust."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in _workflows():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_id, job in (data.get("jobs") or {}).items():
            steps = job.get("steps") or []
            text = "\n".join(
                str(step.get("uses", "")) + "\n" + str(step.get("run", ""))
                for step in steps
                if isinstance(step, dict)
            )
            if RUST_PATTERN.search(text):
                found.append((path.name, job_id, job))
    return found


def _is_fleet_capable(job: dict[str, Any]) -> bool:
    runs_on = str(job.get("runs-on", ""))
    return "pick-runner" in runs_on or "d-sorg-fleet" in runs_on or "self-hosted" in runs_on


def _fleet_rust_jobs() -> list[tuple[str, str, dict[str, Any]]]:
    return [entry for entry in _rust_jobs() if _is_fleet_capable(entry[2])]


def test_rust_jobs_are_discovered() -> None:
    # Guards the discovery logic itself: ide-extensions runs cargo today.
    assert ("ci.yml", "ide-extensions") in {(w, j) for w, j, _ in _rust_jobs()}


@pytest.mark.parametrize(
    ("workflow", "job_id", "job"),
    _fleet_rust_jobs(),
    ids=lambda value: value if isinstance(value, str) else "",
)
@pytest.mark.parametrize("variable", ["RUSTUP_HOME", "CARGO_HOME"])
def test_fleet_rust_job_uses_workspace_local_home(
    workflow: str, job_id: str, job: dict[str, Any], variable: str
) -> None:
    env = job.get("env") or {}
    value = str(env.get(variable, ""))
    assert value.startswith(WORKSPACE_PREFIX), (
        f"{workflow}:{job_id} must set job-level {variable} under "
        f"{WORKSPACE_PREFIX} (not runner.temp, not the shared home)"
    )


@pytest.mark.parametrize(
    ("workflow", "job_id", "job"),
    _fleet_rust_jobs(),
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_fleet_rust_job_puts_cargo_bin_first_on_path(
    workflow: str, job_id: str, job: dict[str, Any]
) -> None:
    steps = [s for s in job.get("steps", []) if isinstance(s, dict)]
    rust_index = next(
        i
        for i, step in enumerate(steps)
        if RUST_PATTERN.search(str(step.get("uses", "")) + str(step.get("run", "")))
    )
    prepend_indexes = [
        i
        for i, step in enumerate(steps)
        if "$CARGO_HOME/bin" in str(step.get("run", ""))
        and "GITHUB_PATH" in str(step.get("run", ""))
    ]
    assert prepend_indexes and prepend_indexes[0] <= rust_index, (
        f"{workflow}:{job_id} must add $CARGO_HOME/bin to GITHUB_PATH before its first Rust step"
    )
