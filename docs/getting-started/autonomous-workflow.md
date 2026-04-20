# Autonomous workflow

Maxwell-Daemon can create GitHub issues, queue them for the daemon, and have the daemon draft pull requests against them — so you can keep typing new issues while the agent grinds through the backlog in the background.

## Prerequisites

- `gh` CLI installed and authenticated (`gh auth login`).
- A running daemon (`maxwell-daemon serve`).
- At least one configured LLM backend with a working API key.

## Create an issue

```bash
maxwell-daemon issue new owner/repo "Fix the parser" \
    --body "Reproduce: run X, get Y, expected Z."
```

Add labels:

```bash
maxwell-daemon issue new owner/repo "Title" -b "body" -l bug -l p1
```

## Dispatch the daemon against an issue

Plan mode — safe, no code changes, opens a draft PR seeded with the agent's plan:

```bash
maxwell-daemon issue dispatch owner/repo 42 --mode plan
```

Implement mode — agent produces a unified diff, workspace applies it, pushes a branch, and opens a draft PR for human review:

```bash
maxwell-daemon issue dispatch owner/repo 42 --mode implement
```

## One-shot: create and dispatch

```bash
maxwell-daemon issue new owner/repo "Fix it" --body "..." --dispatch --mode plan
```

## Keep adding while the daemon works

New issues can be created and dispatched at any time — the daemon's async task queue processes them without blocking submission. You can fire off five issues at once and the daemon will work through them with however many workers are configured.

## Watch progress

Stream live events:

```bash
# via websocat (install separately)
websocat ws://localhost:8080/api/v1/events
```

Or poll:

```bash
curl -s localhost:8080/api/v1/tasks | jq '.[-5:]'
```

## Modes — when to pick which

| Mode        | Writes code? | Use when                                                     |
|-------------|:------------:|-------------------------------------------------------------|
| `plan`      | No           | Scoping new work, triaging a pile of issues, or testing the agent on a new backend |
| `implement` | Yes (draft)  | Well-scoped bugs with clear acceptance criteria              |

PRs are always opened as **drafts**. A human un-drafts them after review — Maxwell-Daemon never auto-merges.

## REST equivalents

All CLI commands go through the REST API. Useful for CI/CD pipelines, cron, or external dashboards:

```bash
# Create
curl -X POST localhost:8080/api/v1/issues \
     -H 'content-type: application/json' \
     -d '{"repo":"owner/repo","title":"fix","body":"...","dispatch":true,"mode":"plan"}'

# Dispatch existing
curl -X POST localhost:8080/api/v1/issues/dispatch \
     -H 'content-type: application/json' \
     -d '{"repo":"owner/repo","number":42,"mode":"plan"}'

# List open issues
curl -s "localhost:8080/api/v1/issues/owner/repo?state=open&limit=50" | jq
```

## Safety model

- PRs are always drafts — human review is required.
- `implement` mode only runs when the LLM returns a non-empty, valid unified diff.
- Every git invocation goes through `asyncio.create_subprocess_exec` with explicit argv — no shell, no injection.
- Repo strings are regex-validated before they reach any subprocess.
- The issue-plan prompt instructs the LLM to prefer small, surgical changes and to return an empty diff if unsure.
