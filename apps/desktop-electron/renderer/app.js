"use strict";

const $ = (id) => document.getElementById(id);

function parseIssueRef(raw) {
  const value = String(raw || "").trim();
  const urlMatch = value.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);
  if (urlMatch) return { repo: urlMatch[1], number: Number(urlMatch[2]) };
  const short = value.match(/^([A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$/);
  if (short) return { repo: short[1], number: Number(short[2]) };
  return null;
}

function render(snapshot, offline = false) {
  const tasks = snapshot?.tasks || [];
  const repos = snapshot?.fleet?.repos || [];
  const activeTasks = tasks.filter((task) => ["queued", "running", "dispatched"].includes(task.status)).length;
  $("tasks").innerHTML = tasks
    .map((task) => `<li>${task.status}: ${task.issue_repo ? `${task.issue_repo}#${task.issue_number}` : task.prompt}</li>`)
    .join("");
  $("fleet").innerHTML = repos
    .map((repo) => `<li>${repo.name}: ${repo.active_tasks || 0} active</li>`)
    .join("");
  $("status-strip").className = `status-strip ${offline ? "offline" : "online"}`;
  $("connection-state").textContent = offline ? "offline cache" : "online";
  $("resource-state").textContent = `${activeTasks} active task(s), ${repos.length} repo(s)`;
  $("updated-state").textContent = snapshot?.updatedAt
    ? `synced ${new Date(snapshot.updatedAt).toLocaleTimeString()}`
    : "not synced";
  document.title = offline ? "Maxwell-Daemon Desktop (offline)" : "Maxwell-Daemon Desktop";
  window.maxwellDesktop.updateTrayStatus({
    online: !offline,
    activeTasks,
    repos: repos.length,
    updatedAt: snapshot?.updatedAt || null,
  });
}

async function refresh() {
  try {
    const snapshot = await window.maxwellDesktop.snapshot();
    render(snapshot);
    const running = snapshot.tasks.filter((task) => task.status === "running").length;
    if (running > 0) {
      window.maxwellDesktop.notify({ title: "Maxwell-Daemon", body: `${running} task(s) running` });
    }
  } catch {
    render(window.maxwellDesktop.cachedSnapshot(), true);
  }
}

function saveSettings() {
  window.maxwellDesktop.saveSettings({
    daemonUrl: $("daemon-url").value,
    token: $("token").value,
  });
}

async function dispatchIssue() {
  const ref = parseIssueRef($("issue-ref").value);
  if (!ref) return;
  await window.maxwellDesktop.dispatchIssue(ref.repo, ref.number, $("issue-mode").value);
  await refresh();
}

function wireDropZone() {
  const zone = $("drop-zone");
  zone.addEventListener("dragover", (event) => {
    event.preventDefault();
    zone.classList.add("active");
  });
  zone.addEventListener("dragleave", () => zone.classList.remove("active"));
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("active");
    const paths = [...event.dataTransfer.files].map((file) => file.path).join(", ");
    zone.textContent = paths || "Drop files here to attach paths to a prompt";
  });
}

document.addEventListener("DOMContentLoaded", () => {
  const settings = window.maxwellDesktop.getSettings();
  $("daemon-url").value = settings.daemonUrl || "http://127.0.0.1:8080";
  $("token").value = settings.token || "";
  $("save-settings").addEventListener("click", saveSettings);
  $("refresh").addEventListener("click", refresh);
  $("dispatch").addEventListener("click", dispatchIssue);
  $("updates").addEventListener("click", () => window.maxwellDesktop.checkForUpdates());
  $("command-button").addEventListener("click", () => $("command-palette").showModal());
  window.maxwellDesktop.onRefresh(refresh);
  window.maxwellDesktop.onCommandPalette(() => $("command-palette").showModal());
  wireDropZone();
  refresh();
});
