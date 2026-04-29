## 2026-04-29 - Prevent background DOM thrashing for inactive views
**Learning:** In vanilla JS SPAs built without virtual DOMs, updating complex elements (like long tables) for views that are hidden still causes significant garbage collection overhead and CPU cycles, which can block the main thread during high-frequency WebSocket updates.
**Action:** When views are hidden by CSS (`hidden=true` or `display: none`), manually check `if (state.currentView !== "viewName") return;` inside render loops, and force a render when the view is switched back to active.
