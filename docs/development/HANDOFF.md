# Implementation Handoff

Keep this file current and concise. Replace instructional placeholders; do not append an unbounded transcript.

## Identity

- Repository: `D-sorganization/Maxwell_Daemon`
- Working directory: `C:\Users\diete\Repositories\Maxwell_Daemon-worktrees\maintenance-notice`
- Branch: `docs/1177-maintenance-mode`
- Baseline commit: `b200937` (origin/main, `chore(deps): update uv.lock (#1176)`)
- Implementation commit: `SELF` — the commit containing this update; resolve with `git rev-parse HEAD`
- Pull request: `not created` (draft PR to be opened after push)
- Governing issue/epic: `#1177`; epic `D-sorganization/Runner_Dashboard#1192`; provider registry `D-sorganization/Runner_Dashboard#1193`

## Objective and Status

- Objective: Declare maintenance mode (daemon unused in the fleet since June 2026) and defer fleet execution to the Runner Dashboard Staff Hub; keep Maxwell as an optional `maxwell` provider behind the dashboard's `/api/maxwell/*` proxy; archive review 2026-10-22.
- Status: `ready for review`
- Completed: README maintenance notice, AGENTS.md status paragraph, C4 change-log row, DL-#1177 entry, this handoff.
- Remaining: Merge the draft PR; revisit archive decision on 2026-10-22.

## Files and Decisions

- Files changed: `README.md` (notice above "Sibling repos"), `AGENTS.md` (Status section above sibling-repos boundaries), `docs/architecture/C4.md` (change-log row), `docs/development/DEVELOPMENT_LOG.md` (DL-#1177), `docs/development/HANDOFF.md` (new, from RM template).
- Key decisions: docs-only; no code, config, or HTTP-contract changes. The `/api/*` surface stays append-only and unchanged so the dashboard proxy keeps working.
- User-owned or unrelated worktree changes: `none observed`

## Validation

- `python scripts/architecture_map_contract.py --path docs/architecture/C4.md` — see DEVELOPMENT_LOG / PR checks
- `pytest -o pythonpath=. -p no:cacheprovider tests/scripts/test_architecture_map_contract.py -q` — see PR checks
- `pre-commit run --files <changed files>` — see PR checks

## Blockers and Risks

- Blockers: `none`
- Risks/assumptions: Assumes Runner_Dashboard#1193 registers `maxwell` as an optional provider; if that lands differently, update the README wording.

## Next Steps

1. Merge the draft PR for #1177 once the Architecture Map Contract gate is green.
2. On 2026-10-22, check for daemon usage and decide archive vs. keep.

## Change Log

- `SELF` — Created handoff for #1177 maintenance-mode docs change.
