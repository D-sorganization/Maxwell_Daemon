# Tasks CLI

Manage the daemon's task queue from the command line. All commands talk to the running daemon via its REST API.

## `maxwell-daemon tasks list`

```
maxwell-daemon tasks list [--status STATUS] [--kind KIND] [--repo REPO] [--limit N]
                     [--daemon-url URL] [--auth-token TOKEN]
```

Prints newest-first. Filters stack:

```bash
# Every failed implement-mode task against the upstream repo:
maxwell-daemon tasks list --status failed --kind issue --repo D-sorg/upstream
```

## `maxwell-daemon tasks show ID`

```
maxwell-daemon tasks show TASK_ID [--daemon-url URL] [--auth-token TOKEN]
```

Prints every non-null field of a single task, including PR URL, test command, cost, timestamps.

## `maxwell-daemon tasks cancel ID`

```
maxwell-daemon tasks cancel TASK_ID [--daemon-url URL] [--auth-token TOKEN]
```

Sets status to `cancelled` for queued tasks. Returns non-zero exit code for running/completed/failed tasks — they can't be rolled back, only prevented from starting.

## Authentication

Set `MAXWELL_API_TOKEN` in the environment or pass `--auth-token`. The token is the same one configured under `api.auth_token` in `maxwell-daemon.yaml`.

## Remote daemons

Point `--daemon-url` or `MAXWELL_DAEMON_URL` at any reachable daemon:

```bash
maxwell-daemon tasks list --daemon-url https://maxwell-daemon.internal:8080
```

SSH tunnel into a remote host to stay off the public internet:

```bash
ssh -L 8080:localhost:8080 fleet-host &
maxwell-daemon tasks list
```
