"""Shrink-only ratchet for the file-size budget (#987).

An exception entry may pin a per-file ``max_lines`` ceiling above the global
budget. The file is tolerated while it is decomposed, but it must never grow
past that ceiling — growth fails so a god module cannot regrow.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_file_size_budget.py"

_spec = importlib.util.spec_from_file_location("check_file_size_budget", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _write_lines(path: Path, n: int) -> None:
    path.write_text("\n".join(f"# line {i}" for i in range(n)) + "\n", encoding="utf-8")


def _config(ceiling: int | None) -> dict:
    exc: dict = {
        "path": "pkg/big.py",
        "expires_on": "2999-12-31",
        "owner": "tester",
        "reason": "test",
    }
    if ceiling is not None:
        exc["max_lines"] = ceiling
    return {"max_lines": 100, "exceptions": [exc]}


def test_file_within_global_budget_passes(tmp_path: Path) -> None:
    f = tmp_path / "pkg" / "small.py"
    f.parent.mkdir(parents=True)
    _write_lines(f, 50)
    violations = _mod._check([f], tmp_path, _config(ceiling=None))
    assert violations == []


def test_over_budget_at_or_below_ceiling_warns_not_fails(tmp_path: Path) -> None:
    f = tmp_path / "pkg" / "big.py"
    f.parent.mkdir(parents=True)
    _write_lines(f, 500)  # over global budget (100), at/below ceiling (500)
    violations = _mod._check([f], tmp_path, _config(ceiling=500))
    assert violations == []


def test_growth_past_ceiling_fails(tmp_path: Path) -> None:
    f = tmp_path / "pkg" / "big.py"
    f.parent.mkdir(parents=True)
    _write_lines(f, 600)  # over the 500 ceiling -> must fail
    violations = _mod._check([f], tmp_path, _config(ceiling=500))
    assert len(violations) == 1
    assert "shrink-only ratchet ceiling" in violations[0]


def test_open_ended_exception_without_ceiling_still_warns(tmp_path: Path) -> None:
    f = tmp_path / "pkg" / "big.py"
    f.parent.mkdir(parents=True)
    _write_lines(f, 5000)  # huge, but no ceiling -> legacy warn-only behavior
    violations = _mod._check([f], tmp_path, _config(ceiling=None))
    assert violations == []


def test_runner_py_has_shrink_only_ceiling() -> None:
    """The real config pins runner.py to a shrink-only ceiling (#987)."""
    config = json.loads(
        (_REPO_ROOT / "scripts" / "config" / "file_size_budget.json").read_text(encoding="utf-8")
    )
    runner = next(e for e in config["exceptions"] if e["path"] == "maxwell_daemon/daemon/runner.py")
    assert "max_lines" in runner
    # The live file must not already exceed its own ratchet ceiling.
    runner_path = _REPO_ROOT / "maxwell_daemon" / "daemon" / "runner.py"
    actual = sum(1 for _ in runner_path.open(encoding="utf-8"))
    assert actual <= runner["max_lines"], (
        f"runner.py is {actual} lines, over its ratchet ceiling {runner['max_lines']}"
    )
