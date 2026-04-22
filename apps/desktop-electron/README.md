# Maxwell-Daemon Desktop

Electron shell for Maxwell-Daemon. It connects to the daemon HTTP API, shows
fleet and task state, supports issue dispatch, keeps a cached snapshot for
offline status, and wires native desktop affordances.

The tray tooltip, dock/taskbar badge, and taskbar progress indicator all mirror
the same active-task and connectivity snapshot so the desktop shell remains
useful when the main window is hidden.

Update checks stream their lifecycle into the renderer. When an update is ready,
the shell shows an install action and sends a native desktop notification.

The command palette opens from the app button or Cmd/Ctrl+K and runs common
desktop actions such as refresh, dispatch focus, and update checks.

## Commands

```bash
npm install
npm start
npm run dist
```

The `dist` script is configured for DMG, MSI, AppImage, and Snap targets through `electron-builder`.
