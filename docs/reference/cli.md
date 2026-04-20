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

## `maxwell-daemon serve`

Start the daemon and mount the FastAPI app in the foreground. Suitable for a systemd `ExecStart=`.

```
maxwell-daemon serve [--host HOST] [--port PORT] [--workers N]
```
