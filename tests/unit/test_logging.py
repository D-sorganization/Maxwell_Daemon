"""Structured logging — configuration, context binding, and JSON output."""

from __future__ import annotations

import json

import pytest

from maxwell_daemon.logging import bind_context, configure_logging, get_logger


class TestConfigureLogging:
    def test_returns_logger_with_given_level(self) -> None:
        configure_logging(level="DEBUG", json_format=False)
        logger = get_logger("test")
        assert logger is not None

    def test_json_format_emits_parseable_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_format=True)
        get_logger("test").info("hello", extra_field=42)
        captured = capsys.readouterr()
        # At least one line should be valid JSON with our fields.
        lines = [line for line in captured.err.splitlines() + captured.out.splitlines() if line]
        parsed = [json.loads(line) for line in lines if line.startswith("{")]
        assert any(p.get("event") == "hello" and p.get("extra_field") == 42 for p in parsed)

    def test_plain_format_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_format=False)
        get_logger("test").info("hello there")
        out = capsys.readouterr()
        text = out.err + out.out
        assert "hello there" in text


class TestBindContext:
    def test_binds_persistent_key(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_format=True)
        with bind_context(request_id="req-123"):
            get_logger("test").info("inside")
        out = capsys.readouterr()
        lines = [line for line in (out.err + out.out).splitlines() if line.startswith("{")]
        parsed = [json.loads(line) for line in lines]
        assert any(p.get("request_id") == "req-123" for p in parsed)

    def test_context_is_scoped_to_with_block(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_format=True)
        with bind_context(scoped="yes"):
            pass
        get_logger("test").info("outside")
        out = capsys.readouterr()
        lines = [line for line in (out.err + out.out).splitlines() if line.startswith("{")]
        parsed = [json.loads(line) for line in lines]
        # None of the outside events should carry scoped="yes"
        outside = [p for p in parsed if p.get("event") == "outside"]
        assert outside
        assert all("scoped" not in p for p in outside)

    def test_nested_contexts_merge(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", json_format=True)
        with bind_context(outer="a"), bind_context(inner="b"):
            get_logger("test").info("both")
        out = capsys.readouterr()
        lines = [line for line in (out.err + out.out).splitlines() if line.startswith("{")]
        parsed = [json.loads(line) for line in lines]
        matched = [p for p in parsed if p.get("event") == "both"]
        assert matched and matched[0].get("outer") == "a" and matched[0].get("inner") == "b"
