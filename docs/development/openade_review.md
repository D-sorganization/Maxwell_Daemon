# OpenADE Repository Review & Integration Analysis

This document reviews the [bearlyai/openade](https://github.com/bearlyai/openade) repository in terms of its architecture, development practices, and features, and analyzes how its components and design patterns can be effectively used to improve **Maxwell Daemon** and our development workflows.

---

## 1. What OpenADE Represents

**OpenADE (Agentic Development Environment)** by Bearly AI is an open-source, terminal-centric development environment designed around the core philosophy of **Plan → Revise → Execute**. It is built with clean architecture in **TypeScript**, using a monorepo workspace structured as follows:
- `projects/harness`: A unified TypeScript library (`@openade/harness`) that wraps local coding CLIs (specifically **Claude Code** and **OpenAI Codex**) as child processes and pipes their stdout/stderr streams into structured JSONL events.
- `projects/web`: A React + Vite web dashboard displaying files, diffs, shell execution progress, and chat interfaces.
- `projects/electron`: An Electron-based desktop app hosting the React web view and providing local API endpoints.
- `projects/shared`: Common components and utility scripts, such as the Tailscale-based companion pairing logic.

### Core Design Philosophy
1. **The "Anti-Feature":** OpenADE intentionally **removes direct editing capability** for developers in the UI. Instead, developers guide the agent by commenting on code lines, diffs, or agent messages, letting the agent make the edits.
2. **Explicit Planning over Autocomplete:** It prevents agents from blindly editing code. Instead, it forces them to generate an explicit plan, allows developers to comment on and refine the plan, and executes the tasks linearly once approved.
3. **Local & Private:** Everything runs locally on the user's host machine. Storing state locally, using local CLI configurations, and relying on in-process/local MCP servers ensures code remains private.

---

## 2. Key Architectural Features

### A. `@openade/harness` (CLI Wrapper Engine)
Instead of invoking raw LLM API endpoints directly (which charges double API tax and bypasses local CLI tooling/credentials), OpenADE drives the **Claude Code** and **Codex** CLIs as subprocesses:
* **Interactive JSONL Streaming:** Spawns `claude` with `--output-format stream-json --verbose` and parses the stdout stream line-by-line into a unified typescript `HarnessEvent` type.
* **Temp File Prompt Sandboxing:** Passes large prompts/system instructions via `--system-prompt-file` and `--append-system-prompt-file` instead of using command-line arguments to prevent `E2BIG` (argument list too long) shell limits.
* **Permissions Bridging:** In read-only mode, it runs Claude Code with `--permission-mode dontAsk` (which auto-denies interactive prompts) and supplies a strict whitelist of safe read-only tools and Bash command patterns (e.g. `git diff`, `ls`, `grep`) via `--allowedTools` and `--disallowed-tools`.
* **Session Management:** Re-uses or forks existing CLI sessions directly from the local SQLite/JSONL cache of Claude Code and Codex (e.g., reading from `~/.claude/projects/` and checking active process PIDs in `~/.claude/sessions/`).

### B. HyperPlan (Multi-Agent Planning)
HyperPlan implements a Directed Acyclic Graph (DAG) for collaborative agent planning. It defines the following primitive step types:
* `plan`: Runs an agent to generate a plan for a given task description.
* `review`: Runs an agent to critique another plan step's output (takes 1 input).
* `reconcile`: Takes multiple plans and critiques, merging them into a single coherent plan (takes $\ge 1$ inputs).
* `revise`: Instructs the planning agent to revise their plan based on feedback from a review step (resumes the planner's session).

Using topological sorting and grouping by depth, OpenADE runs independent planning steps in parallel (even using different LLMs/providers) before reconciling them into a unified output.

### C. MCP Integration & Dynamic Client Tools
OpenADE provides out-of-the-box support for Model Context Protocol (MCP) servers (e.g., Notion, Linear, GitHub). More importantly, it supports **in-process Client Tools** by:
1. Dynamically starting a local MCP HTTP server inside the Node.js runner process.
2. Generating a temporary `mcp-config.json` containing the HTTP address and a short-lived authorization token.
3. Spawning the coding CLI with `--mcp-config` and `--strict-mcp-config` to force the CLI to connect to the in-process server.

---

## 3. Applicability to Maxwell Daemon

Maxwell Daemon is an autonomous backend control plane written in Python (FastAPI, SQLite, standard library subprocesses). We can directly import, adapt, or copy several OpenADE design patterns to enhance our daemon:

### 1. Upgrade Claude Code CLI Backend to True Streaming
Currently, our `maxwell_daemon/backends/claude_code.py` CLI wrapper runs `claude` in one-shot mode (`--output-format json`), waiting for the entire command to complete before returning.
> **How to adopt:** Port OpenADE's `ClaudeCodeHarness` stream parser to Python.
> - Run the `claude` process with `--output-format stream-json --verbose --dangerously-skip-permissions`.
> - Read the process `stdout` line-by-line asynchronously, parsing the JSON lines (`system/init`, `assistant` text blocks, `tool_progress`, `user` tool calls, `result`).
> - Yield the partial text blocks and tool use progress directly to Maxwell's event bus and `/api/v1/events` WebSocket stream. This provides instant visual feedback in `/ui/` and `runner-dashboard`.

### 2. Implement Temporary Prompt and Configuration Files
Maxwell Daemon currently constructs command strings containing full prompts. This can hit command length limits (especially on Windows) and expose sensitive prompts in process monitors.
> **How to adopt:** Pass system prompts via temporary files.
> - Write the system prompt and append prompt to temp files in the OS temp directory.
> - Pass `--system-prompt-file` and `--append-system-prompt-file` arguments to the CLI backend.
> - Automatically clean up the files in a `finally:` block after the subprocess exits.

### 3. Expose Python Tools/Skills via In-Process MCP Servers
Maxwell Daemon defines python-based tools and skills (e.g., custom file editing, clinical databases) that are currently only accessible to direct LLM backends (like raw Claude/OpenAI APIs) via custom JSON-schemas.
> **How to adopt:** Start a local MCP server inside Maxwell Daemon.
> - When running a CLI backend (like `claude-code-cli`), launch a lightweight MCP HTTP/stdio server thread exposing Maxwell's local Python tools.
> - Create a temporary `mcp-config.json` file pointing to this server and pass it as `--mcp-config` to the CLI backend.
> - This gives the local CLI access to all of Maxwell's advanced science databases, file editing algorithms, and environment checks without duplicating tool logic in JavaScript/TypeScript.

### 4. HyperPlan Integration in Maxwell's Strategist
Maxwell Daemon's current cognitive pipeline (Strategist → Implementer → Crucible) is a linear flow. The Strategist plans, the Implementer codes, and the Crucible reviews.
> **How to adopt:** Implement a DAG-based planning strategist in Python.
> - Port the HyperPlan strategies (Ensemble, Peer Review, Cross-Review) to Maxwell's Strategist.
> - Run parallel planners (e.g. one using Gemini for high-level outline, one using Claude for detailed structure) and a critic model to reconcile them before handing the finalized contract to the Implementer sandbox.

### 5. Automated Git Snapshots & Worktrees for Sandbox Safety
Maxwell's `ExecutionSandbox` runs commands directly on the host workspace. Although it has directory filters and timeout guards, it lacks rollback capabilities.
> **How to adopt:** Implement Git Snapshots and Worktrees.
> - Before starting the Implementer execution phase, take an automated git snapshot (e.g., using `git stash` or committing to a temp branch).
> - If validation checks in the TDD Gate or Crucible fail, or if a timeout occurs, trigger an automated rollback (`git reset --hard` or `git stash pop`).
> - Alternatively, execute the task inside a temporary `git worktree` so that the main branch remains clean and untouched by failed agent runs.

---

## 4. Workflow and Developer Productivity Gains

Beyond codebase enhancements, OpenADE suggests several developer workflow improvements:
* **The Planning Habit:** Integrating a mandatory "Plan Review" stage into Maxwell's queue system (e.g., sending a WebSocket notification containing the generated plan, waiting for developer approval via `/api/tasks/{id}/approve` before execution).
* **Companion Monitoring:** Since Maxwell is a headless daemon, having a paired lightweight console (like OpenADE's LAN pairing) would allow developers to run long-running refactoring tasks from a desktop, go grab coffee, and monitor logs or approve/deny steps from their phone.
* **Cost Accounting alignment:** By tracking cost data from CLI result events, Maxwell's `CostLedger` can accurately account for CLI usage instead of relying on token counts with missing CLI-specific rate schemas.

## 5. Conclusion & Recommendation

OpenADE represents a major step forward in **local, stream-oriented, planning-first agent orchestration**. Rather than competing with it, Maxwell Daemon should leverage it:
1. **Short term:** Refactor our CLI backends (`claude_code.py` and `codex_cli.py`) to support JSON streaming (`stream-json`) and temp file prompts, leveraging the shell argument whitelist patterns documented in OpenADE.
2. **Medium term:** Build a Python-based dynamic MCP server inside Maxwell to export local tools/skills directly into the CLI subprocesses.
3. **Long term:** Introduce Git snapshots/worktrees into the `ExecutionSandbox` to turn it into a zero-risk testing crucible.
