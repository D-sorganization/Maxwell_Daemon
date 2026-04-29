## 2024-05-24 - Keyboard Navigation for Command Palette
**Learning:** Adding `keydown` listeners for `ArrowUp`/`ArrowDown` coupled with `element.focus()` and `:focus-visible` styles is an effective pattern for building accessible, keyboard-navigable list widgets like command palettes in vanilla JS.
**Action:** Always check if custom drop-downs, search result lists, or palettes support arrow key navigation. If not, implement standard WAI-ARIA keyboard interaction patterns to allow power users and screen-reader users to interact smoothly.

## 2024-05-25 - Prevent double submission via disabled state
**Learning:** Adding `disabled` states to submit buttons when an async request is triggered, coupled with appropriate styling (`opacity`, `cursor: not-allowed`), prevents unintended double-submissions and provides valuable feedback that the system is processing the request. A `finally` block should always be used to restore the button to an enabled state regardless of whether the request succeeds or fails.
**Action:** When reviewing or creating forms, ensure submit buttons have visual and logical disabled states during async operations. Ensure CSS styles natively support `button:disabled`.

## 2026-04-28 - Empty states for UI call-to-action discoverability
**Learning:** When displaying dynamic tables or lists (like task lists) that are empty, using a completely blank table is an anti-pattern. Providing an explicit empty state describing why it is empty or what action to take (e.g. "Press 'N' to dispatch your first task") significantly improves call-to-action discoverability and reduces user confusion.
**Action:** Always provide contextual empty states for tables, lists, or queries, ideally coupling them with a shortcut or button that helps the user populate the list.

## 2024-04-28 - Contextual ARIA Labels for Table Actions
**Learning:** Generic button text like "cancel" or "review" in tables becomes ambiguous when a screen reader navigates to them out of context. The `role="status"` and `aria-live="polite"` attributes are necessary for dynamic status span elements to be announced to screen readers.
**Action:** Always provide descriptive `aria-label` attributes on table buttons (e.g., `aria-label="Cancel task 123"`) and use proper live region attributes for dynamic status elements. Ensure destructive actions have native or custom confirmation dialogs.

## 2024-04-29 - Added ARIA labels to action approval buttons
**Learning:** Tables often contain repetitive action buttons ("Approve", "Reject") in every row. Screen reader users navigating by interactive elements (e.g., using "B" in NVDA or JAWS to jump between buttons) will just hear "Approve, button", "Reject, button" without the context of the row. By appending the row's specific summary to the `aria-label` (e.g., "Approve: Update deployment script"), the interface becomes significantly more accessible and less confusing for non-visual users.
**Action:** When creating repetitive list or table actions, always ensure the button's accessible name includes the context of the item it affects, either via `aria-label` or by visually hiding the context within the button text.
