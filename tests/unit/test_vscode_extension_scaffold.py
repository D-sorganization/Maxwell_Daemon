"""Static contract checks for the bundled VS Code extension scaffold."""

from __future__ import annotations

import json
from pathlib import Path

EXTENSION_DIR = Path("extensions/conductor-vscode")


def test_vscode_extension_manifest_declares_supported_surface() -> None:
    manifest = json.loads((EXTENSION_DIR / "package.json").read_text(encoding="utf-8"))

    assert manifest["engines"]["vscode"] == "^1.80.0"
    assert manifest["main"] == "./extension.js"
    commands = {item["command"] for item in manifest["contributes"]["commands"]}
    assert "maxwellConductor.dispatchIssue" in commands
    assert "maxwellConductor.openPrDiff" in commands
    assert "maxwellConductor.streamLogs" in commands
    assert "maxwellConductor.agents" in manifest["activationEvents"][0]


def test_vscode_extension_wires_daemon_api_contracts() -> None:
    source = (EXTENSION_DIR / "extension.js").read_text(encoding="utf-8")

    assert "/api/v1/backends" in source
    assert "/api/v1/tasks?limit=50" in source
    assert "/api/v1/fleet" in source
    assert "/api/v1/issues/dispatch" in source
    assert "createTerminal" in source
    assert "parseIssueRef" in source
