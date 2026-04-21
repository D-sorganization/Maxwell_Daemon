# IJFW Feature Review for Maxwell-Daemon

After reviewing the `ijfw` (It Just F*cking Works) repository, several core features stand out as excellent candidates for integration into **Maxwell-Daemon**. Since Maxwell-Daemon already has a multi-backend architecture (Claude, OpenAI, Ollama), it is uniquely positioned to adopt these concepts natively.

## 1. The Multi-AI "Trident" (Parallel Cross-Audit)
**How ijfw does it:** It sends the same file or pull request to models from different lineages (e.g., OpenAI, Google, and Anthropic) in parallel to review. Disagreements are flagged for user review, while consensus is treated as a green light.
**Maxwell-Daemon Integration:** Since you already route to multiple backends, Maxwell-Daemon could implement a `/cross-audit` endpoint or command. It would dispatch the same review task simultaneously to an expensive model (Claude Opus) and a local model (Ollama Llama 3) or OpenAI. This entirely eliminates single-model blind spots and creates highly professional, bulletproof code reviews.

## 2. Connected Memory & "Dream Cycles"
**How ijfw does it:** It stores agent memory (decisions, patterns, rules) as plain markdown files in `.ijfw/memory/`. Crucially, it has a "Dream Cycle" that periodically sweeps the memory to reconcile contradictions, prune stale entries, and lift project-specific patterns into global memory.
**Maxwell-Daemon Integration:** Implement a local markdown-based memory store for each repository. You can use cheap local models (via Ollama) to run background "Dream Cycle" sweeps to compress and consolidate context, keeping token usage lean for when you invoke expensive frontier models.

## 3. Disciplined Phase-Gate Workflows
**How ijfw does it:** It prevents the AI from rushing into code by enforcing strict phases: Brainstorm -> Plan -> Execute -> Verify -> Ship. It has "Quick" and "Deep" modes. It stops at gates to require user approval (locking the brief) before writing code.
**Maxwell-Daemon Integration:** Enhance the Task Queue to support workflow states. Instead of autonomous loops that can drift, Maxwell-Daemon can pause the task, request human sign-off on the generated plan, and only then proceed to execution. This controls scope creep and saves API costs on hallucinated paths.

## 4. Token Economy via "Skill Hot-Loading"
**How ijfw does it:** It keeps the system prompt tiny (under 60 lines) and only "hot-loads" specific tools and skills (e.g., debug, design, cross-audit) exactly when needed based on the phase.
**Maxwell-Daemon Integration:** Implement dynamic context and tool injection in the Backend Router. Instead of providing the agent with every available function, only load tools relevant to the current task phase, maximizing cache efficiency and reducing per-turn token costs.

## 5. Visual Companion (Live Architecture Diagrams)
**How ijfw does it:** For complex projects, it automatically generates and updates Mermaid diagrams (architecture, data models) in a `.ijfw/visual/` directory at each phase.
**Maxwell-Daemon Integration:** Add a specific hook that runs after major refactors or planning phases to update a `docs/architecture.mermaid` file. This provides instant visual feedback to the user about what the agent *thinks* the architecture looks like.

## 6. Dynamic Custom Agent Teams
**How ijfw does it:** Instead of generic roles, it analyzes a project on the first session and generates a bespoke "team" (e.g., "Software Architect", "Security Lead", "QA") stored as prompts.
**Maxwell-Daemon Integration:** Allow users to request a "Team Assembly" phase where Maxwell-Daemon generates specific persona configurations tailored to the repository, which can then be assigned to different LLM backends (e.g., assign QA to a local model, Architect to Claude).
