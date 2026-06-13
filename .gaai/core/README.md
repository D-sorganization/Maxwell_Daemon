# .gaai/ — GAAI Framework (v2.6.3)

## Provenance

> **Vendored from [`Fr-e-d/GAAI-framework`](https://github.com/Fr-e-d/GAAI-framework) @ v2.6.3.**
> This tree is **not** first-party to Maxwell-Daemon. The files under
> `.gaai/core/` (install scripts, git hooks such as
> `pre-push.d/01-block-production.sh`, the skills library, and
> `specialists.registry.yaml`) originate upstream and are kept in sync by the
> post-commit hook described below.

**Why it is committed rather than installed at setup time:** the post-commit sync
hook treats this checkout as the *editing surface* for upstream `core/` — local
edits are auto-contributed back to the OSS framework. Removing it from version
control would break that bidirectional sync. Because the tree is executable
framework code (including git hooks), treat upstream bumps as dependency
updates: review the diff when `v2.6.x` changes, and pin the version recorded in
this header.

**Update path:** bump the upstream version, let the sync hook reconcile
`core/`, then update the version in this README's title and the Provenance note.
Maxwell-Daemon's own integration point is `maxwell_daemon/gaai/loader.py`
(first-party), which only *reads* metadata from this tree.

## Directory Structure

```
.gaai/
├── core/          ← Framework (auto-synced to OSS via post-commit hook)
│   └── README.md  ← This file
└── project/       ← Project-specific data (memory, backlog, artefacts, custom skills)
```

- `core/` changes are **automatically contributed to OSS** on every commit (via post-commit hook → PR → auto-merge)
- `project/` is **local only** — never synced to OSS

---

## Framework Sync (Automatic)

When you commit changes to `.gaai/core/`, a post-commit hook automatically:
1. Detects `.gaai/core/` was modified
2. Clones the OSS repo (shallow)
3. Replaces `core/` with your local version
4. Creates a PR on `Fr-e-d/GAAI-framework`
5. Schedules auto-merge

**You don't need to do anything.** The sync is transparent and non-blocking.

Setup: `git config core.hooksPath .githooks` (done by `install-hooks.sh`).
Logs: `.github/.sync-log`.

---

## Optional: Autonomous Delivery

If your project uses git with a `staging` branch, the **Delivery Daemon** can automate everything:

1. One-time setup: `bash .gaai/core/scripts/daemon-setup.sh`
2. `/gaai-daemon` — starts the daemon (3 concurrent slots, auto-opens monitoring)
3. `/gaai-daemon --stop` — graceful shutdown

The daemon polls for `refined` stories and delivers them in parallel — no human in the loop.
Full reference: see `GAAI.md` → "Branch Model & Automation".

> **Tested on:** macOS (Apple Silicon). Linux and WSL (Windows) are expected to work but not yet validated — issues and feedback welcome.

---

## New Projects: Install GAAI

```bash
# From the GAAI-framework repo
bash /tmp/gaai/install.sh --target . --tool claude-code --yes
```
