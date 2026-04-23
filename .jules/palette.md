## 2024-05-24 - Keyboard Navigation for Command Palette
**Learning:** Adding `keydown` listeners for `ArrowUp`/`ArrowDown` coupled with `element.focus()` and `:focus-visible` styles is an effective pattern for building accessible, keyboard-navigable list widgets like command palettes in vanilla JS.
**Action:** Always check if custom drop-downs, search result lists, or palettes support arrow key navigation. If not, implement standard WAI-ARIA keyboard interaction patterns to allow power users and screen-reader users to interact smoothly.
