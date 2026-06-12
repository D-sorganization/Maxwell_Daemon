"""Regression guard for the coverage ratchet floor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_coverage_floor_preserves_prior_ratchet() -> None:
    floor_path = Path("scripts/config/coverage_floor.json")
    floor = json.loads(floor_path.read_text())["floor_percent"]

    assert floor >= 85.10


def test_pytest_cov_fail_under_matches_phase_one_gate() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]

    assert "--cov-fail-under=80.0" in addopts
