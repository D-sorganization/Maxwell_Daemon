# ⚡ Maxwell-Daemon

> **Your Autonomous Software Engineering Team in a Box.**

Maxwell-Daemon is a professionally packaged, autonomous desktop application that orchestrates an entire AI development team to build, test, and ship your software.

Unlike existing tools that are locked to the terminal, Maxwell-Daemon provides a stunning **Native Desktop GUI**, strict **Test-Driven Development (TDD)** enforcement, and **Bring-Your-Own-CLI (BYO-CLI)** flexibility. It ensures you never burn an API token on tasks you don't need to.

## 🚀 Why Maxwell-Daemon?
- **Professional Desktop Application**: Use `Launch-Maxwell.bat`, `Launch-Maxwell.command`, or `Launch-Maxwell.sh` from a source checkout to bootstrap Maxwell-Daemon on Windows, macOS, or Linux.
- **The Cognitive Pipeline**: A state-machine orchestrated team:
  - 🧠 **Strategist**: Formulates architectural plans using the compressed `RepoSchematic`.
  - 💻 **Implementer**: Generates code and runs validation through a policy-gated `ExecutionSandbox`.
  - ⚔️ **Maxwell Crucible**: Adversarial QA role that violently tests the Implementer's code against the Strategist's contract.
- **BYO-CLI**: Don't pay double API taxes. Maxwell-Daemon can hook into your existing local CLI subscriptions (like `jules-cli`, `claude-code`, or `ollama`).

## 📥 Installation & Setup

**Prerequisites:**
- Python 3.10+

**1. Clone the Repository**
```bash
git clone https://github.com/D-sorganization/Maxwell-Daemon.git
cd Maxwell-Daemon
```

**2. Launch the Application**

Use the launcher for your platform:

| Platform | Launcher |
|----------|----------|
| Windows | `Launch-Maxwell.bat` |
| macOS | `Launch-Maxwell.command` |
| Linux | `Launch-Maxwell.sh` |

The launchers create a local `.venv`, install the runtime package without
developer extras, create a starter config if needed, run `maxwell-daemon
doctor`, and then start `maxwell-daemon serve`.

**Developer setup**
Contributors can install the development extras explicitly:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## 🧠 Architectural Highlights
- **RepoSchematic**: Generates highly compressed file-and-symbol trees, saving massive token budgets compared to dumping raw files.
- **Memory Annealer**: Automatically compresses verbose agent logs into dense `architectural_state.md` files, responsibly purging raw logs to save disk space.
- **Execution Sandbox**: Validation commands run through an argv allowlist, workspace-root check, environment filter, timeout, output redaction, and artifact capture. The current executor uses host subprocesses; it does not provide Docker, filesystem, network, process, or resource isolation. See [Security](docs/operations/security.md) before running untrusted generated code.

---
**License**: MIT © D-sorganization
