# Quick start

## Install

```bash
pip install conductor-agents
```

## Initialise a config

```bash
conductor init
```

This writes a starter `conductor.yaml` to `~/.config/conductor/conductor.yaml` with Claude and Ollama backends pre-wired.

## Point it at an API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

CONDUCTOR reads `${ENV_VAR}` substitutions at load time, so the key stays out of the YAML.

## Verify

```bash
conductor status    # show configured backends and repos
conductor health    # probe every backend for reachability
```

## Send a one-shot prompt

```bash
conductor ask "explain the difference between slots and dict-based dataclasses"
```

## Run the daemon + API

```bash
conductor serve --port 8080
```

Then:

```bash
curl -s localhost:8080/health | jq
curl -s -XPOST localhost:8080/api/v1/tasks \
     -H 'content-type: application/json' \
     -d '{"prompt":"hi there"}'
```
