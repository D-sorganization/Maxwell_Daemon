# Troubleshooting Runbook

This guide contains standard operating procedures for resolving common issues with the Maxwell Daemon in production environments.

## Symptom: Tasks queued but not running

* **Check**: Verify worker count, queue depth, and check recent errors in logs.
* **Commands**:
  ```bash
  curl -s http://127.0.0.1:8000/api/v1/tasks | jq '.[] | select(.status == "QUEUED")'
  curl -s http://127.0.0.1:8000/metrics
  tail -f ~/.local/share/maxwell-daemon/logs/daemon.log | jq .
  ```
* **Common causes**: Budget exhausted, worker crash, gate misconfiguration.
* **Fix**: Restart daemon, check `config.toml` budgets, or adjust gate thresholds.

## Symptom: Cost forecast exceeds budget

* **Check**: Inspect `cost_ledger.db` via scripts.
* **Commands**:
  ```bash
  sqlite3 ~/.local/share/maxwell-daemon/ledger.db "SELECT * FROM ledger ORDER BY timestamp DESC LIMIT 10;"
  # Or use a script to breakdown by backend/model/repo
  ```
* **Common causes**: Inefficient prompts, cache misses, wrong model selected.
* **Fix**: Compress context (`MAXWELL_AGGRESSIVE_COMPRESSION=on`), clear cache, switch to a cheaper model for the task.

## Symptom: High latency on task execution

* **Check**: Gate execution times, critic run times, queue depth.
* **Commands**: Benchmark task by task, inspect critic profiles.
* **Common causes**: Slow critic (e.g. large file to review), queue backlog.
* **Fix**: Increase worker count, optimize gate policy, increase timeout.

## Symptom: Out of memory

* **Check**: Memory annealer status, raw log accumulation.
* **Commands**:
  ```bash
  du -sh ~/.local/share/maxwell-daemon/
  ```
* **Common causes**: Memory annealer disabled, artifact bloat.
* **Fix**: Run memory anneal cycle (`memory_dream_interval_seconds > 0`), prune old artifacts.

## Symptom: Fleet worker goes silent

* **Check**: Worker heartbeat (`last_seen` timestamp in coordinator).
* **Commands**: Check coordinator logs and task reassignment logs.
* **Common causes**: Network partition, OOM on worker, process crash.
* **Fix**: Manual reassignment if needed, restart worker, check resources on worker node.

## Symptom: Service exits with status=226/NAMESPACE

* **Check**: systemd sandboxing paths are correct.
* **Commands**:
  ```bash
  cat /etc/systemd/system/maxwell-daemon.service | grep -E 'WorkingDirectory|ReadWritePaths|ExecStart'
  ls -la <paths from above>
  ```
* **Common causes**: `WorkingDirectory` or `ExecStart` binary path doesn't exist (e.g., repo was moved). `ProtectHome=read-only` is blocking access to a directory not listed in `ReadWritePaths`.
* **Fix**: Verify all paths in the unit file exist. If the repo was migrated to a new location, update the unit file with `sudo sed -i 's|old/path|new/path|g' /etc/systemd/system/maxwell-daemon.service && sudo systemctl daemon-reload`.

## Symptom: Service exits with status=203/EXEC

* **Check**: The `ExecStart` binary exists and has a valid shebang.
* **Commands**:
  ```bash
  head -1 /path/to/.venv/bin/maxwell-daemon
  # If the shebang points to a nonexistent path, the venv is stale
  ```
* **Common causes**: Virtual environment was moved from a different directory. The shebang in `.venv/bin/maxwell-daemon` points to the old path.
* **Fix**: Delete and recreate the venv: `rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e . && deactivate`

## Symptom: Config validation error on startup (status=1/FAILURE)

* **Check**: The journal will show the exact Pydantic validation error.
* **Commands**:
  ```bash
  sudo journalctl -u maxwell-daemon --since "5 min ago" --no-pager | grep -A 5 ValidationError
  ```
* **Common causes**: Missing required fields (`version`, `type`, `model`, `agent.default_backend`), unknown top-level keys (e.g., `logging:`), `host: 0.0.0.0` without `jwt_secret`, backend key name not matching `agent.default_backend`.
* **Fix**: Compare your config against the minimal example in `docs/getting-started/configuration.md`. See `docs/operations/wsl2-node-deployment.md` for a full table of gotchas.

## Symptom: OperationalError — unable to open database file

* **Check**: The data directory exists and the systemd unit allows writes to it.
* **Commands**:
  ```bash
  ls -la ~/.local/share/maxwell-daemon/
  grep ReadWritePaths /etc/systemd/system/maxwell-daemon.service
  ```
* **Common causes**: `~/.local/share/maxwell-daemon/` doesn't exist, or it's not in the unit's `ReadWritePaths` while `ProtectHome=read-only` is set.
* **Fix**: `mkdir -p ~/.local/share/maxwell-daemon` then add the path to `ReadWritePaths` in the unit file and `sudo systemctl daemon-reload && sudo systemctl restart maxwell-daemon`.
