"""Unit tests for the external agent adapter contract layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.backends.external_adapter import (
    ExternalAgentAdapterBase,
    ExternalAgentAdapterError,
    ExternalAgentAdapterRegistry,
    ExternalAgentCapability,
    ExternalAgentOperation,
    ExternalAgentProbeResult,
    ExternalAgentProbeSpec,
    ExternalAgentRunContext,
    ExternalAgentRunResult,
    ExternalAgentRunStatus,
    UnavailableExternalAgentAdapter,
)


class RecordingAdapter(ExternalAgentAdapterBase):
    def __init__(
        self,
        *,
        adapter_id: str = "recording",
        capabilities: ExternalAgentCapability | None = None,
        probe_summary: str = "adapter ready",
        probe_details: tuple[str, ...] = (),
        probe_metadata: dict[str, object] | None = None,
        run_result: ExternalAgentRunResult | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.capabilities = (
            capabilities
            if capabilities is not None
            else ExternalAgentCapability(
                supported_operations=frozenset(
                    {
                        ExternalAgentOperation.PROBE,
                        ExternalAgentOperation.READ,
                        ExternalAgentOperation.WRITE,
                        ExternalAgentOperation.REVIEW,
                    }
                )
            )
        )
        self._probe_summary = probe_summary
        self._probe_details = probe_details
        self._probe_metadata = {} if probe_metadata is None else probe_metadata
        self._run_result = run_result
        self.seen_contexts: list[ExternalAgentRunContext] = []

    def _probe(self, spec: ExternalAgentProbeSpec) -> ExternalAgentProbeResult:
        _ = spec
        return ExternalAgentProbeResult(
            adapter_id=self.adapter_id,
            summary=self._probe_summary,
            details=self._probe_details,
            metadata=self._probe_metadata,
        )

    def _run(self, context: ExternalAgentRunContext) -> ExternalAgentRunResult:
        self.seen_contexts.append(context)
        if self._run_result is not None:
            return self._run_result
        return ExternalAgentRunResult.completed(
            adapter_id=self.adapter_id,
            operation=context.operation,
            summary=f"handled {context.operation.value}",
            details=("detail-1", "detail-2"),
            changed_files=("src/app.py", "tests/test_app.py"),
            artifacts=("artifacts/report.json",),
            read_only=context.read_only,
            cancellation_requested=context.cancellation_requested,
            cancellation_recorded=False,
        )


class TestRegistry:
    def test_duplicate_adapter_ids_rejected(self) -> None:
        registry = ExternalAgentAdapterRegistry()
        registry.register(RecordingAdapter(adapter_id="alpha"))

        with pytest.raises(ExternalAgentAdapterError, match="already registered"):
            registry.register(RecordingAdapter(adapter_id="alpha"))

    def test_unknown_adapter_returns_unavailable_fallback(self) -> None:
        registry = ExternalAgentAdapterRegistry()

        adapter = registry.resolve("missing")

        assert isinstance(adapter, UnavailableExternalAgentAdapter)
        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="missing",
                operation=ExternalAgentOperation.READ,
                prompt="inspect",
            )
        )
        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.unavailable_reason == "adapter 'missing' is unavailable"


class TestRunValidation:
    def test_unsupported_operation_returns_unavailable_result(self) -> None:
        adapter = RecordingAdapter(
            capabilities=ExternalAgentCapability(
                supported_operations=frozenset({ExternalAgentOperation.READ})
            )
        )

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="recording",
                operation=ExternalAgentOperation.WRITE,
                prompt="update file",
                workspace=Path("workspace"),
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.unavailable_reason == "unsupported operation: write"
        assert result.changed_files == ()
        assert result.artifacts == ()

    def test_write_operations_require_workspace_assignment(self) -> None:
        adapter = RecordingAdapter(
            capabilities=ExternalAgentCapability(
                supported_operations=frozenset({ExternalAgentOperation.WRITE})
            )
        )

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="recording",
                operation=ExternalAgentOperation.WRITE,
                prompt="update file",
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.unavailable_reason == "workspace assignment required for write operations"

    def test_review_operations_are_read_only_by_contract(self, tmp_path: Path) -> None:
        adapter = RecordingAdapter(
            capabilities=ExternalAgentCapability(
                supported_operations=frozenset({ExternalAgentOperation.REVIEW})
            )
        )
        context = ExternalAgentRunContext(
            adapter_id="recording",
            operation=ExternalAgentOperation.REVIEW,
            prompt="review this diff",
            workspace=tmp_path,
        )

        result = adapter.run(context)

        assert context.read_only is True
        assert adapter.seen_contexts[0].read_only is True
        assert result.status is ExternalAgentRunStatus.COMPLETED
        assert result.read_only is True

    def test_result_preserves_changed_files_and_artifacts(self) -> None:
        result = ExternalAgentRunResult.completed(
            adapter_id="recording",
            operation=ExternalAgentOperation.WRITE,
            summary="done",
            changed_files=("src/a.py", "src/b.py"),
            artifacts=("artifact-one.json", "artifact-two.json"),
        )

        assert result.changed_files == ("src/a.py", "src/b.py")
        assert result.artifacts == ("artifact-one.json", "artifact-two.json")

    def test_run_preserves_changed_files_and_artifacts_from_adapter(self) -> None:
        result_template = ExternalAgentRunResult.completed(
            adapter_id="recording",
            operation=ExternalAgentOperation.WRITE,
            summary="done",
            changed_files=("src/a.py", "src/b.py"),
            artifacts=("artifact.json",),
        )
        adapter = RecordingAdapter(run_result=result_template)

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="recording",
                operation=ExternalAgentOperation.WRITE,
                prompt="update file",
                workspace=Path("workspace"),
            )
        )

        assert result.changed_files == ("src/a.py", "src/b.py")
        assert result.artifacts == ("artifact.json",)

    def test_cancellation_is_best_effort_and_recorded(self) -> None:
        adapter = RecordingAdapter(
            capabilities=ExternalAgentCapability(
                supported_operations=frozenset({ExternalAgentOperation.READ})
            )
        )
        context = ExternalAgentRunContext(
            adapter_id="recording",
            operation=ExternalAgentOperation.READ,
            prompt="inspect",
            cancellation_requested=True,
        )

        completed = adapter.run(context)
        cancelled = adapter.cancel(context)

        assert completed.cancellation_requested is True
        assert completed.cancellation_recorded is False
        assert cancelled.status is ExternalAgentRunStatus.CANCELLED
        assert cancelled.cancellation_requested is True
        assert cancelled.cancellation_recorded is True


class TestProbeRedaction:
    def test_probe_output_redacts_secrets(self) -> None:
        adapter = RecordingAdapter(
            probe_summary="Authorization: Basic dXNlcjpzZWNyZXQ= token=abc123",
            probe_details=(
                "Authorization: Bearer live-token",
                "api_key=sk-test-12345",
            ),
            probe_metadata={
                "header": "Authorization: Basic dXNlcjpzZWNyZXQ=",
                "nested": {"token": "Bearer should-hide"},
            },
        )

        result = adapter.probe()

        assert "dXNlcjpzZWNyZXQ" not in result.summary
        assert "live-token" not in result.details[0]
        assert "sk-test-12345" not in result.details[1]
        assert result.summary == "Authorization: Basic *** token=***"
        assert result.details == ("Authorization: Bearer ***", "api_key=***")
        assert result.metadata["header"] == "Authorization: Basic ***"
        assert result.metadata["nested"]["token"] == "Bearer ***"

    def test_unavailable_probe_is_redacted_too(self) -> None:
        adapter = UnavailableExternalAgentAdapter(
            adapter_id="missing",
            reason="Authorization: Basic topsecret token=abc123",
        )

        result = adapter.probe()

        assert result.available is False
        assert "topsecret" not in result.summary
        assert result.summary == "Authorization: Basic *** token=***"
