# System Specification & Architecture

## 1. Philosophy
Maxwell-Daemon acts as the "Control Tower" for AI software development. It adheres to strict principles:
- **TDD (Test-Driven Development)**: Code must be tested through the validation sandbox before moving to validation gates.
- **DbC (Design by Contract)**: Role Players fail fast if the assigned backend cannot fulfill the capability requirements.
- **Keep Knowledge in Plain Text**: Memory is stored as dense Markdown, not black-box vector databases.

## 2. Core Subsystems

### 2.1 The Cognitive Pipeline
The pipeline is a state-machine orchestrating the workflow:
- **Strategist (Architect)**: Reads the `RepoSchematic` and Formulates a plan.
- **Implementer (Coder)**: Writes code and tests within the `ExecutionSandbox`.
- **Maxwell Crucible (Validator)**: Cross-audits the code against the Strategist's plan.

### 2.2 Role Orchestration
- **Role**: A dataclass defining the job (e.g., "Requires Tool Use").
- **Job**: The specific instructions and context for a task.
- **RolePlayer**: The runtime wrapper that binds a `Role` to a `BackendRouter` decision.

### 2.3 Environmental Safety
- **Execution Sandbox**: Validates argv-list commands against policy, constrains the working directory to a configured workspace root, filters environment variables, records execution evidence, and enforces timeouts. The current runner uses host subprocess execution; it does not enforce Docker, filesystem, network, process, cgroup, or seccomp isolation.
- **Memory Annealer**: A background cycle that compresses gigabytes of raw logs into kilobytes of architectural markdown, purging the raw files to conserve disk space.

## 3. Deployment Constraints
- Requires Python 3.10+
- Source checkout environments can bootstrap the daemon with `Launch-Maxwell.bat`
  on Windows, `Launch-Maxwell.command` on macOS, or `Launch-Maxwell.sh` on Linux.
- Background tasks (like the Memory Annealer) must yield via `asyncio` to prevent blocking the GUI thread.
