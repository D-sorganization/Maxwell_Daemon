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


def test_coverage_floor_is_single_sourced() -> None:
    """The coverage floor is single-sourced in coverage_floor.json (#993).

    A second ``--cov-fail-under`` in pytest addopts would be a competing, weaker
    floor that drifts from the ratchet job, so it must not be present.
    """
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    addopts = pyproject["tool"]["pytest"]["ini_options"]["addopts"]

    assert "--cov-fail-under" not in addopts
    # The authoritative ratchet floor still exists and is enforced in CI.
    assert Path("scripts/config/coverage_floor.json").is_file()
