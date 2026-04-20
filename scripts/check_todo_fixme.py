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
import tokenize
from pathlib import Path

_PATTERN = re.compile(r"\b(TODO|FIXME|XXX|HACK)\b(?!.*(#\d+|https?://))", re.IGNORECASE)


def _check(paths: list[Path]) -> list[str]:
    violations: list[str] = []
    for path in paths:
        try:
            with tokenize.open(path) as f:
                for token in tokenize.generate_tokens(f.readline):
                    if token.type != tokenize.COMMENT or not _PATTERN.search(token.string):
                        continue
                    violations.append(
                        f"{path}:{token.start[0]}: {token.string.rstrip()} - add #<issue> or URL reference"
                    )
        except (OSError, tokenize.TokenError, UnicodeDecodeError):
            continue
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("maxwell-daemon")])
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
        print(f"\nERROR: {len(violations)} untracked TODO/FIXME comment(s)", file=sys.stderr)
        return 1
    print(f"OK: No untracked TODO/FIXME comments in {len(files)} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
