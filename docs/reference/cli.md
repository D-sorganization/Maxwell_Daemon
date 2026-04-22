# CLI reference

```
maxwell-daemon [OPTIONS] COMMAND [ARGS]...
```

## Global options

- `-V, --version` — print version and exit.

## `maxwell-daemon init`

Write a starter `maxwell-daemon.yaml`.

```
maxwell-daemon init [--path PATH] [--force]
```

## `maxwell-daemon status`

Print the configured backends and repos.

```
maxwell-daemon status [--config PATH]
```

## `maxwell-daemon backends`

List every adapter registered in the current process. Useful for checking which optional SDKs are installed.

## `maxwell-daemon health`

Probe every enabled backend for reachability. Exit code is non-zero if any backend fails.

```
maxwell-daemon health [--config PATH]
```

## `maxwell-daemon ask`

One-shot prompt for smoke-testing.

```
maxwell-daemon ask "your prompt" [--backend NAME] [--model NAME] [--no-stream]
```

## `maxwell-daemon cost`

Month-to-date spend, budget utilisation, per-backend breakdown.

```
maxwell-daemon cost [--config PATH] [--ledger PATH]
```

## `maxwell-daemon memory`

Inspect the local markdown memory store or manage repo-carried memory
proposals inside a repository root.

```
maxwell-daemon memory status [--config PATH]
maxwell-daemon memory anneal [--config PATH]
maxwell-daemon memory repo list REPO_ROOT --repo-id OWNER/REPO [--include-superseded]
maxwell-daemon memory repo proposals REPO_ROOT
maxwell-daemon memory repo propose REPO_ROOT ENTRY_ID --repo-id OWNER/REPO --body TEXT --source TEXT --proposed-by TEXT --reason TEXT
maxwell-daemon memory repo accept REPO_ROOT PROPOSAL_ID --reviewer NAME
maxwell-daemon memory repo reject REPO_ROOT PROPOSAL_ID --reviewer NAME
maxwell-daemon memory repo snapshot REPO_ROOT --repo-id OWNER/REPO [--issue-number N]
```

## `maxwell-daemon task-graph`

Create and inspect typed sub-agent delivery graph definitions.

```
maxwell-daemon task-graph create WORK_ITEM_ID [--title TEXT] [--criterion TEXT] [--risk LEVEL] [--label LABEL] [--template NAME] [--graph-id ID] [--output PATH]
maxwell-daemon task-graph inspect PATH [--json]
```

`create` emits validated graph JSON or writes it to `--output`. `inspect`
validates a saved graph and shows dependency order, roles, required handoff
artifact kinds, output artifact kinds, and retry limits.

## `maxwell-daemon serve`

Start the daemon and mount the FastAPI app in the foreground. Suitable for a systemd `ExecStart=`.

```
maxwell-daemon serve [--host HOST] [--port PORT] [--workers N]
```
