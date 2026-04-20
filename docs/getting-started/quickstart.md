# Quick start

## Install

```bash
pip install maxwell-daemon
```

## Initialise a config

```bash
maxwell-daemon init
```

This writes a starter `maxwell-daemon.yaml` to `~/.config/maxwell-daemon/maxwell-daemon.yaml` with Claude and Ollama backends pre-wired.

## Point it at an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Maxwell-Daemon reads `${ENV_VAR}` substitutions at load time, so the key stays out of the YAML.

## Verify

```bash
maxwell-daemon status    # show configured backends and repos
maxwell-daemon health    # probe every backend for reachability
```

## Send a one-shot prompt

```bash
maxwell-daemon ask "explain the difference between slots and dict-based dataclasses"
```

## Run the daemon + API

```bash
maxwell-daemon serve --port 8080
```

Then:

```bash
curl -s localhost:8080/health | jq
curl -s -XPOST localhost:8080/api/v1/tasks \
     -H 'content-type: application/json' \
     -d '{"prompt":"hi there"}'
```
