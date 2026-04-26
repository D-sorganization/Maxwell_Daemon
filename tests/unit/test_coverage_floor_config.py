"""Regression guard for the coverage ratchet floor."""

from __future__ import annotations

import json
from pathlib import Path


def test_coverage_floor_preserves_prior_ratchet() -> None:
    floor_path = Path("scripts/config/coverage_floor.json")
    floor = json.loads(floor_path.read_text())["floor_percent"]

    assert floor >= 85.10
