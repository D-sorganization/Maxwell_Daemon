#!/usr/bin/env python3
"""CI guard: ensure create_subprocess_shell only appears in _shell_default_runner.

Walks the repository finding *.py files (excluding itself and test files),
greps for `create_subprocess_shell` using Python's tokenizer so that mentions
in comments and string literals are not flagged, and fails with exit code 1 if
any occurrence is found outside the single allowed function
`_shell_default_runner` in `maxwell_daemon/hooks.py`.

Usage:
    python .github/scripts/check_subprocess_shell.py [repo_root]

If ``repo_root`` is not supplied it defaults to the directory two levels above
this script (i.e. the repository root when the script lives at
``.github/scripts/check_subprocess_shell.py``).
"""

from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

# The one file and function that is allowed to call create_subprocess_shell.
_ALLOWED_FILE = Path("maxwell_daemon") / "hooks.py"
_ALLOWED_FUNCTION = "_shell_default_runner"

# The target symbol we are guarding.
_TARGET = "create_subprocess_shell"

# Context window: we look at this many lines above a hit to find a function def.
_CONTEXT_LINES = 40

# Regex to locate the nearest enclosing def when given a list of source lines.
_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")


def _find_enclosing_function(lines: list[str], hit_lineno: int) -> str:
    """Return the name of the nearest ``def`` before ``hit_lineno`` (1-based)."""
    start = max(hit_lineno - 2, 0)
    end = max(hit_lineno - _CONTEXT_LINES - 1, -1)
    for idx in range(start, end, -1):
        m = _DEF_RE.match(lines[idx])
        if m:
            return m.group(1)
    return "<module-level>"


def _actual_uses(source: str) -> list[int]:
    """Return 1-based line numbers where ``create_subprocess_shell`` appears as a
    real NAME token (not inside a comment or string literal).
    """
    linenos: list[int] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok_type, tok_string, tok_start, _tok_end, _line in tokens:
            if tok_type == tokenize.NAME and tok_string == _TARGET:
                linenos.append(tok_start[0])
    except tokenize.TokenError:
        # Partial / unfinished file — fall back to simple grep so we don't
        # silently miss violations in malformed files.
        for lineno, line in enumerate(source.splitlines(), start=1):
            stripped = line.lstrip()
            if _TARGET in line and not stripped.startswith("#"):
                linenos.append(lineno)
    return linenos


def check(repo_root: Path) -> list[str]:
    """Return a list of violation strings (empty means clean)."""
    violations: list[str] = []

    for py_file in sorted(repo_root.rglob("*.py")):
        # Skip this script itself.
        if py_file.resolve() == Path(__file__).resolve():
            continue
        # Skip test files — tests are allowed to monkeypatch the symbol.
        rel = py_file.relative_to(repo_root)
        if rel.parts and rel.parts[0] in ("tests", "test"):
            continue
        if py_file.name.startswith("test_"):
            continue

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Fast skip: no mention at all.
        if _TARGET not in source:
            continue

        lines = source.splitlines()
        for lineno in _actual_uses(source):
            # Is this the single allowed location?
            if rel == _ALLOWED_FILE:
                enclosing = _find_enclosing_function(lines, lineno)
                if enclosing == _ALLOWED_FUNCTION:
                    continue  # The one permitted call site.

            violations.append(
                f"  {rel}:{lineno}: `{_TARGET}` found outside "
                f"`{_ALLOWED_FUNCTION}` in `{_ALLOWED_FILE}`"
            )

    return violations


def main() -> None:
    if len(sys.argv) > 1:
        repo_root = Path(sys.argv[1]).resolve()
    else:
        # Default: two levels up from .github/scripts/
        repo_root = Path(__file__).resolve().parent.parent.parent

    violations = check(repo_root)
    if violations:
        print(
            f"ERROR: `{_TARGET}` must only appear inside "
            f"`{_ALLOWED_FUNCTION}` in `{_ALLOWED_FILE}`.\n"
            "Violations found:"
        )
        for v in violations:
            print(v)
        print(
            f"\nIf you need shell semantics for a hook, set `shell: true` on the "
            f"HookSpec in YAML.  Do not add new `{_TARGET}` calls."
        )
        sys.exit(1)
    else:
        print(
            f"OK: `{_TARGET}` is confined to `{_ALLOWED_FUNCTION}` "
            f"in `{_ALLOWED_FILE}`."
        )
        sys.exit(0)


if __name__ == "__main__":
    main()
