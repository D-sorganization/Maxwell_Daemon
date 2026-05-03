// Maxwell-Daemon UI — API module.
// Handles all REST API calls and WebSocket connections.

import { state } from "./state.js";
import { renderTasks, renderGauntlet } from "./views.js";
import { updateStatusResources } from "./views.js";

const authToken = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("maxwell-daemon.token");

export function getHeaders() {
  return authToken ? { authorization: `Bearer ${authToken}` } : {};
}

export async function fetchTasks() {
  const params = new URLSearchParams();
  const status = document.getElementById("status-filter").value;
  if (status) params.set("status", status);
  params.set("limit", "100");
  const r = await fetch(`/api/v1/tasks?${params}`, { headers: getHeaders() });
  if (!r.ok) throw new Error(`tasks list: ${r.status}`);
  const list = await r.json();
  state.tasks.clear();
  for (const t of list) state.tasks.set(t.id, t);

  // Always fetch an unfiltered snapshot for cost analytics so that the cost
  // dashboard is not affected by the Tasks tab's status filter (#235).
  if (status) {
    const allParams = new URLSearchParams({ limit: "500" });
    const allR = await fetch(`/api/v1/tasks?${allParams}`, { headers: getHeaders() });
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

export async function fetchBackends() {
  const r = await fetch("/api/v1/backends", { headers: getHeaders() });
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

export async function fetchCost() {
  const r = await fetch("/api/v1/cost", { headers: getHeaders() });
  if (!r.ok) return;
  const body = await r.json();
  const el = document.getElementById("cost-summary");
  el.textContent = `MTD ${fmtUsdShort(body.month_to_date_usd)}`;
  updateStatusResources(body.month_to_date_usd);
  return body;
}

export async function refreshAll() {
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

// Re-export fmtUsdShort for use in this module
function fmtUsdShort(n) { return `$${(n || 0).toFixed(2)}`; }

// Placeholder functions - will be imported from views.js
async function fetchFleet() {
  const r = await fetch("/api/v1/fleet", { headers: getHeaders() });
  if (!r.ok) return;
  const fleetData = await r.json();
  // Store for views to use
  window._fleetData = fleetData;
  // Render if view is active
  if (state.currentView === "fleet") {
    const { renderFleet } = await import("./views.js");
    renderFleet(fleetData);
  }
  if (state.currentView === "repos") {
    const { renderRepos } = await import("./views.js");
    renderRepos(fleetData);
  }
}

async function renderHistory() {
  const { renderHistory } = await import("./views.js");
  renderHistory();
}

async function renderCostTasks() {
  const { renderCostTasks } = await import("./views.js");
  renderCostTasks();
}