"""Deployment guide documentation contract checks."""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path("docs/operations/deployment.md")
COVERAGE_DOC = Path("docs/community/documentation-coverage.md")


def test_deployment_guide_is_in_mkdocs_nav() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "Deployment guide: operations/deployment.md" in mkdocs


def test_deployment_guide_covers_launcher_first_run_path() -> None:
    doc = DOC.read_text(encoding="utf-8")

    for required in (
        "Launch-Maxwell.bat",
        "Launch-Maxwell.command",
        "Launch-Maxwell.sh",
        "python -m maxwell_daemon.launcher --repo-root . --port 8080",
        "Create a local `.venv`",
        "`maxwell-daemon doctor`",
        "`maxwell-daemon serve`",
        "GET /health",
        "GET /docs",
    ):
        assert required in doc


def test_deployment_guide_includes_timed_fresh_deploy_proof_under_30_minutes() -> None:
    doc = DOC.read_text(encoding="utf-8")

    assert "### Timed fresh deploy proof" in doc
    assert "same launcher code path that `Launch-Maxwell.bat` uses" in doc
    assert "isolated `HOME` / `USERPROFILE` / `APPDATA` / `LOCALAPPDATA`" in doc

    match = re.search(r"Measured ready time \| `([0-9]+(?:\.[0-9]+)?) seconds` \|", doc)
    assert match is not None
    assert float(match.group(1)) < 30 * 60


def test_documentation_coverage_marks_deployment_guide_shipped() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")

    assert (
        "| Deployment guide | `operations/deployment.md`, `ansible.md`, `webhooks.md`, "
        "`tailscale.md`, `tests/unit/test_deployment_docs.py` | Shipped | Keep the "
        "launcher-based timed deploy proof current when bootstrap steps change. |"
    ) in coverage
