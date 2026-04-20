# Maxwell-Daemon

> **Multi-backend autonomous code agent orchestrator** — use any LLM, from any provider, across any fleet of machines.

Maxwell-Daemon is an open-source orchestration platform for autonomous coding agents. It lets one person (or a small team) run a fleet of agents that discover, implement, and deliver work across many repositories — and it's **backend-agnostic** so you can mix expensive frontier models with cheap local ones on the same task queue.

**Status:** 🚧 Alpha — under active development. See the [roadmap](https://github.com/D-sorganization/Maxwell-Daemon/issues).

## Why Maxwell-Daemon??

The existing autonomous-agent tools are locked to one vendor and one machine. Maxwell-Daemon solves both:

- **Resource-agnostic.** Claude, GPT-4o, Gemini, Llama via Ollama, any OpenAI-compatible endpoint — all through one interface. Your Claude subscription, your OpenAI key, and your local RTX 4090 can all serve the same job queue.
- **Built for the little guy.** Route the cheap tasks to local models, the hard ones to Claude Opus, and stop burning tokens on work a 7B model could handle.
- **Fleet-native.** Run on one laptop, five boxes in your basement, or a cloud cluster. Same config, same CLI, same dashboard.
- **Cost-aware by default.** Every request is metered, every repo has a budget, every backend has a price. When you hit 90% of your monthly cap, Maxwell-Daemon knows.

## Quick Start

```bash
pip install -e ".[dev]"
maxwell-daemon init
export ANTHROPIC_API_KEY=sk-ant-...
maxwell-daemon health
maxwell-daemon ask "Explain what this repo does"
```

## Architecture

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│  CLI / GUI /    │   │  REST / gRPC    │   │  VS Code Ext    │
│  Web Dashboard  │   │  API            │   │                 │
└────────┬────────┘   └────────┬────────┘   └────────┬────────┘
         │                     │                     │
         └─────────────────────┴─────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Maxwell-Daemon daemon  │
                    │  - Task queue       │
                    │  - Backend router   │
                    │  - Cost ledger      │
                    └──────────┬──────────┘
                               │
         ┌─────────┬───────────┼───────────┬─────────┐
         │         │           │           │         │
    ┌────▼───┐ ┌───▼────┐ ┌────▼───┐ ┌─────▼────┐ ┌──▼─────┐
    │ Claude │ │ OpenAI │ │ Ollama │ │  Google  │ │ Azure  │
    │ (API)  │ │ (API)  │ │(local) │ │ (Vertex) │ │ OpenAI │
    └────────┘ └────────┘ └────────┘ └──────────┘ └────────┘
```

## Configuration

`~/.config/maxwell-daemon/maxwell-daemon.yaml`:

```yaml
version: "1"

backends:
  claude:
    type: claude
    model: claude-sonnet-4-6
    api_key: ${ANTHROPIC_API_KEY}

  local:
    type: ollama
    model: llama3.1
    base_url: http://localhost:11434

  gpt:
    type: openai
    model: gpt-4o-mini
    api_key: ${OPENAI_API_KEY}

agent:
  default_backend: claude
  max_turns: 200

repos:
  - name: my-project
    path: ~/work/my-project
    backend: claude          # override per-repo
    slots: 2

  - name: scratch
    path: ~/work/scratch
    backend: local           # route cheap work to Ollama

budget:
  monthly_limit_usd: 200
  alert_thresholds: [0.75, 0.9, 1.0]

api:
  enabled: true
  host: 127.0.0.1
  port: 8080
  auth_token: ${MAXWELL_API_TOKEN}
```

## CLI

```bash
maxwell-daemon init            # create starter config
maxwell-daemon status          # show configured backends and repos
maxwell-daemon backends        # list registered adapters
maxwell-daemon health          # probe every backend
maxwell-daemon ask "hello"     # one-shot prompt (useful for smoke tests)
maxwell-daemon-runner          # run the daemon (systemd entrypoint)
```

## Roadmap

See the [full issue roadmap](https://github.com/D-sorganization/Maxwell-Daemon/issues). At a glance:

| Phase  | Focus                           | Status |
|--------|---------------------------------|--------|
| 1      | Foundation & architecture       | 🟢 In progress |
| 2      | Multi-backend LLM support       | 🟡 Claude/OpenAI/Ollama done |
| 3      | VS Code-like GUI                | ⚪ Planned |
| 4      | Remote access & fleet mgmt      | 🟡 REST API scaffolded |
| 5      | Ansible + Terraform deployment  | ⚪ Planned |
| 6      | VS Code extension, desktop app  | ⚪ Planned |
| 7      | Observability & cost analytics  | 🟡 Ledger done, Prometheus next |
| 8      | Encryption, RBAC, audit logging | ⚪ Planned |
| 9      | Docs site, community            | ⚪ Planned |
| 10     | 1.0.0 release                   | ⚪ Target Q3 2026 |

## Contributing

Maxwell-Daemon is MIT-licensed and contributor-friendly. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT © D-sorganization
