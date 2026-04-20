// Maxwell-Daemon web UI — vanilla JS, no build step, no framework.
// Talks to the same REST + WebSocket endpoints the CLI uses.

const authToken = new URLSearchParams(location.search).get("token")
  || localStorage.getItem("maxwell-daemon.token");

const headers = () => authToken ? { authorization: `Bearer ${authToken}` } : {};

const state = {
  tasks: new Map(),           // id -> task object
  selected: null,             // currently-shown task id
  testOutput: new Map(),      // task id -> accumulated text
};

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
  el.textContent = `MTD $${body.month_to_date_usd.toFixed(2)}`;
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
  const sorted = [...state.tasks.values()].sort(
    (a, b) => b.created_at.localeCompare(a.created_at)
  );
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
      <td>$${(t.cost_usd || 0).toFixed(4)}</td>
      <td>${pr}</td>
      <td>${cancel}</td>
    `;
    tr.addEventListener("click", (ev) => {
      if (ev.target.dataset.cancel) return;  // handled below
      fetchTaskDetail(t.id);
    });
    tbody.appendChild(tr);
  }

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
  document.getElementById("detail-title").textContent =
    `Task ${task.id}`;
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
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
    handleEvent(evt);
  });
}

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
    // Any task-lifecycle event triggers a refresh of that task row.
    fetchTaskDetail(p.id).catch(() => {});
    fetchTasks().catch(() => {});
  }
}

// ---- wiring ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("refresh-btn").addEventListener("click", fetchTasks);
  document.getElementById("status-filter").addEventListener("change", fetchTasks);
  document.getElementById("detail-close").addEventListener("click", () => {
    document.getElementById("detail-card").hidden = true;
    state.selected = null;
  });

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
