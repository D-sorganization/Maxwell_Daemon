## 2024-05-18 - Thundering Herds on WebSocket Events
**Learning:** Un-debounced WebSocket event handlers calling synchronous fetches (`fetchTaskDetail`, `fetchTasks`) for every event with a task ID can lead to a thundering herd of redundant API requests and UI re-renders, especially when agents produce a flurry of actions.
**Action:** Always debounce global UI updates and list fetches tied to rapid event streams using timers (e.g. `setTimeout`), ensuring rapid sequential updates are batched into a single UI and network operation.
