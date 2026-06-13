#!/usr/bin/env python3
"""Fail if any tracked file matches a .gitignore pattern.

Repo hygiene guard (#983/#984/#985): development junk, runtime logs, build
output, and vendored dependencies must never be committed. Once a path is
added to ``.gitignore`` *and* removed from the index, this guard prevents it
from creeping back in. ``git`` itself is the source of truth — we ask it which
tracked paths it would otherwise ignore.
"""

from __future__ import annotations

import subprocess  # nosec B404 - argv-list git invocation, no shell
import sys


def _tracked_but_ignored() -> list[str]:
    # ``--cached`` lists files in the index; ``-i --exclude-standard`` filters
    # to those matching a gitignore rule. The intersection is exactly the set
    # of tracked-yet-ignored paths we want to reject.
    result = subprocess.run(  # nosec B603 B607 - fixed argv, git resolved from PATH
        ["git", "ls-files", "--cached", "-i", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main() -> int:
    violations = _tracked_but_ignored()
    if violations:
        print(
            "ERROR: the following tracked files match a .gitignore pattern.\n"
            "Run `git rm --cached <file>` to untrack them:\n",
            file=sys.stderr,
        )
        print("\n".join(f"  {v}" for v in violations), file=sys.stderr)
        print(f"\n{len(violations)} tracked-but-ignored file(s).", file=sys.stderr)
        return 1
    print("OK: no tracked files match .gitignore patterns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
