#!/usr/bin/env python3
"""Reject new TODO/FIXME/XXX comments that don't reference a tracked issue.

Fleet standard: every open placeholder either points to GitHub issue #NNN or
links to an external tracker URL. This keeps tech debt visible rather than
buried.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_PATTERN = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b(?!.*(#\d+|https?://))", re.IGNORECASE)


def _check(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        try:
            with path.open(encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if _PATTERN.search(line):
                        violations.append(
                            f"{path}:{lineno}: {line.rstrip()} — add #<issue> or URL reference"
                        )
        except (OSError, UnicodeDecodeError):
            continue
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("conductor")])
    args = parser.parse_args()

    files: list[Path] = []
    for p in args.paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            files.extend(p.rglob("*.py"))

    violations = _check(files)
    if violations:
        print("\n".join(violations), file=sys.stderr)
        print(f"\n✗ {len(violations)} untracked TODO/FIXME comment(s)", file=sys.stderr)
        return 1
    print(f"✓ No untracked TODO/FIXME comments in {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
