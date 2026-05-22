"""Tests for ``scripts/check_noqa_ratchet.py`` (Phase 1.5).

The ratchet enforces a "no new tech debt" policy: ``# noqa: BLE001`` (bare
``except Exception``) and ``# noqa: C901`` (McCabe complexity) sites grandfather
existing debt but block additions. The script reads a baseline JSON and fails
when the current count exceeds it for any tracked rule.

These tests drive the implementation TDD-style — they fail until
``scripts/check_noqa_ratchet.py`` exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The script is not a package; load it via importlib so tests can call its
# public functions without invoking sys.argv parsing.
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_noqa_ratchet.py"


def _load_module() -> object:
    """Load the ratchet script as a module without running its main()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_noqa_ratchet", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_noqa_ratchet"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ratchet() -> object:
    return _load_module()


class TestCountNoqa:
    """``count_noqa(files, rules) -> dict[rule, int]``"""

    def test_counts_single_rule(self, tmp_path: Path, ratchet: object) -> None:
        f = tmp_path / "sample.py"
        f.write_text("import os\ntry:\n    x = 1\nexcept Exception:  # noqa: BLE001\n    pass\n")
        counts = ratchet.count_noqa([f], rules=("BLE001", "C901"))  # type: ignore[attr-defined]
        assert counts == {"BLE001": 1, "C901": 0}

    def test_counts_combined_noqa(self, tmp_path: Path, ratchet: object) -> None:
        # Combined suppression "BLE001, C901" on one line must count for both rules.
        f = tmp_path / "combined.py"
        f.write_text("def f():  # noqa: BLE001, C901\n    pass\n")
        counts = ratchet.count_noqa([f], rules=("BLE001", "C901"))  # type: ignore[attr-defined]
        assert counts == {"BLE001": 1, "C901": 1}

    def test_counts_bare_noqa_is_ignored(self, tmp_path: Path, ratchet: object) -> None:
        # A blanket suppression with no rule is its own bad practice; the ratchet
        # only tracks the rules it was asked about.
        f = tmp_path / "blanket.py"
        f.write_text("x = 1  # noqa\n")
        counts = ratchet.count_noqa([f], rules=("BLE001",))  # type: ignore[attr-defined]
        assert counts == {"BLE001": 0}

    def test_counts_across_files(self, tmp_path: Path, ratchet: object) -> None:
        for name, body in [
            ("a.py", "x = 1  # noqa: BLE001\n"),
            ("b.py", "y = 2  # noqa: BLE001\ny = 3  # noqa: BLE001\n"),
        ]:
            (tmp_path / name).write_text(body)
        counts = ratchet.count_noqa(  # type: ignore[attr-defined]
            sorted(tmp_path.glob("*.py")), rules=("BLE001",)
        )
        assert counts == {"BLE001": 3}

    def test_no_files_returns_zero_per_rule(self, ratchet: object) -> None:
        counts = ratchet.count_noqa([], rules=("BLE001", "C901"))  # type: ignore[attr-defined]
        assert counts == {"BLE001": 0, "C901": 0}


class TestRatchetVerdict:
    """``verdict(current, baseline) -> RatchetResult``"""

    def test_equal_is_ok(self, ratchet: object) -> None:
        result = ratchet.verdict(  # type: ignore[attr-defined]
            current={"BLE001": 95, "C901": 11},
            baseline={"BLE001": 95, "C901": 11},
        )
        assert result.ok
        assert result.violations == {}

    def test_decrease_is_ok(self, ratchet: object) -> None:
        # Burn-down: a PR that *removes* debt must always pass.
        result = ratchet.verdict(  # type: ignore[attr-defined]
            current={"BLE001": 90, "C901": 11},
            baseline={"BLE001": 95, "C901": 11},
        )
        assert result.ok
        assert result.improvements == {"BLE001": -5}

    def test_increase_is_violation(self, ratchet: object) -> None:
        result = ratchet.verdict(  # type: ignore[attr-defined]
            current={"BLE001": 96, "C901": 11},
            baseline={"BLE001": 95, "C901": 11},
        )
        assert not result.ok
        assert result.violations == {"BLE001": (95, 96)}

    def test_multiple_rule_increase(self, ratchet: object) -> None:
        result = ratchet.verdict(  # type: ignore[attr-defined]
            current={"BLE001": 97, "C901": 12},
            baseline={"BLE001": 95, "C901": 11},
        )
        assert not result.ok
        assert result.violations == {"BLE001": (95, 97), "C901": (11, 12)}

    def test_missing_baseline_key_treats_as_zero(self, ratchet: object) -> None:
        # Adding a new tracked rule mid-flight: baseline has no entry, current
        # has 1. That is a violation — the operator must explicitly add the
        # rule to baseline first.
        result = ratchet.verdict(  # type: ignore[attr-defined]
            current={"NEW001": 1},
            baseline={},
        )
        assert not result.ok
        assert result.violations == {"NEW001": (0, 1)}


class TestLoadBaseline:
    """``load_baseline(path) -> dict[str, int]``"""

    def test_loads_simple_baseline(self, tmp_path: Path, ratchet: object) -> None:
        p = tmp_path / "baseline.json"
        p.write_text(json.dumps({"BLE001": 95, "C901": 11}))
        assert ratchet.load_baseline(p) == {"BLE001": 95, "C901": 11}  # type: ignore[attr-defined]

    def test_missing_file_is_empty_dict(self, tmp_path: Path, ratchet: object) -> None:
        # New tracked-rules can be introduced by adding to the baseline file;
        # absence of the file is treated as "everything is at zero" so the
        # first run on a fresh checkout still works.
        assert ratchet.load_baseline(tmp_path / "absent.json") == {}  # type: ignore[attr-defined]


class TestMainExitCode:
    """End-to-end: ``main()`` returns 0 on OK, 1 on violation."""

    def test_main_returns_zero_when_at_baseline(
        self, tmp_path: Path, ratchet: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "src.py"
        src.write_text("x = 1  # noqa: BLE001\n")
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"BLE001": 1}))
        # main(argv) for testability — DRY with the CLI entrypoint.
        rc = ratchet.main(  # type: ignore[attr-defined]
            ["--baseline", str(baseline), "--rules", "BLE001", str(tmp_path)]
        )
        assert rc == 0

    def test_main_returns_one_when_above_baseline(self, tmp_path: Path, ratchet: object) -> None:
        src = tmp_path / "src.py"
        src.write_text("x = 1  # noqa: BLE001\ny = 2  # noqa: BLE001\n")
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps({"BLE001": 1}))
        rc = ratchet.main(  # type: ignore[attr-defined]
            ["--baseline", str(baseline), "--rules", "BLE001", str(tmp_path)]
        )
        assert rc == 1
