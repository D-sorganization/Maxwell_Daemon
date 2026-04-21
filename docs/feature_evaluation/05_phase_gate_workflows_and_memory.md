# Feature Evaluation: Phase-Gate Workflows & Local Markdown Memory

## Overview
Inspired by `ijfw`, this feature abandons the "one-shot" autonomous loop in favor of strict phases (Brainstorm -> Plan -> Execute -> Verify). It also implements a persistent `.maxwell/memory/` folder in the repo to store architectural decisions and patterns in plain markdown, updated via background "Dream Cycles".

## Interface with Existing Approach
The task queue must be upgraded to a state machine. The daemon stops at "Gates" (e.g., after Planning) to require user approval. The background daemon handles the Dream Cycle memory consolidation during idle time using cheap local models.

## Pros, Cons, and Costs
*   **Pros:** Prevents massive API token burns on hallucinated paths; ensures the agent adheres to `AGENTS.md`; preserves context across sessions.
*   **Cons:** Requires the user to be a "Human-in-the-Loop" at specific gates, slowing down fully autonomous "fire and forget" workflows.
*   **Costs:** Low API costs due to context compression (markdown memory). Background sweeps are free if run on Ollama.

## Impact on Target User (The Hobbyist)
Fulfills the need to go from vision to product *iteratively*. The user guides the vision at the Phase-Gates, while the daemon handles the execution. The local memory ensures that if the hobbyist puts a project down for a month, the daemon instantly remembers the architecture when they return.

## Engineering Principles Enforced
*   **DRY (Don't Repeat Yourself):** Local memory ensures the agent doesn't have to re-learn or re-derive architectural decisions on every prompt.
*   **Pragmatic Programmer:** "Keep Knowledge in Plain Text." The memory store is human-readable markdown, not an opaque vector database. It travels with the git repo.
*   **Pragmatic Programmer:** "Don't Code Blindfolded." Phase-gates force the agent (and the user) to agree on a concrete plan before a single line of code is written.
