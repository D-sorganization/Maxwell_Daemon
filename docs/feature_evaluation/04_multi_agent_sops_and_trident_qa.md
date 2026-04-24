# Feature Evaluation: Multi-Agent SOPs & Adversarial QA (Trident)

## Overview
Drawing from MetaGPT and `ijfw`, this feature simulates a full software company. Work follows a Standard Operating Procedure (SOP): an Architect plans, a Coder executes, and an Adversarial QA agent (the "Trident" cross-audit) tries to break it.

## Interface with Existing Approach
Maxwell-Daemon's core strength is its multi-backend router. This feature exploits that perfectly. The router assigns the "Architect" persona to Claude 3.5, the "Coder" to a local Ollama model, and the "QA" to OpenAI + Gemini.

## Pros, Cons, and Costs
*   **Pros:** Drastically reduces hallucinations; catches edge cases early; creates a highly professional, enterprise-grade output.
*   **Cons:** Can be slower due to multiple sequential and parallel LLM calls.
*   **Costs:** Can increase API costs if not managed, though routing bulk work to local models offsets this.

## Impact on Target User (The Hobbyist)
Provides a literal "development team in a box." The hobbyist doesn't just get a coding assistant; they get a manager, a senior dev, and a QA tester working in synergy. It forces the iterative process—the QA agent rejects bad code before the user even has to review it.

## Engineering Principles Enforced
*   **DbC (Design by Contract):** The SOP inherently enforces contracts. The Architect defines the contract (the plan/interfaces), the Coder fulfills it, and the QA agent aggressively verifies the contract was met.
*   **Pragmatic Programmer:** "Find Bugs Once" and "Coding Ain't Done 'Til All the Tests Run." The Adversarial QA agent embodies these principles by acting as an unyielding gatekeeper for quality.
