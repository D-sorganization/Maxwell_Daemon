#!/usr/bin/env python3
"""Run TestSubmitThreadsafe 100 times sequentially and report results."""

import subprocess
import sys
import time

TEST_PATH = "tests/unit/test_daemon.py::TestSubmitThreadsafe"
RUNS = 100

passed = 0
failed = 0
failures = []
start = time.monotonic()

for i in range(1, RUNS + 1):
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
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed": run_elapsed,
            }
        )

    print(f"Run {i:3d}/{RUNS}: {status} ({run_elapsed:.2f}s) | Total: {passed} passed, {failed} failed")

    if i % 10 == 0:
        print(f"--- Progress: {i}/{RUNS} ---")

total_elapsed = time.monotonic() - start

print("\n" + "=" * 60)
print(f"SUMMARY: {passed} passed, {failed} failed out of {RUNS} runs")
print(f"Total time: {total_elapsed:.1f}s  Avg per run: {total_elapsed/RUNS:.2f}s")
print("=" * 60)

if failures:
    print(f"\n--- First failure details ---\n")
    f = failures[0]
    print(f"Run #{f['run']} ({f['elapsed']:.2f}s):")
    print("STDOUT:", f["stdout"][:2000])
    print("STDERR:", f["stderr"][:2000])

    if len(failures) > 1:
        print(f"\n... and {len(failures) - 1} more failures")