# Feature Evaluation: AST Repository Mapping

## Overview
AST (Abstract Syntax Tree) Repository Mapping (inspired by Aider) uses `tree-sitter` to parse the entire codebase into a compressed map of classes, methods, and signatures. Instead of dumping full file contents into the LLM's context window, the agent gets a structural skeleton of the repository.

## Interface with Existing Approach
Maxwell-Daemon currently relies on backend routing and a task queue. It likely passes user prompts and selected files naively. AST Mapping would be injected into the `PLANNING` phase as the default context provider, dramatically reducing the token payload sent to any backend (Claude, OpenAI, or Ollama).

## Pros, Cons, and Costs
*   **Pros:** Massive reduction in token consumption; prevents hallucinated function calls because the LLM sees the exact signatures; gives global context without blowing up the context window.
*   **Cons:** Requires maintaining `tree-sitter` binaries/bindings for multiple languages.
*   **Costs:** Negligible compute cost to generate the map locally. High savings on API costs (fewer input tokens).

## Impact on Target User (The Hobbyist)
The hobbyist coder often works across multiple disjointed files and projects. AST Mapping categorizes and synthesizes this code automatically. The user doesn't have to choose which files to include in the prompt—the daemon chooses the relevant structural context, acting as the team's "Librarian."

## Engineering Principles Enforced
*   **DRY (Don't Repeat Yourself):** The agent sees exactly where logic is defined globally, preventing it from writing duplicate helper functions.
*   **LOD (Law of Demeter):** By seeing the explicit signatures and class boundaries, the agent is naturally guided to use explicit interfaces rather than deep-chaining into unknown objects.
*   **Pragmatic Programmer:** "Keep Knowledge in Plain Text" and "Don't Gather More Than You Need." The AST map provides exactly the metadata required without the noise of implementation details.
