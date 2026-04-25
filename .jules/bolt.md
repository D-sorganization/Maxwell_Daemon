## 2024-05-18 - Thundering Herds on WebSocket Events
**Learning:** Un-debounced WebSocket event handlers calling synchronous fetches (`fetchTaskDetail`, `fetchTasks`) for every event with a task ID can lead to a thundering herd of redundant API requests and UI re-renders, especially when agents produce a flurry of actions.
**Action:** Always debounce global UI updates and list fetches tied to rapid event streams using timers (e.g. `setTimeout`), ensuring rapid sequential updates are batched into a single UI and network operation.

## 2024-10-24 - Layout Thrashing on WebSocket Event Streams
**Learning:** Synchronous layout thrashing from rapidly updating DOM properties like `el.scrollTop = el.scrollHeight` and `el.textContent` during rapid WebSocket event streams causes severe UI lag and blocks the main thread.
**Action:** Always use `requestAnimationFrame` to batch visual DOM manipulations (especially layout triggers like `scrollHeight`) resulting from rapid event streams, ensuring at most one layout calculation per visual frame.

## 2024-10-25 - Redundant Data Fetching on Event Streams
**Learning:** Fetching heavy detailed state for a resource (e.g. `fetchTaskDetail`) on every background event for that resource—regardless of whether it's currently visible in the UI—causes redundant API requests and wasteful re-renders. A global list fetch often handles the high-level status updates needed for hidden resources.
**Action:** Always verify if a resource is actively being viewed (e.g. `state.selected === id`) before firing off detailed fetch operations in response to background event streams.

## 2024-10-26 - O(N) Array Allocations for Map Aggregates
**Learning:** Spreading `Map.values()` into arrays repeatedly (e.g., `[...state.allTasks.values()].filter(...).length` or `.reduce(...)`) just to compute aggregates causes unnecessary O(N) memory allocations and garbage collection pressure, leading to UI frame drops when updates happen rapidly (like via WebSockets).
**Action:** Always compute aggregate values over Map or Set structures using a single `for...of` loop or iterator instead of creating intermediate arrays.

## 2024-04-25 - [Frontend Performance: Object allocations in render loops]
**Learning:** Returning new objects inside replacer functions (e.g. `replace(..., () => ({}))`) and relying on object literals for lookups inside frequently called functions (e.g., sort comparators or formatting methods) creates new object instances on every call, causing memory churn and GC pressure.
**Action:** Extract static mapping objects (like `ESCAPE_MAP` or `GATE_STATUS_RANK`) outside of the functions that use them to prevent unnecessary allocations on every execution.
