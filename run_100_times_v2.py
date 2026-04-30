#!/usr/bin/env python3
"""Run TestSubmitThreadsafe 100 times sequentially and report results."""

import json
import subprocess
import sys
import time
from pathlib import Path

TEST_PATH = "tests/unit/test_daemon.py::TestSubmitThreadsafe"
RUNS = 100
PROGRESS_FILE = Path("run_100_progress.json")
RESULT_FILE = Path("run_100_results.json")


def load_progress() -> dict:
    """Load existing progress if available."""
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "current_run": 0,
        "total_runs": RUNS,
        "passed": 0,
        "failed": 0,
        "status": "RUNNING",
        "run_time": 0,
    }


def save_progress(progress: dict) -> None:
    """Save progress to file."""
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f)


def main() -> int:
    """Run tests sequentially."""
    progress = load_progress()
    start_run = progress["current_run"]
    passed = progress["passed"]
    failed = progress["failed"]
    failures = []
    start = time.monotonic()

    if start_run >= RUNS:
        print("Already completed.")
        return 0

    for i in range(start_run + 1, RUNS + 1):
        run_start = time.monotonic()
        result = subprocess.run(
            [sys.executable, "-m", "pytest", TEST_PATH, "--no-cov", "-q"],
            capture_output=True,
            text=True,
        )
        run_elapsed = time.monotonic() - run_start

        if result.returncode == 0:
            passed += 1
            status = "PASS"
        else:
            failed += 1
            status = "FAIL"
            failures.append(
                {
                    "run": i,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                    "elapsed": run_elapsed,
                }
            )

        # Write progress after each run
        progress = {
            "current_run": i,
            "total_runs": RUNS,
            "passed": passed,
            "failed": failed,
            "status": status,
            "run_time": run_elapsed,
        }
        save_progress(progress)

        print(
            f"Run {i:3d}/{RUNS}: {status} ({run_elapsed:.2f}s) | Total: {passed} passed, {failed} failed"
        )

        if i % 10 == 0:
            print(f"--- Progress: {i}/{RUNS} ---")

    total_elapsed = time.monotonic() - start

    summary = {
        "runs": RUNS,
        "passed": passed,
        "failed": failed,
        "total_time": total_elapsed,
        "avg_time": total_elapsed / RUNS,
        "failures": failures[:5],  # Keep first 5 failures
    }

    with open(RESULT_FILE, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 60)
    print(f"SUMMARY: {passed} passed, {failed} failed out of {RUNS} runs")
    print(f"Total time: {total_elapsed:.1f}s  Avg per run: {total_elapsed / RUNS:.2f}s")
    print("=" * 60)

    if failures:
        print("\n--- First failure details ---\n")
        f = failures[0]
        print(f"Run #{f['run']} ({f['elapsed']:.2f}s):")
        print("STDOUT:", f["stdout"])
        print("STDERR:", f["stderr"])

        if len(failures) > 1:
            print(f"\n... and {len(failures) - 1} more failures")

    return 0


if __name__ == "__main__":
    sys.exit(main())
