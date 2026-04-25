#!/usr/bin/env python3
"""Enforce per-file line-count budgets with dated, owned exceptions.

Adapted from fleet standards: large files resist change, so we cap them. Any file
that needs an exemption must declare an owner, a reason, and an expiry — no open-ended
exceptions.

Usage:
    python scripts/check_file_size_budget.py                  # check working tree
    python scripts/check_file_size_budget.py --diff origin/main  # check only changed files
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

CONFIG_PATH = Path("scripts/config/file_size_budget.json")


def _load_config(repo_root: Path) -> dict[str, Any]:
    with (repo_root / CONFIG_PATH).open(encoding="utf-8") as f:
        return dict(json.load(f))


def _exception_active(exc: dict[str, Any]) -> bool:
    expires = exc.get("expires_on")
    if not expires:
        return False
    return date.today() <= date.fromisoformat(expires)


def _exception_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        exc["path"]: exc
        for exc in config.get("exceptions", [])
        if _exception_active(exc)
    }


def _run_git(args: list[str], repo_root: Path) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "git failed")
    return r.stdout


def _changed_python_files(repo_root: Path, base_ref: str) -> list[Path]:
    diff = _run_git(["diff", "--name-only", f"{base_ref}...HEAD", "--"], repo_root)
    return [
        repo_root / p
        for p in diff.splitlines()
        if p.endswith(".py") and (repo_root / p).exists()
    ]


def _all_python_files(repo_root: Path) -> list[Path]:
    return [p for p in (repo_root / "maxwell-daemon").rglob("*.py") if p.is_file()]


def _check(paths: list[Path], repo_root: Path, config: dict[str, Any]) -> list[str]:
    max_lines = int(config["max_lines"])
    exc_map = _exception_map(config)
    violations: list[str] = []
    for path in paths:
        rel = path.relative_to(repo_root).as_posix()
        with path.open(encoding="utf-8") as f:
            n = sum(1 for _ in f)
        if n <= max_lines:
            continue
        if rel in exc_map:
            exc = exc_map[rel]
            print(
                f"⚠ {rel} ({n} lines) exempt until {exc['expires_on']} "
                f"- owner: {exc['owner']}, reason: {exc['reason']}"
            )
            continue
        violations.append(
            f"ERROR: {rel} has {n} lines (budget: {max_lines}). "
            f"Split the module or add a dated exception to {CONFIG_PATH}."
        )
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", help="Only check files changed since this ref")
    parser.add_argument("--repo-root", default=".", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    config = _load_config(repo_root)

    paths = (
        _changed_python_files(repo_root, args.diff)
        if args.diff
        else _all_python_files(repo_root)
    )
    if not paths:
        print("No Python files to check.")
        return 0

    violations = _check(paths, repo_root, config)
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"OK: All {len(paths)} file(s) within budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
