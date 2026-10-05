# Implementation Handoff

Keep this file current and concise. Replace instructional placeholders; do not append an unbounded transcript.

## Identity

- Repository: `D-sorganization/Maxwell_Daemon`
- Working directory: `C:\Users\diete\Repositories\_worktrees\Maxwell_Daemon-smoke-electron`
- Branch: `ci/1211-desktop-smoke-clock`
- Implementation commit: `SELF` — the commit containing this update; resolve with `git rev-parse HEAD`
- Pull request: `#1212`
- Governing issue: #1211

## Objective and Status

- Objective: stop the desktop launch smoke from billing the Electron binary download against the 180000 ms launch budget.
- Status: `in review`
- Root cause: Electron 44's `require("electron")` downloads the binary lazily ("Downloading Electron binary..."). `smoke-launch.js` read `performance.now()` before that `require`. In job 111478297475 (merge_group for #1205) the app was ready in 119 ms, but the measured wall time was 233102 ms.
- Fix: `resolveBinaryThenStartClock()` resolves the binary first, then starts the clock. The budget is unchanged.

## Files and Decisions

- Files changed:
  - `apps/desktop-electron/smoke-launch.js`: testable helpers; `main()` runs only when invoked as a script.
  - `apps/desktop-electron/test/smoke-launch.test.js`, plus an `npm test` script.
  - `apps/desktop-electron/README.md`
  - `tests/unit/test_desktop_electron_scaffold.py`
  - `SPEC.md` (change-log row), the development log, and this handoff.
- Key decision: fix the measurement in the script instead of adding a pre-download workflow step.
  - It covers the Windows leg too.
  - It needs no workflow change.
  - It avoids `actions/cache` uploads from persistent self-hosted runners, which `test_desktop_smoke_does_not_upload_persistent_runner_npm_cache` already guards against.
- The budget stays at 180000 ms; this is not a tolerance change.

## Validation

- `npm test` (node:test): 4 passed. Before the fix, it was RED with `Cannot find module 'electron'`, because importing the script resolved Electron eagerly.
- `pytest tests/unit/test_desktop_electron_scaffold.py tests/unit/test_ci_timeout_contract.py`: all passed.
- The new static check fails against `origin/main`'s `smoke-launch.js`, which contains `const launchStartedAt = performance.now();`.

## Blockers and Risks

- Blockers: `none`
- Risk: a slow download still lengthens the job, but it is bounded by the job's `timeout-minutes: 60`, not by the launch budget.

## Next Steps

1. Merge; confirm the next `Desktop launcher smoke (d-sorg-fleet)` run passes, with wall time close to app time.
2. On 2026-10-22, check for daemon usage and decide archive vs. keep (from #1177).

## Change Log

- `SELF` — desktop smoke clock starts after Electron binary resolution (#1211).
