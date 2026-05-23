#!/usr/bin/env python3
"""Block additions of grandfathered noqa debt.

Phase 1.5 of the production epic (#896): BLE001 (bare ``except Exception``)
and C901 (McCabe complexity) inline suppressions are grandfathered. This
script reads a baseline JSON recording the *current* count per rule and
refuses any PR that *increases* that count for any tracked rule.

Decreases are always welcome — the script reports them so reviewers see
burn-down progress on every PR.

Design (DRY/LoD):

* The script is decomposed into pure helpers (``count_noqa``, ``verdict``,
  ``load_baseline``) so unit tests cover every code path without spawning
  subprocesses.
* ``main(argv)`` accepts an explicit ``argv`` list so the test suite can
  exercise the CLI without ``monkeypatch.setattr(sys, "argv", ...)``.
* The verdict object is a frozen dataclass — handlers read its public
  fields and never mutate it.

Usage:
    python scripts/check_noqa_ratchet.py                                # check repo
    python scripts/check_noqa_ratchet.py --baseline path.json maxwell_daemon
    python scripts/check_noqa_ratchet.py --update-baseline              # rewrite
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

DEFAULT_BASELINE: Final[Path] = Path("scripts/config/noqa_baseline.json")
DEFAULT_RULES: Final[tuple[str, ...]] = ("BLE001", "C901")
DEFAULT_PATHS: Final[tuple[str, ...]] = ("maxwell_daemon",)

# Matches ruff's per-line suppression syntax (single or comma-list).
# Capture everything after the colon up to end-of-comment, then split on
# whitespace/commas. Case-insensitive because ruff accepts either case.
_NOQA_RE: Final[re.Pattern[str]] = re.compile(r"#\s*noqa\s*:\s*([A-Za-z0-9, ]+)")


@dataclass(frozen=True, slots=True)
class RatchetResult:
    """Verdict of comparing ``current`` counts to ``baseline``.

    ``violations[rule] = (baseline_count, current_count)`` for each rule
    where current > baseline.
    ``improvements[rule] = (current - baseline)`` (negative) for each rule
    that went down.
    """

    ok: bool
    violations: dict[str, tuple[int, int]] = field(default_factory=dict)
    improvements: dict[str, int] = field(default_factory=dict)


# ── Pure helpers (unit-tested directly) ──────────────────────────────────────


def count_noqa(files: Iterable[Path], *, rules: Sequence[str]) -> dict[str, int]:
    """Count occurrences of ``# noqa: <rule>`` per ``rule`` across ``files``.

    Combined directives like ``# noqa: BLE001, C901`` count once for each
    listed rule.
    """
    counts: dict[str, int] = dict.fromkeys(rules, 0)
    rule_set = {rule.upper() for rule in rules}
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _NOQA_RE.finditer(text):
            listed = {tok.strip().upper() for tok in match.group(1).split(",") if tok.strip()}
            for rule in listed & rule_set:
                # Map back to the canonical casing the caller asked for.
                canonical = next(r for r in rules if r.upper() == rule)
                counts[canonical] += 1
    return counts


def verdict(*, current: dict[str, int], baseline: dict[str, int]) -> RatchetResult:
    """Compare ``current`` to ``baseline``; return a frozen verdict."""
    violations: dict[str, tuple[int, int]] = {}
    improvements: dict[str, int] = {}
    for rule, current_count in current.items():
        baseline_count = baseline.get(rule, 0)
        if current_count > baseline_count:
            violations[rule] = (baseline_count, current_count)
        elif current_count < baseline_count:
            improvements[rule] = current_count - baseline_count
    return RatchetResult(ok=not violations, violations=violations, improvements=improvements)


def load_baseline(path: Path) -> dict[str, int]:
    """Read the baseline JSON; missing file ⇒ empty mapping (everything at zero)."""
    if not path.is_file():
        return {}
    return {str(k): int(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


# ── CLI plumbing ─────────────────────────────────────────────────────────────


def _iter_py_files(roots: Sequence[Path]) -> list[Path]:
    """Expand directories to all ``*.py`` files under them; pass through files."""
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
        elif root.is_dir():
            out.extend(sorted(root.rglob("*.py")))
    return out


def _format_violation(rule: str, baseline_count: int, current_count: int) -> str:
    delta = current_count - baseline_count
    return (
        f"  ✗ {rule}: {current_count} (baseline {baseline_count}, +{delta}) "
        f"— remove the new suppression or burn down existing ones first"
    )


def _format_improvement(rule: str, delta: int) -> str:
    return f"  ✓ {rule}: improved by {-delta} (good — thank you)"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=list(DEFAULT_PATHS),
        help=f"Files/dirs to scan (default: {' '.join(DEFAULT_PATHS)})",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"Baseline JSON path (default: {DEFAULT_BASELINE})",
    )
    parser.add_argument(
        "--rules",
        default=",".join(DEFAULT_RULES),
        help=f"Comma-separated rules to track (default: {','.join(DEFAULT_RULES)})",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Rewrite the baseline to match the current counts (use sparingly).",
    )
    args = parser.parse_args(argv)

    rules = tuple(r.strip() for r in args.rules.split(",") if r.strip())
    files = _iter_py_files([Path(p) for p in args.paths])
    current = count_noqa(files, rules=rules)

    if args.update_baseline:
        args.baseline.parent.mkdir(parents=True, exist_ok=True)
        args.baseline.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print(f"updated baseline at {args.baseline}: {current}")
        return 0

    baseline = load_baseline(args.baseline)
    result = verdict(current=current, baseline=baseline)

    if result.improvements:
        print("noqa burn-down:")
        for rule, delta in sorted(result.improvements.items()):
            print(_format_improvement(rule, delta))

    if not result.ok:
        print("noqa ratchet failed — new debt detected:")
        for rule, (baseline_count, current_count) in sorted(result.violations.items()):
            print(_format_violation(rule, baseline_count, current_count))
        print(
            "\nIf the new suppression is unavoidable, raise the baseline with:\n"
            f"  python {sys.argv[0]} --update-baseline\n"
            "and document why in your PR description."
        )
        return 1

    print(f"noqa ratchet OK — counts at or below baseline: {current}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
