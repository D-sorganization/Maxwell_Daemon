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
