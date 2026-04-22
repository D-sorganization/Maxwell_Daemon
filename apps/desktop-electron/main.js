"use strict";

const { app, BrowserWindow, Menu, Notification, Tray, globalShortcut, ipcMain, nativeImage } = require("electron");
const { autoUpdater } = require("electron-updater");
const { performance } = require("perf_hooks");
const path = require("path");

let mainWindow = null;
let tray = null;
const launchStartedAt = performance.now();
const launchSmokeBudgetMs = Number(process.env.MAXWELL_DESKTOP_LAUNCH_BUDGET_MS || 2000);
let lastFleetStatus = {
  online: false,
  activeTasks: 0,
  repos: 0,
  updatedAt: null,
};
let lastUpdateStatus = {
  state: "idle",
  message: "Updates not checked",
  version: null,
  percent: null,
};

function trayIcon() {
  return nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l2HH5QAAAABJRU5ErkJggg=="
  );
}

function isLaunchSmoke() {
  return process.env.MAXWELL_DESKTOP_LAUNCH_SMOKE === "1";
}

function finishLaunchSmoke(stage) {
  if (!isLaunchSmoke()) return;
  const elapsedMs = Math.round(performance.now() - launchStartedAt);
  const passed = elapsedMs <= launchSmokeBudgetMs;
  process.stdout.write(
    `${JSON.stringify({
      budgetMs: launchSmokeBudgetMs,
      elapsedMs,
      passed,
      stage,
    })}\n`
  );
  app.exit(passed ? 0 : 1);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 1024,
    minHeight: 768,
    show: false,
    title: "Maxwell-Daemon",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
  mainWindow.once("ready-to-show", () => {
    finishLaunchSmoke("ready-to-show");
    if (!isLaunchSmoke()) mainWindow.show();
  });
  updateTaskbar(lastFleetStatus);
  mainWindow.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray() {
  const icon = trayIcon();
  tray = new Tray(icon);
  updateTray(lastFleetStatus);
}

function updateTray(status) {
  lastFleetStatus = { ...lastFleetStatus, ...status };
  updateTaskbar(lastFleetStatus);
  if (!tray) return;
  const state = lastFleetStatus.online ? "online" : "offline";
  const tooltip =
    `Maxwell-Daemon ${state}: ${lastFleetStatus.activeTasks} active task(s), ` +
    `${lastFleetStatus.repos} repo(s)`;
  tray.setToolTip(tooltip);
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: tooltip, enabled: false },
      { type: "separator" },
      { label: "Show Maxwell-Daemon", click: () => mainWindow?.show() },
      { label: "Refresh Fleet", click: () => mainWindow?.webContents.send("desktop:refresh") },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          app.isQuitting = true;
          app.quit();
        },
      },
    ])
  );
}

function updateTaskbar(status) {
  const activeTasks = Math.max(0, Number(status.activeTasks || 0));
  app.setBadgeCount(activeTasks);

  if (!mainWindow) return;
  if (!status.online) {
    mainWindow.setProgressBar(1, { mode: "error" });
    return;
  }
  if (activeTasks > 0) {
    mainWindow.setProgressBar(2, { mode: "indeterminate" });
    return;
  }
  mainWindow.setProgressBar(-1);
}

function publishUpdateStatus(status) {
  lastUpdateStatus = { ...lastUpdateStatus, ...status };
  mainWindow?.webContents.send("desktop:updateStatus", lastUpdateStatus);
}

function registerAutoUpdaterEvents() {
  autoUpdater.autoDownload = true;
  autoUpdater.on("checking-for-update", () => {
    publishUpdateStatus({
      state: "checking",
      message: "Checking for updates",
      percent: null,
    });
  });
  autoUpdater.on("update-available", (info) => {
    publishUpdateStatus({
      state: "available",
      message: `Update ${info.version || ""} is available`.trim(),
      version: info.version || null,
      percent: null,
    });
  });
  autoUpdater.on("download-progress", (progress) => {
    publishUpdateStatus({
      state: "downloading",
      message: "Downloading update",
      percent: Math.round(progress.percent || 0),
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    publishUpdateStatus({
      state: "ready",
      message: `Update ${info.version || ""} is ready to install`.trim(),
      version: info.version || null,
      percent: 100,
    });
    if (Notification.isSupported()) {
      new Notification({
        title: "Maxwell-Daemon update ready",
        body: "Restart the desktop app to install it.",
      }).show();
    }
  });
  autoUpdater.on("update-not-available", () => {
    publishUpdateStatus({
      state: "current",
      message: "Maxwell-Daemon is up to date",
      percent: null,
    });
  });
  autoUpdater.on("error", (error) => {
    publishUpdateStatus({
      state: "error",
      message: error.message || "Update check failed",
      percent: null,
    });
  });
}

function registerShortcuts() {
  globalShortcut.register("CommandOrControl+K", () => {
    mainWindow?.webContents.send("desktop:command-palette");
    mainWindow?.show();
  });
}

ipcMain.handle("desktop:notify", (_event, payload) => {
  if (!Notification.isSupported()) return false;
  new Notification({
    title: payload.title || "Maxwell-Daemon",
    body: payload.body || "",
  }).show();
  return true;
});

ipcMain.handle("desktop:updateTrayStatus", (_event, status) => {
  updateTray(status || {});
  return lastFleetStatus;
});

ipcMain.handle("desktop:checkForUpdates", async () => {
  try {
    publishUpdateStatus({ state: "checking", message: "Checking for updates", percent: null });
    const result = await autoUpdater.checkForUpdates();
    return { ok: true, updateInfo: result?.updateInfo || null, status: lastUpdateStatus };
  } catch (error) {
    publishUpdateStatus({ state: "error", message: error.message, percent: null });
    return { ok: false, error: error.message, status: lastUpdateStatus };
  }
});

ipcMain.handle("desktop:installUpdate", () => {
  if (lastUpdateStatus.state !== "ready") return false;
  autoUpdater.quitAndInstall(false, true);
  return true;
});

app.whenReady().then(() => {
  app.setAppUserModelId("org.d-sorganization.maxwell-daemon");
  createWindow();
  if (isLaunchSmoke()) return;
  createTray();
  registerAutoUpdaterEvents();
  registerShortcuts();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
    mainWindow?.show();
  });
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
