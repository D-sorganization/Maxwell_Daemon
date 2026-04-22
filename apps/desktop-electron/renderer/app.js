"use strict";

const $ = (id) => document.getElementById(id);
const notificationStateKey = "maxwell.desktop.notificationState";
const activeTaskStatuses = new Set(["queued", "running", "dispatched"]);
const doneTaskStatuses = new Set(["completed", "succeeded", "passed", "merged"]);
const blockedTaskStatuses = new Set(["failed", "error", "blocked", "cancelled"]);

function parseIssueRef(raw) {
  const value = String(raw || "").trim();
  const urlMatch = value.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);
  if (urlMatch) return { repo: urlMatch[1], number: Number(urlMatch[2]) };
  const short = value.match(/^([A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$/);
  if (short) return { repo: short[1], number: Number(short[2]) };
  return null;
}

function taskKey(task) {
  if (task.id) return String(task.id);
  if (task.issue_repo && task.issue_number) return `${task.issue_repo}#${task.issue_number}`;
  return String(task.prompt || "unknown-task");
}

function taskLabel(task) {
  if (task.issue_repo && task.issue_number) return `${task.issue_repo}#${task.issue_number}`;
  return String(task.prompt || task.id || "Maxwell task");
}

function loadNotificationState() {
  try {
    return JSON.parse(localStorage.getItem(notificationStateKey) || "{}");
  } catch {
    return {};
  }
}

function saveNotificationState(state) {
  localStorage.setItem(notificationStateKey, JSON.stringify(state));
}

function notificationForStatus(task) {
  if (activeTaskStatuses.has(task.status)) {
    return { title: "Delegate active", body: `${taskLabel(task)} is ${task.status}` };
  }
  if (doneTaskStatuses.has(task.status)) {
    return { title: "Delegate completed", body: `${taskLabel(task)} finished` };
  }
  if (blockedTaskStatuses.has(task.status)) {
    return { title: "Delegate needs attention", body: `${taskLabel(task)} is ${task.status}` };
  }
  return null;
}

function notifyForSnapshot(snapshot, offline = false) {
  const previous = loadNotificationState();
  const nextTasks = {};
  const tasks = snapshot?.tasks || [];

  if (offline && previous.online !== false) {
    window.maxwellDesktop.notify({
      title: "Maxwell-Daemon offline",
      body: "Showing the cached desktop snapshot.",
    });
  } else if (!offline && previous.online === false) {
    window.maxwellDesktop.notify({
      title: "Maxwell-Daemon online",
      body: "Desktop status is live again.",
    });
  }

  for (const task of tasks) {
    const key = taskKey(task);
    nextTasks[key] = task.status;
    if (!previous.tasks || previous.tasks[key] === undefined || previous.tasks[key] === task.status) {
      continue;
    }
    const notification = notificationForStatus(task);
    if (notification) window.maxwellDesktop.notify(notification);
  }

  saveNotificationState({ online: !offline, tasks: nextTasks });
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

function renderUpdateStatus(status) {
  const state = status || { state: "idle", message: "Updates not checked" };
  const message = state.percent === null || state.percent === undefined
    ? state.message
    : `${state.message} (${state.percent}%)`;
  $("update-state").textContent = message || "Updates not checked";
  $("install-update").hidden = state.state !== "ready";
}

async function refresh() {
  try {
    const snapshot = await window.maxwellDesktop.snapshot();
    render(snapshot);
    notifyForSnapshot(snapshot);
  } catch {
    const cached = window.maxwellDesktop.cachedSnapshot();
    render(cached, true);
    notifyForSnapshot(cached, true);
  }
}

function saveSettings() {
  window.maxwellDesktop.saveSettings({
    daemonUrl: $("daemon-url").value,
    token: $("token").value,
  });
}

function openCommandPalette() {
  const palette = $("command-palette");
  const input = $("command-input");
  $("command-status").textContent = "";
  if (!palette.open) palette.showModal();
  input.focus();
  input.select();
}

async function runCommand(rawCommand) {
  const command = String(rawCommand || "").trim().toLowerCase();
  const palette = $("command-palette");
  const status = $("command-status");
  if (command === "refresh" || command === "sync") {
    palette.close();
    await refresh();
    return true;
  }
  if (command === "dispatch") {
    palette.close();
    $("issue-ref").focus();
    return true;
  }
  if (command === "updates" || command === "update") {
    const result = await window.maxwellDesktop.checkForUpdates();
    renderUpdateStatus(result.status);
    palette.close();
    return true;
  }
  status.textContent = command ? `Unknown command: ${command}` : "Enter a command";
  return false;
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
  $("updates").addEventListener("click", async () => {
    const result = await window.maxwellDesktop.checkForUpdates();
    renderUpdateStatus(result.status);
  });
  $("install-update").addEventListener("click", () => window.maxwellDesktop.installUpdate());
  $("command-button").addEventListener("click", openCommandPalette);
  $("command-form").addEventListener("submit", (event) => {
    event.preventDefault();
    runCommand($("command-input").value);
  });
  document.querySelectorAll("[data-command]").forEach((button) => {
    button.addEventListener("click", () => runCommand(button.dataset.command));
  });
  window.maxwellDesktop.onRefresh(refresh);
  window.maxwellDesktop.onUpdateStatus(renderUpdateStatus);
  window.maxwellDesktop.onCommandPalette(openCommandPalette);
  wireDropZone();
  renderUpdateStatus();
  refresh();
});
