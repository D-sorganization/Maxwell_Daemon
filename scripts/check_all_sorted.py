"""Check all __all__ lists in maxwell_daemon for RUF022 compliance."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_sorted(names: list[str]) -> bool:
    return names == sorted(names, key=str.lower)


def main() -> int:
    errors = 0
    for path in Path("maxwell_daemon").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, ast.List):
                            names = []
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                    names.append(elt.value)
                            if names and not _is_sorted(names):
                                print(f"UNSORTED: {path}")
                                for i, (a, b) in enumerate(zip(names, sorted(names, key=str.lower))):
                                    if a != b:
                                        print(f"  position {i}: got {a!r}, expected {b!r}")
                                        break
                                errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())