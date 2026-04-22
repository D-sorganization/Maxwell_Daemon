"use strict";

const { app, BrowserWindow, Menu, Notification, Tray, globalShortcut, ipcMain, nativeImage } = require("electron");
const { autoUpdater } = require("electron-updater");
const path = require("path");

let mainWindow = null;
let tray = null;
let lastFleetStatus = {
  online: false,
  activeTasks: 0,
  repos: 0,
  updatedAt: null,
};

function trayIcon() {
  return nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/l2HH5QAAAABJRU5ErkJggg=="
  );
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
  mainWindow.once("ready-to-show", () => mainWindow.show());
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
    const result = await autoUpdater.checkForUpdates();
    return { ok: true, updateInfo: result?.updateInfo || null };
  } catch (error) {
    return { ok: false, error: error.message };
  }
});

app.whenReady().then(() => {
  createWindow();
  createTray();
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
