# Agentic Orchestrators: Market Assessment and Home User Strategy

This planning note updates the initial market memo with the supplied research
report and an additional Maxwell-Daemon product analysis. It is intentionally
biased toward the target user: a home user, hobbyist, or solo developer who has
limited cash, a few AI subscriptions, one or more personal machines, and a
strong desire to self-host, test safely, and improve code quality without
running an enterprise platform.

## Decision

Maxwell-Daemon should not copy the enterprise "agent control tower" category.
The stronger product is a **personal autonomous engineering hub**:

- Local-first by default, with SQLite, HTTP, and Tailscale-friendly networking.
- Subscription-aware, so users can route work across Claude, Codex/Copilot,
  API-backed models, and local Ollama without wasting scarce premium quota.
- Safety-first, so every side effect is bounded by approval policy, audit
  events, work-item scope, source-controlled checks, and sandboxed execution.
- Memory-aware, so the codebase carries durable project knowledge across
  sessions and across the user's own fleet.

Enterprise primitives such as Redis Streams, Ray, Kubernetes, and policy engines
are useful references, but they should remain optional scale-out backends until
the home-user flow is excellent.

## Evidence Basis

The supplied report argues that autonomous software engineering is moving from
single coding assistants toward coordinated fleets with shared memory,
spec-driven workflows, tool protocols, validation loops, and runtime governance.
Treat niche framework adoption numbers in that report as leads rather than
proof. Product strategy should rely on primary references where possible.

| Market signal | Evidence | Maxwell implication |
| --- | --- | --- |
| Tool interoperability is consolidating around MCP. | The [Model Context Protocol](https://modelcontextprotocol.io/docs/getting-started/intro) describes an open standard for connecting AI apps to data sources, tools, and workflows. | Maxwell needs real MCP client and server support. The current internal tool registry is useful, but not sufficient protocol adoption. |
| Home users already have capable subscription-based coding agents. | [Claude Code](https://code.claude.com/docs/en/overview) supports terminal, IDE, desktop, web, MCP, memory, hooks, scheduled tasks, and multi-agent workflows. [GitHub's Codex integration](https://docs.github.com/en/copilot/concepts/agents/openai-codex) can be powered by Copilot plans. | Maxwell should orchestrate these tools and account for quota, rate limits, and local context rather than pretending to replace every vendor surface. |
| Local model execution is a baseline requirement. | [Ollama's API](https://docs.ollama.com/api/introduction) runs locally at `http://localhost:11434/api` and has official Python and JavaScript libraries. | Maxwell should make Ollama the default low-cost workhorse for routine refactors, summarization, memory consolidation, and test-fix loops. |
| Repository maps reduce token waste. | [Aider's repository map](https://aider.chat/docs/repomap.html) sends concise class/function/signature context and lets the model request specific files when needed. | Maxwell should prioritize repo maps before heavyweight RAG. Tree-sitter is valuable, but the product goal is "enough structure, low tokens, fast setup." |
| AI checks are becoming source-controlled project policy. | [Continue](https://docs.continue.dev/) runs markdown-defined AI checks on PRs and reports GitHub status checks. | Maxwell's `.maxwell/checks/*.md` direction is strategically correct and should become a core home-user quality feature. |
| Distributed runtimes are powerful but heavy. | [Ray Core](https://docs.ray.io/en/latest/ray-core/walkthrough.html) provides tasks, actors, objects, scheduling, fault tolerance, and accelerator support. | Ray/Kubernetes are appropriate optional backends for advanced users, not the default installation path. |

## Corrections to the Initial Memo

The initial PR memo was directionally useful but overstated a few capabilities.

| Prior statement | Corrected interpretation |
| --- | --- |
| "Full adoption of MCP" | Maxwell currently has `maxwell_daemon/tools/mcp.py`, an internal tool schema registry that emits provider-specific tool definitions. Real MCP adoption means implementing MCP client/server transports and permission mapping. |
| "`dual_config` backend system already supports this" | Maxwell has backend config, `fallback_backend`, budget config, request cost tracking, and `/api/v1/cost`. The roadmap still needs explicit repo budgets, subscription quota tracking, fallback policy, and dashboard UX. |
| "Same orchestration capabilities as enterprise fleets" | Maxwell has the right local primitives, but should not claim parity with Ray/Kubernetes/Redis systems. The advantage is home-user simplicity, not enterprise scale. |
| "Hermetic validation loops" | The loop is a high-value goal. It needs a safety design: isolated worktrees or containers, resource caps, command allowlists, secrets isolation, network policy, timeouts, and a kill switch. |

## Product Positioning

Maxwell-Daemon should be the layer that turns scattered personal AI resources
into a governed development system.

The home user problem is not "how do I run 100 enterprise agents?" It is:

- I pay for multiple AI products and want to use the right one at the right time.
- I have a local GPU or spare machine and want it used for cheap work.
- I want agents to run tests and improve code quality without damaging my repo.
- I want project memory to live with the codebase, not inside one vendor chat.
- I want self-hosting and Tailscale fleet use without Kubernetes operations.
- I want advanced features, but I need the default path to be understandable.

That makes Maxwell's core promise:

> A local-first daemon that budgets, routes, remembers, tests, and audits
> autonomous code work across the user's own machines and subscriptions.

## Market Map for Maxwell

| Category | Representative tools from the report and market | What Maxwell should learn | What Maxwell should avoid |
| --- | --- | --- | --- |
| Personal coding agents | Claude Code, Codex/Copilot, Aider, Cline/Roo, Continue | Great local workflows, repo awareness, PR checks, subscription-backed access | Lock-in to one model or one IDE |
| Local/self-hosted substrate | Ollama, OpenHands-style sandboxes, local CLIs | Cheap repeatable execution, privacy, offline-capable loops | Treating local models as if they can handle every architecture task |
| Enterprise orchestration | Ray, Kubernetes/KubeRay, Redis Streams, LangGraph, AutoGen, CrewAI, Dify, n8n | Actors, queues, observability, fault tolerance, role separation | Making a home user operate a cluster before they get value |
| Memory systems | Zep, Letta/MemGPT, Mem0, repo-local markdown memory | Episodic, semantic, and procedural memory scopes; shared state for fleets | Opaque memory that users cannot inspect, diff, or fix |
| Governance | Agent OS-style policy, work items, audit logs, action approval, capability gates | Policy before side effects, immutable audit, trust boundaries | Enterprise compliance complexity in the default flow |
| Vision and GUI automation | screenshot-to-code, UI-TARS, Agent-S, Playwright/browser-use patterns | Useful for frontend QA, screenshot comparison, and browser tasks | Desktop-control autonomy before file/code safety is mature |

## Home-User Product Opportunities

### 1. Subscription-Aware Routing

Build routing around the resources the user actually has:

- API backends with measurable USD cost.
- Flat-rate subscriptions with request/session quotas.
- Local models with no per-token fee but limited latency, quality, RAM, and VRAM.
- Cloud agent sessions such as Codex/Copilot or Claude Code where billing is not
  the same as raw API tokens.
- Terms-safe integrations only: use official APIs, CLIs, SDKs, or documented
  agent surfaces. If a provider does not expose exact cost or quota telemetry,
  record estimates and uncertainty rather than inventing precision.

Acceptance criteria:

- Each backend advertises capability tags: `planning`, `coding`, `review`,
  `vision`, `long_context`, `cheap`, `local`, `offline`, `subscription`.
- The cost UI shows USD, estimated subscription usage, rate-limit cooldowns, and
  local resource use separately.
- Users can set policies such as "use local for tests and summaries", "spend
  frontier models only on architecture and final review", and "hard stop after
  this repo spends $X this month."

### 2. Real MCP Client and Server Support

Maxwell should be both:

- An MCP client that consumes local/community MCP servers.
- An MCP server that exposes Maxwell tasks, work items, checks, memories,
  artifacts, cost state, and fleet status to compatible AI clients.

This creates a strong home-user loop: Claude Code, Codex/Copilot, or another
client can ask Maxwell for repo context, budget state, or task history while
Maxwell can call the user's local tools through standard MCP.

Acceptance criteria:

- Do not market `maxwell_daemon/tools/mcp.py` as complete MCP.
- Add transport-level MCP support with permission profiles.
- Map every tool to an action policy tier and audit event.
- Provide a starter catalog of safe local MCP servers: filesystem read-only,
  GitHub read-only, SQLite read-only, and browser test runner.

### 3. Repo-Carried Memory and Shared Fleet Experience

Memory should belong to the codebase first. The default should be a
human-readable `.maxwell/memory/` folder with:

- `decisions.md` for architectural decisions.
- `patterns.md` for project conventions.
- `commands.md` for build, test, and debug commands.
- `failures.md` for known failure modes and fixes.
- `fleet.md` for machine capabilities and routing lessons.

For fleets, the coordinator can publish memory deltas to workers over the
existing HTTP/Tailscale transport. The worker should never invent a separate
truth unless it syncs back as a proposed memory update.

Acceptance criteria:

- Memory updates are proposed actions, not silent writes.
- Memories have source links to tasks, PRs, tests, or user decisions.
- Workers receive read-only memory snapshots by default.
- Conflicting memories require human review or coordinator reconciliation.

See also [phase-gate workflows and memory](../../feature_evaluation/05_phase_gate_workflows_and_memory.md).

### 4. Safe Local Validation Loops

The strongest home-user value is an agent that proves code works before asking
for review. The safe version requires more than "run pytest":

- Run in an isolated worktree or container.
- Default-deny destructive commands.
- Bound CPU, RAM, disk, time, network, and process count.
- Strip secrets unless explicitly approved.
- Store stdout, stderr, diffs, screenshots, and artifacts.
- Retry only within a configured budget.

Acceptance criteria:

- Work items define acceptance criteria and required checks.
- `.maxwell/checks/*.md` adds project-specific AI review gates.
- The action ledger records every command, file mutation, and approval.
- The UI shows "what changed", "what ran", "what failed", and "what was fixed."

See also [Docker sandboxing and auto-fix loops](../../feature_evaluation/02_docker_sandboxing_and_auto_fix.md), [source-controlled checks](../../architecture/checks.md), and [governed work items](../../architecture/work-items.md).

### 5. Tailscale-Native Personal Fleet

The fleet should feel like "use my machines" rather than "operate a cluster."
Tailscale is the right default network assumption, but Maxwell should reduce
the setup burden.

Product improvements:

- Fleet setup wizard that validates MagicDNS, auth token/JWT, and ACL reachability.
- Worker capability registry: OS, CPU, RAM, GPU, installed tools, local models,
  repo checkout availability, and max parallel tasks.
- Coordinator scheduling policy that routes cheap/test-heavy work to local
  machines and premium/review-heavy work to the best model.
- Offline queueing and stale-task recovery when a laptop sleeps.
- Shared memory snapshots and artifact fetches across the tailnet.

Acceptance criteria:

- `maxwell-daemon fleet doctor` reports actionable setup failures.
- Workers can be read-only, test-only, or full-execution nodes.
- Fleet dispatch never exposes task or memory APIs to the public internet.

See also [Tailscale fleet deployment](../../operations/tailscale.md).

### 6. Home-Grade Governance

Do not import enterprise trust frameworks wholesale. Translate the useful parts
into understandable local controls:

- Action ledger: every side effect has a proposed/running/applied/failed state.
- Permission modes: suggest, auto-edit, full-auto with explicit scope limits.
- Capability profiles: read-only, edit-only, command-limited, PR-capable.
- Kill switch: stop current task, revoke worker assignment, and freeze side effects.
- Audit export: a single file or API response the user can inspect after a run.

This is where Maxwell can beat single-agent tools for trust. The product should
make the autonomous system legible.

### 7. Vision, Browser, and GUI Automation After Core Safety

The report's multimodal section is relevant, but it should be sequenced after
safe code mutation and validation. The first practical home-user slice is not
full desktop control. It is:

- Playwright browser testing.
- Screenshot capture and visual diff artifacts.
- UI failure summarization.
- Optional screenshot-to-code scaffolding for small prototypes.

Full OS GUI automation should remain experimental until command/file safety,
approval policy, and artifact review are mature.

## Prioritized Roadmap

| Priority | Work | Why it matters for home users |
| --- | --- | --- |
| P0 | Keep planning docs navigable and source-backed. | Users and contributors need a stable strategy, not chat-only market notes. |
| P1 | Subscription-aware routing and budget dashboard. | Directly saves money and makes paid plans more useful. |
| P1 | Real MCP client/server implementation. | Lets Maxwell plug into the broader tool ecosystem and expose its own memory/tasks. |
| P1 | Repo-carried memory with fleet sync. | Gives the codebase a durable "web of experience" across machines. |
| P1 | Safe validation loop with action ledger, checks, and work items. | Converts code generation into verified code improvement. |
| P2 | Tailscale fleet doctor and capability-aware scheduling. | Lets enthusiasts use spare machines without Kubernetes. |
| P2 | Repo-map context provider. | Reduces token waste and improves code quality across large projects. |
| P3 | Optional Ray/Redis scale-out backend. | Useful for advanced users after the local-first path is solid. |
| P3 | Browser/vision QA and screenshot artifacts. | Valuable for UI work, but not before core safety. |

## Recommended Issues

Create or update issues around these implementation slices:

1. `MCP`: implement protocol-level client/server support and permission mapping.
2. `Routing`: add terms-safe subscription-aware backend metadata and policy-driven fallback.
3. `Memory`: add repo-local memory files, proposed memory updates, and fleet sync.
4. `Validation`: implement sandboxed check execution with resource limits.
5. `Fleet`: add `fleet doctor`, worker capability registry, and Tailscale checks.
6. `UI`: add cost/resource dashboard, action approval queue, and validation artifacts.
7. `Context`: add repo-map provider with a simple parser first, Tree-sitter where available.

These are the product gaps most likely to improve Maxwell for the target home
user while preserving the long-term ability to grow into larger fleets.
