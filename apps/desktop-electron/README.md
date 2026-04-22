# Maxwell-Daemon Desktop

Electron shell for Maxwell-Daemon. It connects to the daemon HTTP API, shows fleet and task state, supports issue dispatch, keeps a cached snapshot for offline status, and wires native desktop affordances.

## Commands

```bash
npm install
npm start
npm run dist
```

The `dist` script is configured for DMG, MSI, AppImage, and Snap targets through `electron-builder`.
