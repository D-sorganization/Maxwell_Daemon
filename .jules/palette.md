## 2024-05-24 - Keyboard Navigation for Command Palette
**Learning:** Adding `keydown` listeners for `ArrowUp`/`ArrowDown` coupled with `element.focus()` and `:focus-visible` styles is an effective pattern for building accessible, keyboard-navigable list widgets like command palettes in vanilla JS.
**Action:** Always check if custom drop-downs, search result lists, or palettes support arrow key navigation. If not, implement standard WAI-ARIA keyboard interaction patterns to allow power users and screen-reader users to interact smoothly.

## 2024-05-25 - Prevent double submission via disabled state
**Learning:** Adding `disabled` states to submit buttons when an async request is triggered, coupled with appropriate styling (`opacity`, `cursor: not-allowed`), prevents unintended double-submissions and provides valuable feedback that the system is processing the request. A `finally` block should always be used to restore the button to an enabled state regardless of whether the request succeeds or fails.
**Action:** When reviewing or creating forms, ensure submit buttons have visual and logical disabled states during async operations. Ensure CSS styles natively support `button:disabled`.

## 2024-04-28 - Contextual ARIA Labels for Table Actions
**Learning:** Generic button text like "cancel" or "review" in tables becomes ambiguous when a screen reader navigates to them out of context. The `role="status"` and `aria-live="polite"` attributes are necessary for dynamic status span elements to be announced to screen readers.
**Action:** Always provide descriptive `aria-label` attributes on table buttons (e.g., `aria-label="Cancel task 123"`) and use proper live region attributes for dynamic status elements. Ensure destructive actions have native or custom confirmation dialogs.
