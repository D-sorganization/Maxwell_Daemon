"""Benchmarks for audit log operations."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.audit import AuditLogger


@pytest.fixture
def benchmark_logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "benchmark.jsonl")


def test_log_api_call(benchmark: pytest.BenchmarkFixture, benchmark_logger: AuditLogger) -> None:
    """Benchmark single API call logging."""
    benchmark(benchmark_logger.log_api_call, method="GET", path="/health", status=200)


def test_log_api_call_with_request_id(
    benchmark: pytest.BenchmarkFixture, benchmark_logger: AuditLogger
) -> None:
    """Benchmark API call logging with request_id."""
    benchmark(
        benchmark_logger.log_api_call,
        method="POST",
        path="/api/v1/tasks",
        status=202,
        request_id="req-123",
    )


def test_entries_pagination(benchmark: pytest.BenchmarkFixture, tmp_path: Path) -> None:
    """Benchmark paginated entry retrieval."""
    logger = AuditLogger(tmp_path / "paginate.jsonl")
    for i in range(1000):
        logger.log_api_call(method="GET", path=f"/{i}", status=200)
    benchmark(logger.entries, limit=100, offset=500)


def test_verify_chain(benchmark: pytest.BenchmarkFixture, tmp_path: Path) -> None:
    """Benchmark hash-chain verification."""
    from maxwell_daemon.audit import verify_chain

    logger = AuditLogger(tmp_path / "chain.jsonl")
    for i in range(500):
        logger.log_api_call(method="GET", path=f"/{i}", status=200)
    benchmark(verify_chain, logger._path)
