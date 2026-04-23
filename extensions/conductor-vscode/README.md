# CONDUCTOR for Maxwell-Daemon

This extension provides a VS Code control surface for a running Maxwell-Daemon API.

## Capabilities

- Activity Bar container with an Agents tree.
- Sidebar entries for available backends, active tasks, fleet repositories, and PR links.
- Command to dispatch a GitHub issue in `plan` or `implement` mode.
- Command to open a PR diff in VS Code's browser surface.
- Pseudo-terminal command that streams daemon task snapshots into the integrated terminal pane.

## Local Development

Open this folder in VS Code and run `Developer: Reload Window`, or package it with the VS Code extension tooling.

Configuration lives under `Maxwell CONDUCTOR`:

- `maxwellConductor.daemonUrl`
- `maxwellConductor.token`
- `maxwellConductor.defaultMode`
