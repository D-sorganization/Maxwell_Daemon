"""This repo's real workflows never run fork PR code on the self-hosted fleet.

Governing issue: D-sorganization/Repository_Management#1989.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import fork_pr_runner_guard as guard

pytestmark = pytest.mark.unit

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def test_repository_workflows_have_no_fork_pr_fleet_violations() -> None:
    assert WORKFLOWS_DIR.is_dir(), f"Expected workflows at {WORKFLOWS_DIR}"
    assert guard.find_violations(WORKFLOWS_DIR) == []
