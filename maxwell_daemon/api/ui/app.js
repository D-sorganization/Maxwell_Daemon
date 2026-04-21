// Maxwell-Daemon web UI — vanilla JS, no build step, no framework.
// Talks to the same REST + WebSocket endpoints the CLI uses.

const authToken = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("maxwell-daemon.token");

const headers = () => authToken ? { authorization: `Bearer ${authToken}` } : {};

const state = {
  tasks: new Map(),           // id -> task object
  selected: null,             // currently-shown task id
  testOutput: new Map(),      // task id -> accumulated text
  monitorLines: [],           // raw event lines (capped at 500)
  debugEvents: [],            // raw JSON events for debug view (capped at 200)
  currentView: "tasks",       // active tab
};

// ---- helpers ---------------------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtUsd(n) { return `$${(n || 0).toFixed(4)}`; }
function fmtUsdShort(n) { return `$${(n || 0).toFixed(2)}`; }

function fmtTs(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "short", timeStyle: "medium" });
}

function fmtTsShort(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { timeStyle: "short" });
}

// ---- tab navigation --------------------------------------------------------

function switchView(name) {
  state.currentView = name;
  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.view === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".view").forEach((el) => {
    const active = el.id === `view-${name}`;
    el.hidden = !active;
    el.classList.toggle("active", active);
  });

  // Lazy-load view data
  if (name === "fleet") fetchFleet().catch(console.error);
  if (name === "cost") fetchCostDetail().catch(console.error);
  if (name === "history") renderHistory();
  if (name === "repos") fetchFleet().catch(console.error);  // repos uses same data
}

// ---- data fetching ---------------------------------------------------------

async function fetchTasks() {
  const params = new URLSearchParams();
  const status = document.getElementById("status-filter").value;
  if (status) params.set("status", status);
  params.set("limit", "100");
  const r = await fetch(`/api/v1/tasks?${params}`, { headers: headers() });
  if (!r.ok) throw new Error(`tasks list: ${r.status}`);
  const list = await r.json();
  state.tasks.clear();
  for (const t of list) state.tasks.set(t.id, t);
  renderTasks();
  if (state.currentView === "history") renderHistory();
  if (state.currentView === "cost") renderCostTasks();
}

async function fetchBackends() {
  const r = await fetch("/api/v1/backends", { headers: headers() });
  if (!r.ok) return;
  const body = await r.json();
  const ul = document.getElementById("backends-list");
  ul.innerHTML = "";
  for (const name of body.backends) {
    const li = document.createElement("li");
    li.textContent = name;
    ul.appendChild(li);
  }
}

async function fetchCost() {
  const r = await fetch("/api/v1/cost", { headers: headers() });
  if (!r.ok) return;
  const body = await r.json();
  const el = document.getElementById("cost-summary");
  el.textContent = `MTD ${fmtUsdShort(body.month_to_date_usd)}`;
  return body;
}

async function fetchCostDetail() {
  const body = await fetchCost();
  if (!body) return;

  const detailEl = document.getElementById("cost-summary-detail");
  const byBackend = body.by_backend || {};
  const total = body.month_to_date_usd || 0;
  const taskCount = [...state.tasks.values()].length;
  const avgCost = taskCount > 0
    ? [...state.tasks.values()].reduce((s, t) => s + (t.cost_usd || 0), 0) / taskCount
    : 0;

  detailEl.innerHTML = `
    <div class="cost-stat">
      <span class="value">${fmtUsdShort(total)}</span>
      <span class="label">Month-to-Date</span>
    </div>
    <div class="cost-stat">
      <span class="value">${fmtUsd(avgCost)}</span>
      <span class="label">Avg per Task</span>
    </div>
    <div class="cost-stat">
      <span class="value">${taskCount}</span>
      <span class="label">Total Tasks</span>
    </div>
  `;

  const tbody = document.getElementById("cost-backend-body");
  tbody.innerHTML = "";
  const entries = Object.entries(byBackend).sort((a, b) => b[1] - a[1]);
  if (entries.length === 0) {
    tbody.innerHTML = `<tr><td colspan="2" style="color:var(--muted)">No data yet</td></tr>`;
  }
  for (const [backend, cost] of entries) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${escapeHtml(backend)}</td><td>${fmtUsdShort(cost)}</td>`;
    tbody.appendChild(tr);
  }

  renderCostTasks();
}

function renderCostTasks() {
  const tbody = document.getElementById("cost-task-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  const sorted = [...state.tasks.values()]
    .filter((t) => (t.cost_usd || 0) > 0)
    .sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0))
    .slice(0, 20);
  if (sorted.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:var(--muted)">No cost data yet</td></tr>`;
    return;
  }
  for (const t of sorted) {
    const target = t.issue_repo ? `${t.issue_repo}#${t.issue_number}` : (t.prompt || "").slice(0, 50);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(t.id)}</td>
      <td>${escapeHtml(target)}</td>
      <td>${escapeHtml(t.backend || "—")}</td>
      <td><span class="status-${t.status}">${escapeHtml(t.status)}</span></td>
      <td>${fmtUsd(t.cost_usd)}</td>
    `;
    tbody.appendChild(tr);
  }
}

let _fleetData = null;

async function fetchFleet() {
  const r = await fetch("/api/v1/fleet", { headers: headers() });
  if (!r.ok) return;
  _fleetData = await r.json();
  renderFleet();
  renderRepos();
}

function renderFleet() {
  if (!_fleetData) return;
  const { fleet, repos } = _fleetData;

  const metaEl = document.getElementById("fleet-meta");
  metaEl.textContent =
    `${fleet.name} — discovery every ${fleet.discovery_interval_seconds}s` +
    (fleet.auto_promote_staging ? " — auto-promote enabled" : "");

  const tbody = document.getElementById("fleet-body");
  tbody.innerHTML = "";
  for (const r of repos) {
    const tr = document.createElement("tr");
    const ghLink = r.github_url
      ? `<a href="${escapeHtml(r.github_url)}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a>`
      : escapeHtml(r.name);
    const labels = (r.watch_labels || []).map((l) => `<code>${escapeHtml(l)}</code>`).join(" ");
    const activeCell = r.active_tasks > 0
      ? `<span class="status-running">${r.active_tasks}</span>`
      : "0";
    tr.innerHTML = `
      <td>${ghLink}</td>
      <td>${escapeHtml(r.org)}</td>
      <td>${r.slots}</td>
      <td>${activeCell}</td>
      <td>${fmtUsdShort(r.budget_per_story)}</td>
      <td>${escapeHtml(r.pr_target_branch)}</td>
      <td>${fmtUsd(r.total_cost_usd)}</td>
      <td>${labels}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderRepos() {
  if (!_fleetData) return;
  const grid = document.getElementById("repos-grid");
  grid.innerHTML = "";
  for (const r of _fleetData.repos) {
    const div = document.createElement("div");
    div.className = "repo-card";
    const nameHtml = r.github_url
      ? `<a href="${escapeHtml(r.github_url)}" target="_blank" rel="noopener">${escapeHtml(r.name)}</a>`
      : escapeHtml(r.name);
    const activeHtml = r.active_tasks > 0
      ? `<span class="repo-active">${r.active_tasks} active</span>`
      : "";
    div.innerHTML = `
      <h3>${nameHtml}</h3>
      <div class="repo-meta">
        <span>${escapeHtml(r.org)}</span>
        <span>${r.slots} slot${r.slots !== 1 ? "s" : ""}</span>
        <span>${fmtUsdShort(r.budget_per_story)}/story</span>
        <span>→ ${escapeHtml(r.pr_target_branch)}</span>
        ${activeHtml}
      </div>
    `;
    grid.appendChild(div);
  }
}

async function fetchTaskDetail(id) {
  const r = await fetch(`/api/v1/tasks/${id}`, { headers: headers() });
  if (!r.ok) return;
  const t = await r.json();
  state.tasks.set(id, t);
  renderDetail(t);
}

async function cancelTask(id) {
  const r = await fetch(`/api/v1/tasks/${id}/cancel`, {
    method: "POST",
    headers: headers(),
  });
  if (!r.ok) {
    alert(`Cancel failed: ${r.status}`);
    return;
  }
  await fetchTasks();
}

// ---- rendering -------------------------------------------------------------

function renderTasks() {
  const tbody = document.getElementById("tasks-body");
  tbody.innerHTML = "";
  // ⚡ Bolt: Fast ISO 8601 sort. String operators are ~3x faster than localeCompare.
  const sorted = [...state.tasks.values()].sort(
    (a, b) => a.created_at < b.created_at ? 1 : (a.created_at > b.created_at ? -1 : 0)
  );
  // ⚡ Bolt: Batch DOM insertions using DocumentFragment to prevent layout thrashing.
  const fragment = document.createDocumentFragment();
  for (const t of sorted) {
    const tr = document.createElement("tr");
    tr.dataset.id = t.id;
    const target = t.issue_repo
      ? `${t.issue_repo}#${t.issue_number}`
      : (t.prompt || "").slice(0, 40);
    const pr = t.pr_url
      ? `<a href="${t.pr_url}" target="_blank" rel="noopener">PR</a>`
      : "";
    const cancel = t.status === "queued"
      ? `<button class="cancel" data-cancel="${t.id}">cancel</button>`
      : "";
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${t.kind}</td>
      <td><span class="status-${t.status}">${t.status}</span></td>
      <td>${escapeHtml(target)}</td>
      <td>${fmtUsd(t.cost_usd)}</td>
      <td>${pr}</td>
      <td>${cancel}</td>
    `;
    tr.addEventListener("click", (ev) => {
      if (ev.target.dataset.cancel) return;
      fetchTaskDetail(t.id);
    });
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);

  tbody.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      cancelTask(btn.dataset.cancel);
    });
  });
}

function renderDetail(task) {
  state.selected = task.id;
  document.getElementById("detail-card").hidden = false;
  document.getElementById("detail-title").textContent = `Task ${task.id}`;
  const dl = document.getElementById("detail-fields");
  dl.innerHTML = "";
  const fields = [
    "status", "kind", "repo", "backend", "model",
    "issue_repo", "issue_number", "issue_mode",
    "pr_url", "cost_usd",
    "created_at", "started_at", "finished_at",
    "result", "error",
  ];
  for (const name of fields) {
    const value = task[name];
    if (value === null || value === undefined || value === "") continue;
    const dt = document.createElement("dt");
    dt.textContent = name;
    const dd = document.createElement("dd");
    dd.textContent = String(value).slice(0, 500);
    dl.append(dt, dd);
  }
  const out = document.getElementById("detail-output");
  out.textContent = state.testOutput.get(task.id) || "(no streamed output)";
}

function renderHistory() {
  const ol = document.getElementById("history-list");
  if (!ol) return;
  ol.innerHTML = "";
  const filterVal = document.getElementById("history-filter")?.value || "";
  const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
  const items = [...state.tasks.values()]
    .filter((t) => {
      if (filterVal) return t.status === filterVal;
      return terminalStatuses.has(t.status);
    })
    .sort((a, b) => {
      const aT = a.finished_at || a.created_at;
      const bT = b.finished_at || b.created_at;
      // ⚡ Bolt: Fast ISO 8601 sort using string operators.
      return aT < bT ? 1 : (aT > bT ? -1 : 0);
    });

  if (items.length === 0) {
    ol.innerHTML = `<li style="padding:14px;color:var(--muted)">No finished tasks yet.</li>`;
    return;
  }
  // ⚡ Bolt: Batch DOM insertions using DocumentFragment to prevent layout thrashing.
  const fragment = document.createDocumentFragment();
  for (const t of items) {
    const li = document.createElement("li");
    const target = t.issue_repo
      ? `${t.issue_repo}#${t.issue_number}`
      : (t.prompt || "").slice(0, 80);
    const pr = t.pr_url
      ? ` <a href="${t.pr_url}" target="_blank" rel="noopener">PR ↗</a>`
      : "";
    li.innerHTML = `
      <span class="ts">${escapeHtml(fmtTs(t.finished_at || t.created_at))}</span>
      <span class="title">
        <span class="status-${t.status}">${t.status}</span>
        ${escapeHtml(target)}${pr}
      </span>
      <span class="cost">${fmtUsd(t.cost_usd)}</span>
    `;
    li.addEventListener("click", () => {
      switchView("tasks");
      fetchTaskDetail(t.id);
    });
    fragment.appendChild(li);
  }
  ol.appendChild(fragment);
}

// ---- monitor view ----------------------------------------------------------

function appendMonitorLine(line) {
  state.monitorLines.push(line);
  if (state.monitorLines.length > 500) state.monitorLines.shift();

  if (state.currentView !== "monitor") return;
  const el = document.getElementById("monitor-log");
  const filterVal = document.getElementById("monitor-filter")?.value?.toLowerCase() || "";
  const visible = filterVal
    ? state.monitorLines.filter((l) => l.toLowerCase().includes(filterVal))
    : state.monitorLines;
  el.textContent = visible.join("\n") || "(no matching events)";
  el.scrollTop = el.scrollHeight;
}

function refreshMonitorDisplay() {
  const el = document.getElementById("monitor-log");
  if (!el) return;
  const filterVal = document.getElementById("monitor-filter")?.value?.toLowerCase() || "";
  const visible = filterVal
    ? state.monitorLines.filter((l) => l.toLowerCase().includes(filterVal))
    : state.monitorLines;
  el.textContent = visible.join("\n") || "(waiting for events…)";
  el.scrollTop = el.scrollHeight;
}

// ---- debug view ------------------------------------------------------------

function appendDebugEvent(raw) {
  state.debugEvents.push(raw);
  if (state.debugEvents.length > 200) state.debugEvents.shift();
  if (state.currentView !== "debug") return;
  const el = document.getElementById("debug-log");
  el.textContent = state.debugEvents.join("\n");
  el.scrollTop = el.scrollHeight;
}

function refreshDebugDisplay() {
  const el = document.getElementById("debug-log");
  if (!el) return;
  el.textContent = state.debugEvents.join("\n") || "(no events yet)";
  el.scrollTop = el.scrollHeight;
}

// ---- live event stream -----------------------------------------------------

function openEventStream() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = new URL(`${proto}//${location.host}/api/v1/events`);
  if (authToken) url.searchParams.set("token", authToken);
  const ws = new WebSocket(url);
  const conn = document.getElementById("connection");

  ws.addEventListener("open", () => {
    conn.textContent = "live";
    conn.className = "pill ok";
  });

  ws.addEventListener("close", () => {
    conn.textContent = "disconnected";
    conn.className = "pill err";
    setTimeout(openEventStream, 2000);
  });

  ws.addEventListener("error", () => {
    conn.textContent = "error";
    conn.className = "pill err";
  });

  ws.addEventListener("message", (ev) => {
    let evt;
    try { evt = JSON.parse(ev.data); } catch { return; }
    appendDebugEvent(ev.data);
    const ts = fmtTsShort(new Date().toISOString());
    appendMonitorLine(`[${ts}] ${evt.kind} ${JSON.stringify(evt.payload || {}).slice(0, 120)}`);
    handleEvent(evt);
  });
}

let _fetchTasksTimer = null;
const _fetchTaskDetailTimers = new Map();

function handleEvent(evt) {
  const p = evt.payload || {};
  if (evt.kind === "test_output" && p.task_id) {
    const prev = state.testOutput.get(p.task_id) || "";
    state.testOutput.set(p.task_id, (prev + (p.chunk || "")).slice(-64_000));
    if (p.task_id === state.selected) {
      document.getElementById("detail-output").textContent =
        state.testOutput.get(p.task_id);
    }
    return;
  }
  if (p.id) {
    // Debounce detail fetch per task ID
    clearTimeout(_fetchTaskDetailTimers.get(p.id));
    _fetchTaskDetailTimers.set(
      p.id,
      setTimeout(() => fetchTaskDetail(p.id).catch(() => {}), 300)
    );

    // Debounce global tasks list fetch
    clearTimeout(_fetchTasksTimer);
    _fetchTasksTimer = setTimeout(() => fetchTasks().catch(() => {}), 300);
  }
}

// ---- wiring ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  // Tab navigation
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });

  // Tasks view
  document.getElementById("refresh-btn").addEventListener("click", fetchTasks);
  document.getElementById("status-filter").addEventListener("change", fetchTasks);
  document.getElementById("detail-close").addEventListener("click", () => {
    document.getElementById("detail-card").hidden = true;
    state.selected = null;
  });

  // Fleet view
  document.getElementById("fleet-refresh-btn").addEventListener("click", () => fetchFleet().catch(console.error));

  // Cost view
  document.getElementById("cost-refresh-btn").addEventListener("click", () => fetchCostDetail().catch(console.error));

  // History view
  document.getElementById("history-filter").addEventListener("change", renderHistory);
  document.getElementById("history-refresh-btn").addEventListener("click", () => fetchTasks().catch(console.error));

  // Monitor view
  document.getElementById("monitor-filter").addEventListener("input", refreshMonitorDisplay);
  document.getElementById("monitor-clear-btn").addEventListener("click", () => {
    state.monitorLines.length = 0;
    document.getElementById("monitor-log").textContent = "(cleared)";
  });

  // Repos view
  document.getElementById("repos-refresh-btn").addEventListener("click", () => fetchFleet().catch(console.error));

  // Debug view
  document.getElementById("debug-clear-btn").addEventListener("click", () => {
    state.debugEvents.length = 0;
    document.getElementById("debug-log").textContent = "(cleared)";
  });

  // Keyboard shortcut: digit keys 1-7 switch tabs
  document.addEventListener("keydown", (ev) => {
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const views = ["tasks", "fleet", "cost", "history", "monitor", "repos", "debug"];
    const idx = parseInt(ev.key, 10) - 1;
    if (idx >= 0 && idx < views.length) {
      ev.preventDefault();
      switchView(views[idx]);
    }
  });

  // Touch swipe to navigate tabs (left/right swipe)
  const views = ["tasks", "fleet", "cost", "history", "monitor", "repos", "debug"];
  let touchStartX = 0;
  let touchStartY = 0;
  document.addEventListener("touchstart", (ev) => {
    touchStartX = ev.touches[0].clientX;
    touchStartY = ev.touches[0].clientY;
  }, { passive: true });
  document.addEventListener("touchend", (ev) => {
    const dx = ev.changedTouches[0].clientX - touchStartX;
    const dy = ev.changedTouches[0].clientY - touchStartY;
    if (Math.abs(dx) < 50 || Math.abs(dy) > Math.abs(dx)) return; // not a horizontal swipe
    const current = views.indexOf(state.currentView);
    if (current === -1) return;
    const next = dx < 0
      ? Math.min(current + 1, views.length - 1)
      : Math.max(current - 1, 0);
    if (next !== current) switchView(views[next]);
  }, { passive: true });

  // Handle ?view= URL param on load (PWA shortcut links)
  const viewParam = new URLSearchParams(location.search).get("view");
  if (viewParam && views.includes(viewParam)) switchView(viewParam);

  // Initial load
  fetchTasks().catch(console.error);
  fetchBackends().catch(console.error);
  fetchCost().catch(console.error);
  openEventStream();

  setInterval(fetchCost, 30_000);
});

// ---- new-task dialog -----------------------------------------------------

function parseIssueRef(raw) {
  const s = String(raw || "").trim();
  const urlMatch = s.match(/github\.com\/([^/]+\/[^/]+)\/issues\/(\d+)/);
  if (urlMatch) return { repo: urlMatch[1], number: Number(urlMatch[2]) };
  const short = s.match(/^([A-Za-z0-9][A-Za-z0-9._-]*\/[A-Za-z0-9][A-Za-z0-9._-]*)#(\d+)$/);
  if (short) return { repo: short[1], number: Number(short[2]) };
  return null;
}

function openNewTaskDialog() {
  const d = document.getElementById("new-task-dialog");
  document.getElementById("new-task-error").hidden = true;
  document.getElementById("issue-input").value = "";
  document.getElementById("prompt-input").value = "";
  d.showModal();
  setTimeout(() => document.getElementById("issue-input").focus(), 0);
}

function closeNewTaskDialog() {
  document.getElementById("new-task-dialog").close();
}

function wireKindSwitch() {
  const issueFields = document.getElementById("issue-fields");
  const promptFields = document.getElementById("prompt-fields");
  document.querySelectorAll('input[name="kind"]').forEach((r) => {
    r.addEventListener("change", () => {
      const kind = document.querySelector('input[name="kind"]:checked').value;
      issueFields.hidden = kind !== "issue";
      promptFields.hidden = kind !== "prompt";
      (kind === "issue"
        ? document.getElementById("issue-input")
        : document.getElementById("prompt-input")
      ).focus();
    });
  });
}

async function submitNewTask(ev) {
  ev.preventDefault();
  const errEl = document.getElementById("new-task-error");
  errEl.hidden = true;

  const kind = document.querySelector('input[name="kind"]:checked').value;
  let url, body;

  if (kind === "issue") {
    const ref = parseIssueRef(document.getElementById("issue-input").value);
    if (!ref) {
      errEl.textContent = "Unrecognised issue reference.";
      errEl.hidden = false;
      return;
    }
    url = "/api/v1/issues/dispatch";
    body = {
      repo: ref.repo,
      number: ref.number,
      mode: document.getElementById("mode-input").value,
    };
  } else {
    const prompt = document.getElementById("prompt-input").value.trim();
    if (!prompt) {
      errEl.textContent = "Prompt cannot be empty.";
      errEl.hidden = false;
      return;
    }
    url = "/api/v1/tasks";
    body = { prompt };
  }

  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json", ...headers() },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const detail = await r.text();
      errEl.textContent = `Dispatch failed (${r.status}): ${detail.slice(0, 200)}`;
      errEl.hidden = false;
      return;
    }
  } catch (e) {
    errEl.textContent = `Network error: ${e.message}`;
    errEl.hidden = false;
    return;
  }

  closeNewTaskDialog();
  await fetchTasks();
}

document.addEventListener("DOMContentLoaded", () => {
  wireKindSwitch();
  document.getElementById("new-task-btn").addEventListener("click", openNewTaskDialog);
  document.getElementById("new-task-cancel").addEventListener("click", closeNewTaskDialog);
  document.getElementById("new-task-form").addEventListener("submit", submitNewTask);

  // N opens the dialog when it's closed and no input is focused.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "n" && ev.key !== "N") return;
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const d = document.getElementById("new-task-dialog");
    if (!d.open) {
      ev.preventDefault();
      openNewTaskDialog();
    }
  });
});
