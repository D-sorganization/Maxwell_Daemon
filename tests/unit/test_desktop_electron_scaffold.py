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
    assert "updateTray" in main
    assert "desktop:updateTrayStatus" in main
    assert "new Notification" in main
    assert "globalShortcut.register" in main
    assert "autoUpdater.checkForUpdates" in main


def test_electron_auto_updater_streams_lifecycle_to_renderer() -> None:
    main = (APP_DIR / "main.js").read_text(encoding="utf-8")
    preload = (APP_DIR / "preload.js").read_text(encoding="utf-8")
    renderer = (APP_DIR / "renderer" / "app.js").read_text(encoding="utf-8")
    html = (APP_DIR / "renderer" / "index.html").read_text(encoding="utf-8")

    assert "function registerAutoUpdaterEvents" in main
    assert 'autoUpdater.on("update-downloaded"' in main
    assert 'mainWindow?.webContents.send("desktop:updateStatus"' in main
    assert "autoUpdater.quitAndInstall(false, true)" in main
    assert "desktop:installUpdate" in preload
    assert "onUpdateStatus(callback)" in preload
    assert "function renderUpdateStatus" in renderer
    assert "install-update" in html
    assert "update-state" in html


def test_electron_main_process_wires_taskbar_status() -> None:
    main = (APP_DIR / "main.js").read_text(encoding="utf-8")

    assert "function updateTaskbar" in main
    assert "app.setAppUserModelId" in main
    assert "app.setBadgeCount(activeTasks)" in main
    assert 'mainWindow.setProgressBar(2, { mode: "indeterminate" })' in main
    assert 'mainWindow.setProgressBar(1, { mode: "error" })' in main
    assert "mainWindow.setProgressBar(-1)" in main


def test_renderer_tracks_system_light_and_dark_theme() -> None:
    styles = (APP_DIR / "renderer" / "styles.css").read_text(encoding="utf-8")

    assert "color-scheme: light dark" in styles
    assert "@media (prefers-color-scheme: light)" in styles
    assert "--bg: #f6f7f9" in styles
    assert "--panel: #ffffff" in styles


def test_renderer_wires_daemon_api_offline_cache_and_drag_drop() -> None:
    preload = (APP_DIR / "preload.js").read_text(encoding="utf-8")
    renderer = (APP_DIR / "renderer" / "app.js").read_text(encoding="utf-8")

    assert "/api/v1/tasks?limit=100" in preload
    assert "/api/v1/fleet" in preload
    assert "/api/v1/issues/dispatch" in preload
    assert "updateTrayStatus" in preload
    assert "localStorage.setItem(cacheKey" in preload
    assert "cachedSnapshot" in renderer
    assert "status-strip" in renderer
    assert "dataTransfer.files" in renderer


def test_renderer_uses_event_based_notifications() -> None:
    renderer = (APP_DIR / "renderer" / "app.js").read_text(encoding="utf-8")

    assert "maxwell.desktop.notificationState" in renderer
    assert "function notifyForSnapshot" in renderer
    assert "previous.tasks[key] === task.status" in renderer
    assert "Delegate needs attention" in renderer
    assert "Maxwell-Daemon offline" in renderer
    assert "`${running} task(s) running`" not in renderer


def test_renderer_command_palette_executes_common_desktop_actions() -> None:
    renderer = (APP_DIR / "renderer" / "app.js").read_text(encoding="utf-8")
    html = (APP_DIR / "renderer" / "index.html").read_text(encoding="utf-8")

    assert "function openCommandPalette" in renderer
    assert "async function runCommand" in renderer
    assert 'command === "refresh" || command === "sync"' in renderer
    assert 'command === "dispatch"' in renderer
    assert 'command === "updates" || command === "update"' in renderer
    assert "window.maxwellDesktop.onCommandPalette(openCommandPalette)" in renderer
    assert 'id="command-form"' in html
    assert 'data-command="refresh"' in html
    assert 'data-command="dispatch"' in html
    assert 'data-command="updates"' in html
