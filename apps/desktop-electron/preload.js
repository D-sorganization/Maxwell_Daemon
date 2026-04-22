"use strict";

const { contextBridge, ipcRenderer } = require("electron");

const cacheKey = "maxwell.desktop.snapshot";

async function requestJson(path, options = {}) {
  const settings = JSON.parse(localStorage.getItem("maxwell.desktop.settings") || "{}");
  const baseUrl = (settings.daemonUrl || "http://127.0.0.1:8080").replace(/\/+$/, "");
  const token = settings.token || "";
  const headers = {
    accept: "application/json",
    ...(options.body ? { "content-type": "application/json" } : {}),
    ...(token ? { authorization: `Bearer ${token}` } : {}),
  };
  const response = await fetch(`${baseUrl}${path}`, { ...options, headers });
  if (!response.ok) throw new Error(`${path} failed: ${response.status}`);
  return response.json();
}

contextBridge.exposeInMainWorld("maxwellDesktop", {
  getSettings() {
    return JSON.parse(localStorage.getItem("maxwell.desktop.settings") || "{}");
  },
  saveSettings(settings) {
    localStorage.setItem("maxwell.desktop.settings", JSON.stringify(settings));
  },
  async snapshot() {
    const [tasks, fleet, cost] = await Promise.all([
      requestJson("/api/v1/tasks?limit=100"),
      requestJson("/api/v1/fleet").catch(() => ({ repos: [] })),
      requestJson("/api/v1/cost").catch(() => ({ month_to_date_usd: 0 })),
    ]);
    const snapshot = { tasks, fleet, cost, updatedAt: new Date().toISOString() };
    localStorage.setItem(cacheKey, JSON.stringify(snapshot));
    return snapshot;
  },
  cachedSnapshot() {
    return JSON.parse(localStorage.getItem(cacheKey) || "null");
  },
  dispatchIssue(repo, number, mode) {
    return requestJson("/api/v1/issues/dispatch", {
      method: "POST",
      body: JSON.stringify({ repo, number, mode }),
    });
  },
  notify(payload) {
    return ipcRenderer.invoke("desktop:notify", payload);
  },
  checkForUpdates() {
    return ipcRenderer.invoke("desktop:checkForUpdates");
  },
  onRefresh(callback) {
    ipcRenderer.on("desktop:refresh", callback);
  },
  onCommandPalette(callback) {
    ipcRenderer.on("desktop:command-palette", callback);
  },
});
