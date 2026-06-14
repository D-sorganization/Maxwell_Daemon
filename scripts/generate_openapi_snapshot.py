#!/usr/bin/env python3
"""Generate or verify the checked-in OpenAPI contract snapshot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_OUTPUT = Path("docs/reference/openapi.json")


class _SchemaDaemon:
    """Minimal daemon stand-in needed for route registration and schema export."""

    def __init__(self, config: Any) -> None:
        self._config = config


def _schema_config() -> Any:
    from maxwell_daemon.config import MaxwellDaemonConfig

    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "openapi-snapshot"}},
            "agent": {"default_backend": "primary"},
        }
    )


def generate_schema() -> dict[str, Any]:
    """Return the live FastAPI OpenAPI schema for the stable app surface."""
    from maxwell_daemon.api import create_app

    app = create_app(_SchemaDaemon(_schema_config()))  # type: ignore[arg-type]
    schema = app.openapi()
    if not isinstance(schema, dict):
        raise TypeError("FastAPI OpenAPI generation did not return a JSON object")
    return schema


def render_schema(schema: dict[str, Any]) -> str:
    """Render the schema deterministically for snapshot diffs and release assets."""
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_snapshot(path: Path = DEFAULT_OUTPUT) -> None:
    path.write_text(render_schema(generate_schema()), encoding="utf-8", newline="\n")


def check_snapshot(path: Path = DEFAULT_OUTPUT) -> bool:
    expected = render_schema(generate_schema())
    return path.read_text(encoding="utf-8") == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail if the snapshot is stale")
    args = parser.parse_args()

    if args.check:
        if check_snapshot(args.output):
            print(f"{args.output} matches the live OpenAPI schema.")
            return 0
        print(f"{args.output} is stale. Run scripts/generate_openapi_snapshot.py.")
        return 1

    write_snapshot(args.output)
    print(f"Wrote {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
