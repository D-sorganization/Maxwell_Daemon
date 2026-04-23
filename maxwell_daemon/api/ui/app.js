// Maxwell-Daemon web UI — vanilla JS, no build step, no framework.
// Talks to the same REST + WebSocket endpoints the CLI uses.

const authToken = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("maxwell-daemon.token");

const headers = () => authToken ? { authorization: `Bearer ${authToken}` } : {};

const state = {
  tasks: new Map(),           // id -> task object (filtered by Tasks tab status filter)
  allTasks: new Map(),        // id -> task object (always unfiltered, used for cost analytics)
  controlPlane: [],           // gate-aware work item snapshots
  controlPlaneError: "",      // visible gauntlet fetch failure message
  gauntletTaskFocus: null,    // optional task filter for the gauntlet view
  selected: null,             // currently-shown task id
  testOutput: new Map(),      // task id -> accumulated text
  monitorLines: [],           // raw event lines (capped at 500)
  debugEvents: [],            // raw JSON events for debug view (capped at 200)
  currentView: "tasks",       // active tab
};

const viewOrder = [
  "tasks", "fleet", "gauntlet", "work-items", "approvals", "artifacts",
  "graphs", "checks", "repos", "history", "cost", "monitor", "debug",
];

const commands = [
  { id: "view.tasks", title: "Show Tasks", detail: "Open the task editor", run: () => switchView("tasks") },
  { id: "view.fleet", title: "Show Fleet", detail: "Open fleet overview", run: () => switchView("fleet") },
  { id: "view.gauntlet", title: "Show Gauntlet", detail: "Open gate and critic status", run: () => switchView("gauntlet") },
  { id: "view.work-items", title: "Show Work Items", detail: "Open work-item queue", run: () => switchView("work-items") },
  { id: "view.approvals", title: "Show Approvals", detail: "Open action approval queue", run: () => switchView("approvals") },
  { id: "view.artifacts", title: "Show Artifacts", detail: "Open artifact browser", run: () => switchView("artifacts") },
  { id: "view.graphs", title: "Show Task Graphs", detail: "Open sub-agent graph runs", run: () => switchView("graphs") },
  { id: "view.checks", title: "Show Checks", detail: "Open validation checks", run: () => switchView("checks") },
  { id: "view.repos", title: "Show Repositories", detail: "Open repository dashboard", run: () => switchView("repos") },
  { id: "view.monitor", title: "Show Daemon Logs", detail: "Open live monitor", run: () => switchView("monitor") },
  { id: "view.history", title: "Show History", detail: "Open completed work timeline", run: () => switchView("history") },
  { id: "view.cost", title: "Show Cost", detail: "Open cost analytics", run: () => switchView("cost") },
  { id: "task.new", title: "Dispatch New Task", detail: "Open task dispatch dialog", run: () => openNewTaskDialog() },
  { id: "data.refresh", title: "Refresh Dashboard", detail: "Reload task, cost, and fleet data", run: () => refreshAll() },
];

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

function fmtBytes(n) {
  const value = Number(n || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function fmtDurationSeconds(value) {
  if (value === null || value === undefined) return "—";
  const seconds = Math.max(0, Math.round(Number(value)));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`;
}

function setTableMessage(tbodyId, colspan, message) {
  const tbody = document.getElementById(tbodyId);
  if (!tbody) return;
  tbody.innerHTML = "";
  const tr = document.createElement("tr");
  const td = document.createElement("td");
  td.colSpan = colspan;
  td.className = "empty-cell";
  td.textContent = message;
  tr.appendChild(td);
  tbody.appendChild(tr);
}

function shortId(value) {
  const s = String(value || "");
  return s.length > 12 ? `${s.slice(0, 12)}…` : s;
}

function controlPlaneByTaskId(taskId) {
  return state.controlPlane.find((item) => item.task_id === taskId) || null;
}

function setGauntletTaskFocus(taskId) {
  state.gauntletTaskFocus = taskId || null;
  const label = document.getElementById("gauntlet-focus-state");
  const clear = document.getElementById("gauntlet-clear-focus-btn");
  if (label) {
    label.textContent = state.gauntletTaskFocus
      ? `Focused on ${state.gauntletTaskFocus}`
      : "All tasks";
  }
  if (clear) {
    clear.hidden = !state.gauntletTaskFocus;
  }
}

function openGauntletForTask(taskId) {
  setGauntletTaskFocus(taskId);
  switchView("gauntlet");
  fetchGauntlet().catch(console.error);
}

// ---- tab navigation --------------------------------------------------------

function switchView(name) {
  state.currentView = name;
  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.view === name;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".sidebar-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === name);
  });
  document.querySelectorAll(".view").forEach((el) => {
    const active = el.id === `view-${name}`;
    el.hidden = !active;
    el.classList.toggle("active", active);
  });

  // Lazy-load view data
  if (name === "fleet") fetchFleet().catch(console.error);
  if (name === "gauntlet") fetchGauntlet().catch(console.error);
  if (name === "work-items") fetchWorkItems().catch(console.error);
  if (name === "approvals") fetchApprovals().catch(console.error);
  if (name === "graphs") fetchTaskGraphs().catch(console.error);
  if (name === "checks") fetchChecks().catch(console.error);
  if (name === "cost") fetchCostDetail().catch(console.error);
  if (name === "history") renderHistory();
  if (name === "repos") fetchFleet().catch(console.error);  // repos uses same data
  document.getElementById("status-operation").textContent = `Viewing ${name}`;
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

  // Always fetch an unfiltered snapshot for cost analytics so that the cost
  // dashboard is not affected by the Tasks tab's status filter (#235).
  if (status) {
    const allParams = new URLSearchParams({ limit: "500" });
    const allR = await fetch(`/api/v1/tasks?${allParams}`, { headers: headers() });
    if (allR.ok) {
      const allList = await allR.json();
      state.allTasks.clear();
      for (const t of allList) state.allTasks.set(t.id, t);
    } else {
      state.allTasks = new Map(state.tasks);
    }
  } else {
    // No filter active — filtered set is already the full set.
    state.allTasks = new Map(state.tasks);
  }

  renderTasks();
  updateStatusResources();
  if (state.currentView === "history") renderHistory();
  if (state.currentView === "cost") renderCostTasks();
}

async function fetchBackends() {
  const r = await fetch("/api/v1/backends", { headers: headers() });
  if (!r.ok) return;
  const body = await r.json();
  const ul = document.getElementById("backends-list");
  const compact = document.getElementById("sidebar-backends-list");
  ul.innerHTML = "";
  compact.innerHTML = "";
  for (const name of body.backends) {
    const li = document.createElement("li");
    li.textContent = name;
    ul.appendChild(li);
    const compactLi = document.createElement("li");
    compactLi.textContent = name;
    compact.appendChild(compactLi);
  }
}

async function fetchCost() {
  const r = await fetch("/api/v1/cost", { headers: headers() });
  if (!r.ok) return;
  const body = await r.json();
  const el = document.getElementById("cost-summary");
  el.textContent = `MTD ${fmtUsdShort(body.month_to_date_usd)}`;
  updateStatusResources(body.month_to_date_usd);
  return body;
}

async function refreshAll() {
  document.getElementById("status-operation").textContent = "Refreshing";
  await Promise.all([
    fetchTasks().catch(console.error),
    fetchBackends().catch(console.error),
    fetchFleet().catch(console.error),
    fetchGauntlet().catch(console.error),
    fetchCost().catch(console.error),
  ]);
  document.getElementById("status-operation").textContent = "Ready";
}

function updateStatusResources(costOverride) {
  const taskCount = state.allTasks.size || state.tasks.size;
  const running = [...state.allTasks.values()].filter((t) => t.status === "running").length;
  const totalCost = costOverride ?? [...state.allTasks.values()].reduce((s, t) => s + (t.cost_usd || 0), 0);
  const el = document.getElementById("status-resources");
  if (el) el.textContent = `Tasks ${taskCount} | Running ${running} | Cost ${fmtUsdShort(totalCost)}`;
}

async function fetchCostDetail() {
  const body = await fetchCost();
  if (!body) return;

  const detailEl = document.getElementById("cost-summary-detail");
  const byBackend = body.by_backend || {};
  const total = body.month_to_date_usd || 0;
  const taskCount = [...state.allTasks.values()].length;
  const avgCost = taskCount > 0
    ? [...state.allTasks.values()].reduce((s, t) => s + (t.cost_usd || 0), 0) / taskCount
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
  const sorted = [...state.allTasks.values()]
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
  const sidebarSummary = document.getElementById("sidebar-fleet-summary");
  if (sidebarSummary) {
    const active = repos.reduce((sum, repo) => sum + (repo.active_tasks || 0), 0);
    sidebarSummary.textContent = `${repos.length} repos, ${active} active tasks`;
  }

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

async function fetchGauntlet() {
  state.controlPlaneError = "";
  const params = new URLSearchParams({ limit: "100" });
  if (state.gauntletTaskFocus) params.set("task_id", state.gauntletTaskFocus);
  try {
    const r = await fetch(`/api/v1/control-plane/gauntlet?${params}`, { headers: headers() });
    if (!r.ok) {
      state.controlPlane = [];
      state.controlPlaneError = r.status === 401 || r.status === 403
        ? "Gate gauntlet requires a viewer token; action controls stay hidden until access is granted."
        : `Gate gauntlet unavailable (${r.status})`;
      renderGauntlet();
      renderTasks();
      return;
    }
    state.controlPlane = await r.json();
  } catch {
    state.controlPlane = [];
    state.controlPlaneError = "Gate gauntlet unavailable (network error)";
    renderGauntlet();
    renderTasks();
    return;
  }
  renderGauntlet();
  renderTasks();
}

function renderGauntlet() {
  const board = document.getElementById("gauntlet-board");
  if (!board) return;
  board.innerHTML = "";
  setGauntletTaskFocus(state.gauntletTaskFocus);
  if (state.controlPlaneError) {
    board.innerHTML = `<div class="empty-state gauntlet-error">${escapeHtml(state.controlPlaneError)}</div>`;
    return;
  }
  if (state.controlPlane.length === 0) {
    board.innerHTML = `<div class="empty-state">${
      state.gauntletTaskFocus
        ? `Task ${escapeHtml(state.gauntletTaskFocus)} has not reached the gauntlet yet.`
        : "No work items have reached the control plane yet."
    }</div>`;
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.controlPlane) {
    const section = document.createElement("article");
    section.className = `gauntlet-item decision-${item.final_decision}`;
    const gates = (item.gates || []).map((gate) => `
      <li class="gate-step gate-${gate.status}">
        <span class="gate-name">${escapeHtml(gate.name)}</span>
        <span class="gate-status">${escapeHtml(gate.status)}</span>
        ${gate.next_action ? `<span class="gate-action">${escapeHtml(gate.next_action)}</span>` : ""}
        ${(gate.evidence_links || []).map((url) => `<a href="${escapeHtml(url)}" target="_blank" rel="noopener">evidence</a>`).join("")}
      </li>
    `).join("");
    const findings = (item.critic_findings || []).map((finding) => `
      <li class="finding finding-${finding.severity}">
        <div class="finding-head">
          <strong>${escapeHtml(finding.severity)}</strong>
          <span class="finding-critic">${escapeHtml(finding.critic || "critic")}</span>
        </div>
        <span class="finding-title">${escapeHtml(finding.title || finding.message || "Finding")}</span>
        <span class="finding-detail">${escapeHtml(finding.detail || finding.message || "No detail recorded.")}</span>
        ${(finding.file || finding.line)
          ? `<span class="finding-meta">${escapeHtml(finding.file || "unknown file")}${finding.line ? `:${escapeHtml(String(finding.line))}` : ""}</span>`
          : ""}
        ${(((Array.isArray(finding.evidence) ? finding.evidence : (finding.evidence ? [finding.evidence] : []))).length)
          ? `<ul class="evidence-list">${(Array.isArray(finding.evidence) ? finding.evidence : [finding.evidence]).map((entry) => `<li>${escapeHtml(entry)}</li>`).join("")}</ul>`
          : ""}
      </li>
    `).join("") || `<li class="finding finding-note">No critic findings recorded.</li>`;
    const delegates = (item.delegates || []).map((delegate) => `
      <li class="delegate-entry">
        <div class="delegate-head">
          <span class="status-${delegate.status}">${escapeHtml(delegate.status)}</span>
          <span>${escapeHtml(delegate.role)}</span>
        </div>
        <div class="delegate-meta">
          ${delegate.backend ? `<span>via ${escapeHtml(delegate.backend)}</span>` : ""}
          ${delegate.machine ? `<span>on ${escapeHtml(delegate.machine)}</span>` : ""}
          <span>${fmtUsd(delegate.cost_usd || 0)}</span>
          <span>${escapeHtml(fmtDurationSeconds(delegate.duration_seconds))}</span>
        </div>
        ${delegate.latest_checkpoint
          ? `<p class="delegate-checkpoint">${escapeHtml(delegate.latest_checkpoint)}</p>`
          : ""}
      </li>
    `).join("") || `<li class="empty-state">No delegate sessions recorded yet.</li>`;
    const routing = item.resource_routing || {};
    const actions = (item.actions || []).map((action) => `
      <button
        type="button"
        class="gate-action-btn gate-action-${escapeHtml(action.kind)}"
        data-gate-action="${escapeHtml(action.kind)}"
      >
        ${escapeHtml(action.label)}
      </button>
    `).join("");
    section.innerHTML = `
      <div class="gauntlet-item-head">
        <div>
          <h3>${escapeHtml(item.title)}</h3>
          <p>${escapeHtml(item.next_action)}</p>
        </div>
        <div class="gauntlet-item-meta">
          ${item.current_gate ? `<span class="gate-focus-pill">${escapeHtml(item.current_gate)}</span>` : ""}
          <span class="decision-pill">${escapeHtml(item.final_decision)}</span>
        </div>
      </div>
      ${actions ? `<div class="gate-actions">${actions}</div>` : ""}
      <div class="gauntlet-columns">
        <section>
          <h4>Timeline</h4>
          <ol class="gate-timeline">${gates}</ol>
        </section>
        <section>
          <h4>Critics</h4>
          <ul class="finding-list">${findings}</ul>
        </section>
        <section>
          <h4>Delegate</h4>
          <ul class="delegate-list">${delegates}</ul>
          <h4>Routing</h4>
          <p>${escapeHtml(routing.selected_backend || "No backend selected")}</p>
          ${routing.warning ? `<p class="routing-warning">${escapeHtml(routing.warning)}</p>` : ""}
        </section>
      </div>
    `;
    section.querySelectorAll("[data-gate-action]").forEach((btn) => {
      const action = (item.actions || []).find((candidate) => candidate.kind === btn.dataset.gateAction);
      if (!action) return;
      btn.addEventListener("click", () => submitGateAction(action, item).catch((error) => {
        alert(`Gate action failed: ${error.message}`);
      }));
    });
    fragment.appendChild(section);
  }
  board.appendChild(fragment);
}

async function fetchWorkItems() {
  const r = await fetch("/api/v1/work-items?limit=100", { headers: headers() });
  if (!r.ok) {
    setTableMessage("work-items-body", 8, `Work items unavailable (${r.status})`);
    return;
  }
  renderWorkItems(await r.json());
}

function renderWorkItems(items) {
  const tbody = document.getElementById("work-items-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!items.length) {
    setTableMessage("work-items-body", 8, "No work items yet");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(shortId(item.id))}</code></td>
      <td>${escapeHtml(item.title)}</td>
      <td>${escapeHtml(item.status)}</td>
      <td>${escapeHtml(item.repo || "—")}</td>
      <td>${Number(item.priority || 0)}</td>
      <td>${(item.task_ids || []).length}</td>
      <td>${escapeHtml(fmtTs(item.updated_at))}</td>
      <td><button data-owner-kind="work-item" data-owner-id="${escapeHtml(item.id)}">artifacts</button></td>
    `;
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);
  tbody.querySelectorAll("[data-owner-kind]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      document.getElementById("artifact-owner-kind").value = btn.dataset.ownerKind;
      document.getElementById("artifact-owner-id").value = btn.dataset.ownerId;
      switchView("artifacts");
      fetchArtifacts().catch(console.error);
    });
  });
}

async function fetchApprovals() {
  const r = await fetch("/api/v1/actions?status=proposed&limit=100", { headers: headers() });
  if (!r.ok) {
    setTableMessage("approvals-body", 8, `Approvals unavailable (${r.status})`);
    return;
  }
  renderApprovals(await r.json());
}

function renderApprovals(actions) {
  const tbody = document.getElementById("approvals-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!actions.length) {
    setTableMessage("approvals-body", 8, "No proposed actions");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const action of actions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(shortId(action.id))}</code></td>
      <td>${escapeHtml(action.kind)}</td>
      <td>${escapeHtml(action.risk_level)}</td>
      <td><code>${escapeHtml(shortId(action.task_id))}</code></td>
      <td>${action.work_item_id ? `<code>${escapeHtml(shortId(action.work_item_id))}</code>` : "—"}</td>
      <td>${escapeHtml(action.summary)}</td>
      <td>${escapeHtml(fmtTs(action.created_at))}</td>
      <td class="row-actions">
        <button data-action-approve="${escapeHtml(action.id)}">approve</button>
        <button data-action-reject="${escapeHtml(action.id)}">reject</button>
      </td>
    `;
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);
  tbody.querySelectorAll("[data-action-approve]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      decideAction(btn.dataset.actionApprove, "approve").catch(console.error);
    });
  });
  tbody.querySelectorAll("[data-action-reject]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      decideAction(btn.dataset.actionReject, "reject").catch(console.error);
    });
  });
}

async function decideAction(actionId, decision) {
  const body = decision === "reject"
    ? JSON.stringify({ reason: prompt("Reject reason") || null })
    : undefined;
  const r = await fetch(`/api/v1/actions/${encodeURIComponent(actionId)}/${decision}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers() },
    body,
  });
  if (!r.ok) {
    alert(`${decision} failed: ${r.status}`);
    return;
  }
  await fetchApprovals();
}

async function fetchArtifacts() {
  const ownerKind = document.getElementById("artifact-owner-kind").value;
  const ownerId = document.getElementById("artifact-owner-id").value.trim();
  document.getElementById("artifact-content").textContent = "(select an artifact)";
  if (!ownerId) {
    setTableMessage("artifacts-body", 7, "Enter an owner id");
    return;
  }
  const base = ownerKind === "work-item"
    ? `/api/v1/work-items/${encodeURIComponent(ownerId)}/artifacts`
    : `/api/v1/tasks/${encodeURIComponent(ownerId)}/artifacts`;
  const r = await fetch(base, { headers: headers() });
  if (!r.ok) {
    setTableMessage("artifacts-body", 7, `Artifacts unavailable (${r.status})`);
    return;
  }
  renderArtifacts(await r.json());
}

function renderArtifacts(artifacts) {
  const tbody = document.getElementById("artifacts-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!artifacts.length) {
    setTableMessage("artifacts-body", 7, "No artifacts found");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const artifact of artifacts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(shortId(artifact.id))}</code></td>
      <td>${escapeHtml(artifact.kind)}</td>
      <td>${escapeHtml(artifact.name)}</td>
      <td>${escapeHtml(artifact.media_type)}</td>
      <td>${fmtBytes(artifact.size_bytes)}</td>
      <td>${escapeHtml(fmtTs(artifact.created_at))}</td>
      <td><button data-artifact-id="${escapeHtml(artifact.id)}" data-artifact-media="${escapeHtml(artifact.media_type)}">view</button></td>
    `;
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);
  tbody.querySelectorAll("[data-artifact-id]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      fetchArtifactContent(btn.dataset.artifactId, btn.dataset.artifactMedia).catch(console.error);
    });
  });
}

let _artifactObjectUrl = null;

async function fetchArtifactContent(artifactId, mediaType) {
  const r = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`, {
    headers: headers(),
  });
  const el = document.getElementById("artifact-content");
  if (!r.ok) {
    el.textContent = `Artifact unavailable (${r.status})`;
    return;
  }
  if (_artifactObjectUrl) URL.revokeObjectURL(_artifactObjectUrl);
  const blob = await r.blob();
  el.innerHTML = "";
  if (String(mediaType || "").startsWith("image/")) {
    _artifactObjectUrl = URL.createObjectURL(blob);
    const img = document.createElement("img");
    img.src = _artifactObjectUrl;
    img.alt = artifactId;
    el.appendChild(img);
    return;
  }
  const pre = document.createElement("pre");
  pre.textContent = (await blob.text()).slice(0, 128_000);
  el.appendChild(pre);
}

async function fetchTaskGraphs() {
  const r = await fetch("/api/v1/task-graphs?limit=100", { headers: headers() });
  if (!r.ok) {
    setTableMessage("graphs-body", 6, `Task graphs unavailable (${r.status})`);
    return;
  }
  renderTaskGraphs(await r.json());
}

function renderTaskGraphs(records) {
  const tbody = document.getElementById("graphs-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!records.length) {
    setTableMessage("graphs-body", 6, "No task graphs yet");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const record of records) {
    const graph = record.graph || {};
    const tr = document.createElement("tr");
    tr.dataset.graphId = graph.id;
    tr.innerHTML = `
      <td><code>${escapeHtml(shortId(graph.id))}</code></td>
      <td><code>${escapeHtml(shortId(graph.work_item_id))}</code></td>
      <td>${escapeHtml(graph.status)}</td>
      <td>${escapeHtml(graph.template)}</td>
      <td>${(graph.nodes || []).length}</td>
      <td>${escapeHtml(fmtTs(graph.updated_at))}</td>
    `;
    tr.addEventListener("click", () => renderGraphDetail(record));
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);
}

function renderGraphDetail(record) {
  const detail = document.getElementById("graph-detail");
  detail.textContent = JSON.stringify(record, null, 2);
}

async function fetchChecks() {
  const r = await fetch("/api/v1/check-runs?limit=100", { headers: headers() });
  if (r.status === 404) {
    setTableMessage("checks-body", 4, "Check run API unavailable");
    return;
  }
  if (!r.ok) {
    setTableMessage("checks-body", 4, `Checks unavailable (${r.status})`);
    return;
  }
  renderChecks(await r.json());
}

function renderChecks(checks) {
  const tbody = document.getElementById("checks-body");
  if (!tbody) return;
  tbody.innerHTML = "";
  if (!checks.length) {
    setTableMessage("checks-body", 4, "No checks found");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const check of checks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(check.name || check.id || "check")}</td>
      <td>${escapeHtml(check.status || check.conclusion || "unknown")}</td>
      <td>${escapeHtml(check.target || check.task_id || check.work_item_id || "—")}</td>
      <td>${escapeHtml(fmtTs(check.updated_at || check.completed_at || check.created_at))}</td>
    `;
    fragment.appendChild(tr);
  }
  tbody.appendChild(fragment);
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

async function submitGateAction(action, item) {
  const body = {
    target_id: action.target_id,
    expected_status: action.expected_status,
  };

  if (action.kind === "waive") {
    const actor = prompt(`Who is waiving ${item.title}?`, "");
    if (!actor || !actor.trim()) return;
    const reason = prompt(`Why is ${item.title} being waived?`, "");
    if (!reason || !reason.trim()) return;
    body.actor = actor.trim();
    body.reason = reason.trim();
  } else if (!confirm(`Retry ${item.title}?`)) {
    return;
  }

  const r = await fetch(action.path, {
    method: action.method || "POST",
    headers: { "content-type": "application/json", ...headers() },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const detail = await r.text();
    const summary = r.status === 401 || r.status === 403
      ? "Gate action denied: operator privileges are required."
      : `Gate action failed (${r.status}): ${detail.slice(0, 200)}`;
    alert(summary);
    return;
  }
  await Promise.all([
    fetchGauntlet().catch(console.error),
    fetchTasks().catch(console.error),
  ]);
}

// ---- rendering -------------------------------------------------------------

function renderTasks() {
  const tbody = document.getElementById("tasks-body");
  if (!tbody) return;
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
    const controlPlane = controlPlaneByTaskId(t.id);
    const delegate = controlPlane?.delegates?.[0] || null;
    const target = t.issue_repo
      ? `${t.issue_repo}#${t.issue_number}`
      : (t.prompt || "").slice(0, 40);
    const pr = t.pr_url
      ? `<a href="${t.pr_url}" target="_blank" rel="noopener">PR</a>`
      : "";
    const cancel = t.status === "queued"
      ? `<button class="cancel" data-cancel="${t.id}">cancel</button>`
      : "";
    const review = `<button data-review="${t.id}">review</button>`;
    tr.innerHTML = `
      <td>${t.id}</td>
      <td>${t.kind}</td>
      <td><span class="status-${t.status}">${t.status}</span></td>
      <td>${escapeHtml(target)}</td>
      <td>${escapeHtml(controlPlane?.final_decision || "—")}</td>
      <td>${escapeHtml(controlPlane?.current_gate || "—")}</td>
      <td>${delegate ? `${escapeHtml(delegate.status)}${delegate.machine ? ` on ${escapeHtml(delegate.machine)}` : ""}` : "—"}</td>
      <td>${fmtUsd(t.cost_usd)}</td>
      <td>${pr}</td>
      <td class="row-actions">${review}${cancel}</td>
    `;
    tr.addEventListener("click", (ev) => {
      if (ev.target.dataset.cancel || ev.target.dataset.review) return;
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
  tbody.querySelectorAll("[data-review]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      openGauntletForTask(btn.dataset.review);
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

// ⚡ Bolt: Batch DOM updates using requestAnimationFrame.
// This prevents layout thrashing (forced synchronous layout from accessing scrollHeight)
// and limits DOM updates to the display refresh rate (usually 60fps), massively
// reducing main thread blockage during rapid WebSocket event streams.
let _monitorRaf = null;
let _terminalRaf = null;

function appendMonitorLine(line) {
  state.monitorLines.push(line);
  if (state.monitorLines.length > 500) state.monitorLines.shift();
  scheduleTerminalRefresh();

  if (state.currentView !== "monitor") return;
  if (!_monitorRaf) {
    _monitorRaf = requestAnimationFrame(() => {
      _monitorRaf = null;
      const el = document.getElementById("monitor-log");
      if (!el) return;
      const filterVal = document.getElementById("monitor-filter")?.value?.toLowerCase() || "";
      const visible = filterVal
        ? state.monitorLines.filter((l) => l.toLowerCase().includes(filterVal))
        : state.monitorLines;
      el.textContent = visible.join("\n") || "(no matching events)";
      el.scrollTop = el.scrollHeight;
    });
  }
}

function scheduleTerminalRefresh() {
  if (_terminalRaf) return;
  _terminalRaf = requestAnimationFrame(() => {
    _terminalRaf = null;
    const el = document.getElementById("terminal-log");
    if (!el) return;
    el.textContent = state.monitorLines.join("\n") || "(waiting for daemon output...)";
    el.scrollTop = el.scrollHeight;
  });
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

// ⚡ Bolt: Batch DOM updates using requestAnimationFrame to prevent layout thrashing
// during rapid WebSocket event bursts.
let _debugRaf = null;

function appendDebugEvent(raw) {
  state.debugEvents.push(raw);
  if (state.debugEvents.length > 200) state.debugEvents.shift();
  if (state.currentView !== "debug") return;

  if (!_debugRaf) {
    _debugRaf = requestAnimationFrame(() => {
      _debugRaf = null;
      const el = document.getElementById("debug-log");
      if (!el) return;
      el.textContent = state.debugEvents.join("\n");
      el.scrollTop = el.scrollHeight;
    });
  }
}

function refreshDebugDisplay() {
  const el = document.getElementById("debug-log");
  if (!el) return;
  el.textContent = state.debugEvents.join("\n") || "(no events yet)";
  el.scrollTop = el.scrollHeight;
}

// ---- command palette -------------------------------------------------------

function renderCommandPalette(query = "") {
  const results = document.getElementById("command-palette-results");
  const needle = query.trim().toLowerCase();
  const matches = commands.filter((cmd) => {
    if (!needle) return true;
    return `${cmd.title} ${cmd.detail}`.toLowerCase().includes(needle);
  });
  results.innerHTML = "";
  for (const cmd of matches) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.command = cmd.id;
    btn.innerHTML = `<span>${escapeHtml(cmd.title)}</span><small>${escapeHtml(cmd.detail)}</small>`;
    btn.addEventListener("click", () => runCommand(cmd.id));
    li.appendChild(btn);
    results.appendChild(li);
  }
  if (matches.length === 0) {
    const li = document.createElement("li");
    li.innerHTML = `<button type="button" disabled><span>No commands found</span></button>`;
    results.appendChild(li);
  }
}

function openCommandPalette() {
  const dialog = document.getElementById("command-palette");
  const input = document.getElementById("command-palette-input");
  input.value = "";
  renderCommandPalette();
  dialog.showModal();
  setTimeout(() => input.focus(), 0);
}

function closeCommandPalette() {
  document.getElementById("command-palette").close();
}

function runCommand(id) {
  const cmd = commands.find((item) => item.id === id);
  if (!cmd) return;
  closeCommandPalette();
  cmd.run();
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
let _fetchGauntletTimer = null;
const _fetchTaskDetailTimers = new Map();

// ⚡ Bolt: Batch DOM updates using requestAnimationFrame to prevent layout thrashing
// during high-frequency text streaming.
let _testOutputRaf = null;

function handleEvent(evt) {
  const p = evt.payload || {};
  if (evt.kind === "test_output" && p.task_id) {
    const prev = state.testOutput.get(p.task_id) || "";
    state.testOutput.set(p.task_id, (prev + (p.chunk || "")).slice(-64_000));
    if (p.task_id === state.selected) {
      if (!_testOutputRaf) {
        const selectedAtSchedule = p.task_id;
        _testOutputRaf = requestAnimationFrame(() => {
          _testOutputRaf = null;
          const outEl = document.getElementById("detail-output");
          if (outEl && state.selected === selectedAtSchedule) {
            outEl.textContent =
              state.testOutput.get(selectedAtSchedule) || "(no streamed output)";
          }
        });
      }
    }
    return;
  }
  if (p.id) {
    // ⚡ Bolt: Only fetch detailed task updates for the task actively being viewed.
    // If a task is not selected, we don't need its heavy detailed state fetched
    // on every event (the global tasks list fetch handles high-level status).
    if (state.selected === p.id) {
      clearTimeout(_fetchTaskDetailTimers.get(p.id));
      _fetchTaskDetailTimers.set(
        p.id,
        setTimeout(() => fetchTaskDetail(p.id).catch(() => {}), 300)
      );
    }

    // Debounce global tasks list fetch
    clearTimeout(_fetchTasksTimer);
    _fetchTasksTimer = setTimeout(() => fetchTasks().catch(() => {}), 300);

    clearTimeout(_fetchGauntletTimer);
    _fetchGauntletTimer = setTimeout(() => fetchGauntlet().catch(() => {}), 300);
  }
}

// ---- wiring ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  // Tab navigation
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.view));
  });
  document.querySelectorAll(".sidebar-item").forEach((btn) => {
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

  // Gauntlet view
  document.getElementById("gauntlet-refresh-btn").addEventListener("click", () => fetchGauntlet().catch(console.error));
  document.getElementById("gauntlet-clear-focus-btn").addEventListener("click", () => {
    setGauntletTaskFocus(null);
    fetchGauntlet().catch(console.error);
  });

  // Control-plane views
  document.getElementById("work-items-refresh-btn").addEventListener("click", () => fetchWorkItems().catch(console.error));
  document.getElementById("approvals-refresh-btn").addEventListener("click", () => fetchApprovals().catch(console.error));
  document.getElementById("artifacts-refresh-btn").addEventListener("click", () => fetchArtifacts().catch(console.error));
  document.getElementById("graphs-refresh-btn").addEventListener("click", () => fetchTaskGraphs().catch(console.error));
  document.getElementById("checks-refresh-btn").addEventListener("click", () => fetchChecks().catch(console.error));

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
    document.getElementById("terminal-log").textContent = "(cleared)";
  });
  document.getElementById("terminal-clear-btn").addEventListener("click", () => {
    state.monitorLines.length = 0;
    document.getElementById("monitor-log").textContent = "(cleared)";
    document.getElementById("terminal-log").textContent = "(cleared)";
  });

  // Repos view
  document.getElementById("repos-refresh-btn").addEventListener("click", () => fetchFleet().catch(console.error));

  // Debug view
  document.getElementById("debug-clear-btn").addEventListener("click", () => {
    state.debugEvents.length = 0;
    document.getElementById("debug-log").textContent = "(cleared)";
  });

  // Command palette
  document.getElementById("command-palette-btn").addEventListener("click", openCommandPalette);
  document.getElementById("command-palette-input").addEventListener("input", (ev) => {
    renderCommandPalette(ev.target.value);
  });

  document.getElementById("command-palette-input").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      const first = document.querySelector("#command-palette-results button[data-command]");
      if (first) first.focus();
    }
  });

  document.getElementById("command-palette-results").addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      const next = ev.target.parentElement.nextElementSibling?.querySelector("button");
      if (next) next.focus();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      const prev = ev.target.parentElement.previousElementSibling?.querySelector("button");
      if (prev) {
        prev.focus();
      } else {
        document.getElementById("command-palette-input").focus();
      }
    }
  });

  document.getElementById("command-palette-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const active = document.activeElement;
    if (active && active.dataset.command) {
        runCommand(active.dataset.command);
    } else {
        const first = document.querySelector("#command-palette-results button[data-command]");
        if (first) runCommand(first.dataset.command);
    }
  });

  // Keyboard shortcut: digit keys switch primary tabs
  document.addEventListener("keydown", (ev) => {
    if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      openCommandPalette();
      return;
    }
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    const idx = parseInt(ev.key, 10) - 1;
    if (idx >= 0 && idx < viewOrder.length) {
      ev.preventDefault();
      switchView(viewOrder[idx]);
    }
  });

  // Touch swipe to navigate tabs (left/right swipe)
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
    const current = viewOrder.indexOf(state.currentView);
    if (current === -1) return;
    const next = dx < 0
      ? Math.min(current + 1, viewOrder.length - 1)
      : Math.max(current - 1, 0);
    if (next !== current) switchView(viewOrder[next]);
  }, { passive: true });

  // Handle ?view= URL param on load (PWA shortcut links)
  const viewParam = new URLSearchParams(location.search).get("view");
  if (viewParam && viewOrder.includes(viewParam)) switchView(viewParam);

  // Initial load
  fetchTasks().catch(console.error);
  fetchBackends().catch(console.error);
  fetchGauntlet().catch(console.error);
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
