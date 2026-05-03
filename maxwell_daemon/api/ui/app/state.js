// Maxwell-Daemon UI — Shared state module.
// Centralized application state for the vanilla JS UI.

export const state = {
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

export const viewOrder = [
  "tasks", "fleet", "gauntlet", "work-items", "approvals", "artifacts",
  "graphs", "checks", "repos", "history", "cost", "monitor", "debug",
];

export const commands = [
  { id: "view.tasks", title: "Show Tasks", detail: "Open the task editor", run: () => import("./views.js").then(m => m.switchView("tasks")) },
  { id: "view.fleet", title: "Show Fleet", detail: "Open fleet overview", run: () => import("./views.js").then(m => m.switchView("fleet")) },
  { id: "view.gauntlet", title: "Show Gauntlet", detail: "Open gate and critic status", run: () => import("./views.js").then(m => m.switchView("gauntlet")) },
  { id: "view.work-items", title: "Show Work Items", detail: "Open work-item queue", run: () => import("./views.js").then(m => m.switchView("work-items")) },
  { id: "view.approvals", title: "Show Approvals", detail: "Open action approval queue", run: () => import("./views.js").then(m => m.switchView("approvals")) },
  { id: "view.artifacts", title: "Show Artifacts", detail: "Open artifact browser", run: () => import("./views.js").then(m => m.switchView("artifacts")) },
  { id: "view.graphs", title: "Show Task Graphs", detail: "Open sub-agent graph runs", run: () => import("./views.js").then(m => m.switchView("graphs")) },
  { id: "view.checks", title: "Show Checks", detail: "Open validation checks", run: () => import("./views.js").then(m => m.switchView("checks")) },
  { id: "view.repos", title: "Show Repositories", detail: "Open repository dashboard", run: () => import("./views.js").then(m => m.switchView("repos")) },
  { id: "view.monitor", title: "Show Daemon Logs", detail: "Open live monitor", run: () => import("./views.js").then(m => m.switchView("monitor")) },
  { id: "view.history", title: "Show History", detail: "Open completed work timeline", run: () => import("./views.js").then(m => m.switchView("history")) },
  { id: "view.cost", title: "Show Cost", detail: "Open cost analytics", run: () => import("./views.js").then(m => m.switchView("cost")) },
  { id: "task.new", title: "Dispatch New Task", detail: "Open task dispatch dialog", run: () => import("./dialog.js").then(m => m.openNewTaskDialog()) },
  { id: "data.refresh", title: "Refresh Dashboard", detail: "Reload task, cost, and fleet data", run: () => import("./api.js").then(m => m.refreshAll()) },
];

// Empty object constant to avoid allocations in frequently called handlers
export const EMPTY_OBJ = Object.freeze({});