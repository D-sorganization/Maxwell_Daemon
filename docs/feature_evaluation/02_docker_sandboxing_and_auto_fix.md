# Feature Evaluation: Docker Sandboxing & Auto-Fix TDD Loops

## Overview
Inspired by OpenHands and Aider, this feature ensures that all code execution, terminal commands, and test suites are run inside ephemeral, isolated Docker containers. If tests fail, the agent reads the traceback and iteratively fixes the code.

## Interface with Existing Approach
Maxwell-Daemon currently orchestrates agents but lacks a formalized execution sandbox. This feature would introduce a `SandboxExecutor` that routes any agent-requested shell commands into a containerized environment, rather than running them on the host.

## Pros, Cons, and Costs
*   **Pros:** Absolute security (agents cannot delete host files or leak credentials); guarantees a clean, reproducible environment; enables true, safe autonomy.
*   **Cons:** Overhead of managing Docker daemon sockets; slower startup times for environments.
*   **Costs:** Increased local memory and disk usage for Docker images. Zero API cost impact.

## Impact on Target User (The Hobbyist)
Provides the ultimate guardrails. A hobbyist can confidently tell the daemon to "build this feature" without fear of their local machine being compromised by a hallucinated `rm -rf` or infinite loop. It shifts the paradigm from "one-shot code generation" to "iterative, safe experimentation."

## Engineering Principles Enforced
*   **TDD (Test-Driven Development):** This feature is the engine for TDD. The agent writes tests, the sandbox runs them safely, they fail, the agent fixes the code, and repeats until green.
*   **DbC (Design by Contract):** The sandbox validates that the implementation meets the strict contract defined by the tests or compilation checks before the user ever sees the code.
*   **Pragmatic Programmer:** "Test Early. Test Often. Test Automatically." The sandbox automates the verification loop entirely.
