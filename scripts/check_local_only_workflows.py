#!/usr/bin/env python3
"""Fail when workflows use hosted runners outside the approved fast lane."""

from __future__ import annotations

from pathlib import Path

WORKFLOW_DIR = Path(".github") / "workflows"
BANNED = (
    "ubuntu-latest",
    "windows-latest",
    "macos-latest",
    "force_cloud",
    "mode=cloud",
    "Routing to GitHub-hosted",
    "using GitHub-hosted",
    "runner=ubuntu-latest",
    "runner=windows-latest",
    "runner=macos-latest",
)

# Files allowlisted from the hosted-runner scan. The tripwire workflow
# intentionally runs on a hosted runner; everything else must stay local.
LEGACY_HOSTED_RUNNER_ALLOWLIST = {
    ".github/workflows/local-only-runner-guard.yml",
}
HYBRID_WORKFLOW_ALLOWLIST = {
    ".github/workflows/ci.yml",
}


def main() -> int:
    failures: list[str] = []
    if not WORKFLOW_DIR.exists():
        return 0

    for path in sorted(WORKFLOW_DIR.rglob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue

        if path.as_posix() in LEGACY_HOSTED_RUNNER_ALLOWLIST:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig")
        hybrid_workflow = path.as_posix() in HYBRID_WORKFLOW_ALLOWLIST
        for line_number, line in enumerate(text.splitlines(), start=1):
            for token in BANNED:
                if token in line:
                    if hybrid_workflow and token in {"ubuntu-latest", "runner=ubuntu-latest"}:
                        continue
                    failures.append(f"{path}:{line_number}: banned hosted-runner token {token!r}")

    if failures:
        print("GitHub-hosted runner routing is forbidden. Use local self-hosted runners only.")
        print("\n".join(failures))
        return 1

    print("Workflow runner routing follows the approved hybrid policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
