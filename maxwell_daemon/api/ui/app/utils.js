// Maxwell-Daemon UI — Utility functions module.
// Common helpers for formatting, escaping, and sorting.

// ⚡ Bolt: Extracted to avoid object allocation on every character replacement
const ESCAPE_MAP = {
  "&": "&", "<": "<", ">": ">", '"': """, "'": "&#39;",
};

// ⚡ Bolt: Extracted to avoid function allocation on every escapeHtml call
const getEscapeChar = (c) => ESCAPE_MAP[c];

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, getEscapeChar);
}

export function fmtUsd(n) { return `$${(n || 0).toFixed(4)}`; }
export function fmtUsdShort(n) { return `$${(n || 0).toFixed(2)}`; }

// ⚡ Bolt: Cache intl.DateTimeFormat instances to avoid extremely slow
// initialization and object allocations on every format call, especially
// important during rapid WebSocket event streams where dates are heavily formatted.
const FORMATTER_LONG = new Intl.DateTimeFormat(undefined, { dateStyle: "short", timeStyle: "medium" });
const FORMATTER_SHORT = new Intl.DateTimeFormat(undefined, { timeStyle: "short" });

export function fmtTs(iso) {
  if (!iso) return "—";
  return FORMATTER_LONG.format(new Date(iso));
}

export function fmtTsShort(iso) {
  if (!iso) return "—";
  return FORMATTER_SHORT.format(new Date(iso));
}

// ⚡ Bolt: Extracted to avoid object allocation on every sort comparison
const GATE_STATUS_RANK = {
  failed: 0,
  blocked: 1,
  waived: 2,
  running: 3,
  pending: 4,
  passed: 5,
  skipped: 6,
};

export function gateStatusRank(status) {
  return GATE_STATUS_RANK[status] ?? 99;
}

export function findingSeverityRank(severity) {
  const normalized = String(severity || "").toLowerCase();
  if (normalized === "blocker" || normalized === "p1") return 0;
  if (normalized === "warning") return 1;
  if (normalized === "note" || normalized === "p2") return 2;
  return 3;
}

export function fmtBytes(n) {
  const value = Number(n || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function fmtDurationSeconds(value) {
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

export function shortId(value) {
  const s = String(value || "");
  return s.length > 12 ? `${s.slice(0, 12)}…` : s;
}

// Sort functions
export const sortBackendCostDesc = (a, b) => b[1] - a[1];
export const sortTaskCostDesc = (a, b) => (b.cost_usd || 0) - (a.cost_usd || 0);
export const sortTaskCreatedAtDesc = (a, b) => a.created_at < b.created_at ? 1 : (a.created_at > b.created_at ? -1 : 0);
export const sortGateStatusAsc = (a, b) => gateStatusRank(a.status) - gateStatusRank(b.status);
export const sortCriticFindings = (a, b) => {
  const severityCmp = findingSeverityRank(a.severity) - findingSeverityRank(b.severity);
  if (severityCmp !== 0) return severityCmp;
  return String(a.message || "").localeCompare(String(b.message || ""));
};
export const sortHistoryItemDesc = (a, b) => {
  const aT = a.finished_at || a.created_at;
  const bT = b.finished_at || b.created_at;
  // ⚡ Bolt: Fast ISO 8601 sort using string operators.
  return aT < bT ? 1 : (aT > bT ? -1 : 0);
};

// DOM helpers
export function setTableMessage(tbodyId, colspan, message) {
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

// Control plane helper
export function controlPlaneByTaskId(taskId, controlPlane) {
  return controlPlane.find((item) => item.task_id === taskId) || null;
}