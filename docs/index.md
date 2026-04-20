# Maxwell-Daemon

**Multi-backend autonomous code agent orchestrator** — use any LLM, from any provider, across any fleet of machines.

Built for developers who want to use everything they've already paid for — Claude subscriptions, OpenAI keys, a local Ollama box, a GPU in the closet — and route each task to the backend that makes sense for it.

## Why

Existing autonomous-agent tools lock you to one vendor and one machine. Maxwell-Daemon solves both:

- **Resource-agnostic** — Claude, GPT-4o, Gemini, Llama via Ollama, any OpenAI-compatible endpoint. One interface, pluggable adapters.
- **Cost-aware by default** — every request is metered, every repo can have a budget, every backend knows its price.
- **Fleet-native** — one laptop, five boxes in your basement, or a cloud cluster. Same config, same CLI, same dashboard.
- **Professional-grade** — TDD, DbC, strict typing, fleet-standard CI (ruff + mypy --strict + coverage ratchet + file-size budget + CodeQL).

## Quick links

- [Quick start](getting-started/quickstart.md) — install, init, ask
- [Configuration](getting-started/configuration.md) — `maxwell-daemon.yaml` reference
- [Architecture overview](architecture/overview.md) — how the pieces fit together
- [Backend interface](architecture/backends.md) — add a new LLM in under an hour
- [Design by Contract](architecture/contracts.md) — how Maxwell-Daemon stays correct at the boundaries
