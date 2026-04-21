#!/usr/bin/env python3
"""Enforce a coverage floor that only ratchets upward.

Reads coverage from coverage.xml (cobertura) and compares it to the stored floor
in scripts/config/coverage_floor.json. Fails if coverage drops.

Philosophy (fleet standard): never lower the floor. If coverage drops, add
tests — don't relax the gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

FLOOR_FILE = Path("scripts/config/coverage_floor.json")
TOLERANCE = 0.0  # zero drop allowed


def _read_xml_coverage(xml_path: Path) -> float:
    tree = ET.parse(xml_path)
    root = tree.getroot()
    # cobertura has line-rate on the root
    rate = root.get("line-rate")
    if rate is None:
        raise RuntimeError(f"No line-rate attribute in {xml_path}")
    return float(rate) * 100.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xml", default="coverage.xml", type=Path)
    parser.add_argument(
        "--update", action="store_true", help="Ratchet floor up to current"
    )
    args = parser.parse_args()

    if not args.xml.exists():
        print(f"ERROR: Coverage report not found at {args.xml}", file=sys.stderr)
        return 1

    current = _read_xml_coverage(args.xml)

    if not FLOOR_FILE.exists():
        FLOOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        FLOOR_FILE.write_text(json.dumps({"floor_percent": current}, indent=2) + "\n")
        print(f"OK: Initialized coverage floor at {current:.2f}%")
        return 0

    floor = json.loads(FLOOR_FILE.read_text())["floor_percent"]
    print(f"Current coverage: {current:.2f}%  Floor: {floor:.2f}%")

    if current + TOLERANCE < floor:
        print(
            f"ERROR: Coverage dropped by {floor - current:.2f}%. Add tests; do not lower the floor.",
            file=sys.stderr,
        )
        return 1

    if args.update and current > floor:
        FLOOR_FILE.write_text(
            json.dumps({"floor_percent": round(current, 2)}, indent=2) + "\n"
        )
        print(f"OK: Ratcheted floor upward: {floor:.2f}% -> {current:.2f}%")
    else:
        print("OK: Coverage floor met.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
