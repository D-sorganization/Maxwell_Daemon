// Maxwell-Daemon UI — State persistence module.
// Handles localStorage and sessionStorage for UI state.

const PERSISTENCE_KEYS = {
  sidebarScroll: "maxwell-sidebar-scroll",
  statusFilter: "maxwell-status-filter",
  historyFilter: "maxwell-history-filter",
  currentView: "maxwell-current-view",
};

function safeGetStorage() {
  try {
    return { localStorage: window.localStorage, sessionStorage: window.sessionStorage };
  } catch (_) {
    return { localStorage: null, sessionStorage: null };
  }
}

export function saveScrollPosition(view, scrollTop) {
  const { sessionStorage } = safeGetStorage();
  if (!sessionStorage) return;
  try {
    sessionStorage.setItem(`${PERSISTENCE_KEYS.sidebarScroll}-${view}`, String(scrollTop));
  } catch (_) { /* sessionStorage may be unavailable */ }
}

export function restoreScrollPosition(view) {
  const { sessionStorage } = safeGetStorage();
  if (!sessionStorage) return null;
  try {
    const saved = sessionStorage.getItem(`${PERSISTENCE_KEYS.sidebarScroll}-${view}`);
    return saved !== null ? Number(saved) : null;
  } catch (_) {
    return null;
  }
}

export function saveStatusFilter(value) {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return;
  try {
    localStorage.setItem(PERSISTENCE_KEYS.statusFilter, value);
  } catch (_) { /* localStorage may be unavailable */ }
}

export function restoreStatusFilter() {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return null;
  try {
    return localStorage.getItem(PERSISTENCE_KEYS.statusFilter);
  } catch (_) {
    return null;
  }
}

export function saveHistoryFilter(value) {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return;
  try {
    localStorage.setItem(PERSISTENCE_KEYS.historyFilter, value);
  } catch (_) { /* localStorage may be unavailable */ }
}

export function restoreHistoryFilter() {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return null;
  try {
    return localStorage.getItem(PERSISTENCE_KEYS.historyFilter);
  } catch (_) {
    return null;
  }
}

export function saveCurrentView(view) {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return;
  try {
    localStorage.setItem(PERSISTENCE_KEYS.currentView, view);
  } catch (_) { /* localStorage may be unavailable */ }
}

export function restoreCurrentView(defaultValue) {
  const { localStorage } = safeGetStorage();
  if (!localStorage) return defaultValue;
  try {
    const saved = localStorage.getItem(PERSISTENCE_KEYS.currentView);
    return saved || defaultValue;
  } catch (_) {
    return defaultValue;
  }
}

export function applyPersistedScroll(view) {
  const scrollPosition = restoreScrollPosition(view);
  if (scrollPosition !== null) {
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) {
      sidebar.scrollTop = scrollPosition;
    }
  }
}

export function initPersistedState() {
  const savedStatusFilter = restoreStatusFilter();
  if (savedStatusFilter !== null) {
    const statusFilterEl = document.getElementById("status-filter");
    if (statusFilterEl) {
      statusFilterEl.value = savedStatusFilter;
    }
  }
  const savedHistoryFilter = restoreHistoryFilter();
  if (savedHistoryFilter !== null) {
    const historyFilterEl = document.getElementById("history-filter");
    if (historyFilterEl) {
      historyFilterEl.value = savedHistoryFilter;
    }
  }
}