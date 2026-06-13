"""Static contract checks for the bundled VS Code extension scaffold."""

from __future__ import annotations

import json
from pathlib import Path

EXTENSION_DIR = Path("extensions/vscode")


def test_vscode_extension_manifest_declares_supported_surface() -> None:
    manifest = json.loads((EXTENSION_DIR / "package.json").read_text(encoding="utf-8"))

    assert manifest["engines"]["vscode"] == "^1.80.0"
    assert manifest["main"] == "./out/extension.js"
    commands = {item["command"] for item in manifest["contributes"]["commands"]}
    assert "maxwell.submitTask" in commands
    assert "maxwell.askAboutSelection" in commands
    assert "maxwell.fixThisFile" in commands
    assert "maxwell.generateTests" in commands
    assert "maxwell.reviewDiff" in commands
    assert "maxwell.showCost" in commands


def test_vscode_extension_wires_daemon_api_contracts() -> None:
    source = (EXTENSION_DIR / "src" / "extension.ts").read_text(encoding="utf-8")

    assert "maxwell.submitTask" in source
    assert "maxwell.askAboutSelection" in source
    assert "maxwell.fixThisFile" in source
    assert "maxwell.generateTests" in source
    assert "maxwell.reviewDiff" in source
    assert "maxwell.showCost" in source
