## 2025-04-21 - [Fix Command Injection via Recursive Template Evaluation]
**Vulnerability:** A command injection vulnerability bypassing `shlex.quote` in `maxwell_daemon/hooks.py`. The `_substitute` method replaced dictionary key-values iteratively, so if an attacker's input value was evaluated after an input that referenced its template key, the attacker's payload would expand *outside* of the `shlex.quote` protection boundary.
**Learning:** Sequential `.replace()` template engines introduce a secondary evaluation order risk. A payload may use recursion or sequential interpolation to bypass quoting mechanisms designed to sanitize the first pass.
**Prevention:** Always parse and replace all template fields in a single execution pass (such as via `re.sub`), prohibiting interpolated template outputs from being re-evaluated.
## 2024-05-20 - Prevent environment variable leakage to hook subprocesses
**Vulnerability:** The `_env` function in `maxwell_daemon/hooks.py` was passing the entire `os.environ` to hook subprocesses, exposing host secrets (like `ANTHROPIC_API_KEY`) to LLM-controlled hooks.
**Learning:** Subprocesses should run with a strictly filtered allowlist of environment variables to maintain the LLM execution sandbox.
**Prevention:** Use an allowlist approach (like `_build_run_bash_env`) to only pass safe environment variables (PATH, HOME, etc.) to subprocesses.
