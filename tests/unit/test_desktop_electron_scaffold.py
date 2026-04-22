"""Static checks for the Electron desktop scaffold."""

from __future__ import annotations

import json
from pathlib import Path

APP_DIR = Path("apps/desktop-electron")


def test_electron_manifest_declares_native_distribution_targets() -> None:
    manifest = json.loads((APP_DIR / "package.json").read_text(encoding="utf-8"))

    assert manifest["main"] == "main.js"
    assert "electron" in manifest["devDependencies"]
    assert "electron-updater" in manifest["dependencies"]
    assert manifest["build"]["mac"]["target"] == ["dmg"]
    assert manifest["build"]["win"]["target"] == ["msi"]
    assert "AppImage" in manifest["build"]["linux"]["target"]
    assert "snap" in manifest["build"]["linux"]["target"]


def test_electron_main_process_wires_native_desktop_features() -> None:
    main = (APP_DIR / "main.js").read_text(encoding="utf-8")

    assert "new Tray" in main
    assert "new Notification" in main
    assert "globalShortcut.register" in main
    assert "autoUpdater.checkForUpdates" in main


def test_renderer_wires_daemon_api_offline_cache_and_drag_drop() -> None:
    preload = (APP_DIR / "preload.js").read_text(encoding="utf-8")
    renderer = (APP_DIR / "renderer" / "app.js").read_text(encoding="utf-8")

    assert "/api/v1/tasks?limit=100" in preload
    assert "/api/v1/fleet" in preload
    assert "/api/v1/issues/dispatch" in preload
    assert "localStorage.setItem(cacheKey" in preload
    assert "cachedSnapshot" in renderer
    assert "dataTransfer.files" in renderer
