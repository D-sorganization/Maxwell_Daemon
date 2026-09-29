# Implementation Handoff

Keep this file current and concise. Replace instructional placeholders; do not append an unbounded transcript.

## Identity

- Repository: `D-sorganization/Maxwell_Daemon`
- Working directory: `C:\Users\diete\Repositories\Maxwell_Daemon-worktrees\w-md-archmap`
- Branch: `claude/md-archmap-contract-0929`
- Implementation commit: `SELF` — the commit containing this update; resolve with `git rev-parse HEAD`
- Pull request: `#TBD`
- Governing issue/epic: none. This fixes the red `Architecture Map Contract` workflow on `main`, which has failed since 2026-09-22 (run 35759334790).

## Objective and Status

- Objective: make the focused architecture-map contract job run in its dependency-light environment.
- Status: `in review`
- Root cause: the job installs only `pytest pytest-asyncio pytest-timeout structlog`, which leaves two gaps.
  - `pyproject` addopts pass `--cov`, so `pytest-cov` is required.
  - `tests/conftest.py` imports the whole `maxwell_daemon` package, so the job fails with `ModuleNotFoundError: yaml`, then `pydantic`.
- Fix:
  - Install `pytest-cov`.
  - Run the focused test with `--confcutdir=tests/scripts`, so the package-wide conftest is not loaded.
- Previous handoff (#1177 maintenance mode) is complete: merged as #1178. The archive review stays due on 2026-10-22.

## Files and Decisions

- Files changed: `.github/workflows/architecture-map-contract.yml`, `SPEC.md` (change-log row), this handoff.
- Key decision: keep the job dependency-light instead of installing the full package. The contract test imports only `scripts.architecture_map_contract`.
- This is a workflow-only change, so it ships alone.

## Validation

- Clean venv with only the workflow's install list plus `pytest-cov`:
  - `python scripts/architecture_map_contract.py --path docs/architecture/C4.md`: PASS.
  - `pytest -o pythonpath=. -p no:cacheprovider --confcutdir=tests/scripts tests/scripts/test_architecture_map_contract.py`: 4 passed.
- Without `--confcutdir`, the same venv reproduces the CI failure: `ModuleNotFoundError: No module named 'yaml'`.

## Blockers and Risks

- Blockers: `none`

## Next Steps

1. Merge; confirm the next `Architecture Map Contract` run on main is green.
2. On 2026-10-22, check for daemon usage and decide archive vs. keep (from #1177).

## Change Log

- `SELF` — Architecture Map Contract job: pytest-cov + `--confcutdir=tests/scripts`.
