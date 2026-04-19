# CLI reference

```
conductor [OPTIONS] COMMAND [ARGS]...
```

## Global options

- `-V, --version` — print version and exit.

## `conductor init`

Write a starter `conductor.yaml`.

```
conductor init [--path PATH] [--force]
```

## `conductor status`

Print the configured backends and repos.

```
conductor status [--config PATH]
```

## `conductor backends`

List every adapter registered in the current process. Useful for checking which optional SDKs are installed.

## `conductor health`

Probe every enabled backend for reachability. Exit code is non-zero if any backend fails.

```
conductor health [--config PATH]
```

## `conductor ask`

One-shot prompt for smoke-testing.

```
conductor ask "your prompt" [--backend NAME] [--model NAME] [--no-stream]
```

## `conductor cost`

Month-to-date spend, budget utilisation, per-backend breakdown.

```
conductor cost [--config PATH] [--ledger PATH]
```

## `conductor serve`

Start the daemon and mount the FastAPI app in the foreground. Suitable for a systemd `ExecStart=`.

```
conductor serve [--host HOST] [--port PORT] [--workers N]
```
