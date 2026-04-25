"""Unit tests for the external agent adapter contract layer."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from maxwell_daemon.backends.base import (
    BackendCapabilities,
    BackendResponse,
    BackendUnavailableError,
    ILLMBackend,
    Message,
    TokenUsage,
)
from maxwell_daemon.backends.external_adapter import (
    BackendReadOnlyExternalAgentAdapter,
    ClaudeCodeCLIExternalAgentAdapter,
    CodexCLIExternalAgentAdapter,
    ContinueCLIExternalAgentAdapter,
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
    JulesCLIExternalAgentAdapter,
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
            else ExternalAgentCapability(supported_operations=frozenset(ExternalAgentOperation))
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
            changed_files=(() if context.read_only else ("src/app.py", "tests/test_app.py")),
            artifacts=("artifacts/report.json",),
            read_only=context.read_only,
            cancellation_requested=context.cancellation_requested,
            cancellation_recorded=False,
        )


class FakeLLMBackend(ILLMBackend):
    name = "fake-codex"

    def __init__(self) -> None:
        self.health_checks = 0
        self.seen_messages: list[list[Message]] = []
        self.response = BackendResponse(
            content="adapter response",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model="gpt-5-codex",
            backend=self.name,
            raw={"stdout": "adapter response"},
        )
        self.unavailable_error: BackendUnavailableError | None = None

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> BackendResponse:
        _ = (model, temperature, max_tokens, tools, kwargs)
        self.seen_messages.append(messages)
        if self.unavailable_error is not None:
            raise self.unavailable_error
        return self.response

    async def stream(
        self,
        messages: list[Message],
        *,
        model: str,
        temperature: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        _ = (messages, model, temperature, max_tokens, tools, kwargs)
        yield self.response.content

    async def health_check(self) -> bool:
        self.health_checks += 1
        return True

    def capabilities(self, model: str) -> BackendCapabilities:
        _ = model
        return BackendCapabilities(
            supports_streaming=False,
            supports_tool_use=True,
            supports_vision=False,
            supports_system_prompt=True,
            max_context_tokens=128_000,
            is_local=False,
            cost_per_1k_input_tokens=0.001,
            cost_per_1k_output_tokens=0.002,
        )


class TestRegistry:
    def test_empty_adapter_ids_rejected(self) -> None:
        registry = ExternalAgentAdapterRegistry()

        with pytest.raises(ExternalAgentAdapterError, match="cannot be empty"):
            registry.register(RecordingAdapter(adapter_id=""))

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

    def test_capability_adapter_id_must_match_registration_id(self) -> None:
        registry = ExternalAgentAdapterRegistry()
        adapter = RecordingAdapter(
            adapter_id="alpha",
            capabilities=ExternalAgentCapability(
                adapter_id="beta",
                supported_operations=frozenset({ExternalAgentOperation.PLAN}),
            ),
        )

        with pytest.raises(ExternalAgentAdapterError, match="capability id does not match"):
            registry.register(adapter)


class TestCapabilityContract:
    def test_required_operations_are_modeled(self) -> None:
        assert {operation.value for operation in ExternalAgentOperation} >= {
            "probe",
            "plan",
            "implement",
            "review",
            "validate",
            "checkpoint",
            "cancel",
        }

    def test_capability_records_adapter_metadata(self) -> None:
        capability = ExternalAgentCapability(
            adapter_id="codex-cli",
            display_name="Codex CLI",
            version="0.5.0",
            probe_info=("codex --version",),
            supported_roles=frozenset({"planner", "reviewer"}),
            supported_operations=frozenset({ExternalAgentOperation.PLAN}),
            capability_tags=frozenset({"cli", "non-interactive"}),
            context_limits={"max_context_tokens": 128_000},
            cost_model="provider account",
            quota_model="provider quota",
            required_credentials=("codex login",),
            required_binaries=("codex",),
            workspace_requirements=("workspace required for implement",),
            can_edit_files=False,
            can_run_tests=False,
            supports_background=True,
            safety_notes=("does not merge PRs",),
        )

        assert capability.adapter_id == "codex-cli"
        assert capability.supports(ExternalAgentOperation.PLAN) is True
        assert capability.context_limits["max_context_tokens"] == 128_000
        assert capability.required_binaries == ("codex",)
        assert capability.supports_background is True


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
                supported_operations=frozenset({ExternalAgentOperation.IMPLEMENT})
            )
        )

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="recording",
                operation=ExternalAgentOperation.IMPLEMENT,
                prompt="update file",
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert (
            result.unavailable_reason
            == "workspace assignment required for write-capable operations"
        )

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

    def test_read_only_operations_reject_reported_file_mutations(self) -> None:
        result_template = ExternalAgentRunResult.completed(
            adapter_id="recording",
            operation=ExternalAgentOperation.REVIEW,
            summary="mutated",
            changed_files=("src/app.py",),
        )
        adapter = RecordingAdapter(
            capabilities=ExternalAgentCapability(
                supported_operations=frozenset({ExternalAgentOperation.REVIEW})
            ),
            run_result=result_template,
        )

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="recording",
                operation=ExternalAgentOperation.REVIEW,
                prompt="review only",
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.changed_files == ()
        assert result.unavailable_reason == "read-only operation reported changed files: review"
        assert result.policy_warnings == (
            "Read-only adapter operation returned changed_files and was rejected.",
        )

    def test_result_preserves_changed_files_and_artifacts(self) -> None:
        result = ExternalAgentRunResult.completed(
            adapter_id="recording",
            operation=ExternalAgentOperation.WRITE,
            summary="done",
            changed_files=("src/a.py", "src/b.py"),
            commands_run=("python -m pytest tests/unit/test_app.py",),
            tests_run=("tests/unit/test_app.py",),
            artifacts=("artifact-one.json", "artifact-two.json"),
            checkpoint="ready to resume",
            policy_warnings=("needs manual approval",),
        )

        assert result.changed_files == ("src/a.py", "src/b.py")
        assert result.commands_run == ("python -m pytest tests/unit/test_app.py",)
        assert result.tests_run == ("tests/unit/test_app.py",)
        assert result.artifacts == ("artifact-one.json", "artifact-two.json")
        assert result.checkpoint == "ready to resume"
        assert result.policy_warnings == ("needs manual approval",)

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

    def test_run_result_redacts_secret_bearing_evidence(self) -> None:
        result = ExternalAgentRunResult.completed(
            adapter_id="recording",
            operation=ExternalAgentOperation.PLAN,
            summary="token=abc123",
            details=("Authorization: Bearer live-token",),
            commands_run=("tool --token=abc123",),
            stdout_snippet="api_key=sk-test-12345",
            stderr_snippet="password=hunter2",
            checkpoint="Bearer should-hide",
            metadata={"nested": {"token": "Bearer should-hide"}},
        )

        assert result.summary == "token=***"
        assert result.details == ("Authorization: Bearer ***",)
        assert result.commands_run == ("tool --token=***",)
        assert result.stdout_snippet == "api_key=***"
        assert result.stderr_snippet == "password=***"
        assert result.checkpoint == "Bearer ***"
        assert result.metadata["nested"]["token"] == "Bearer ***"


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


class TestCodexCLIExternalAgentAdapter:
    def test_probe_uses_existing_backend_health_check(self) -> None:
        backend = FakeLLMBackend()
        adapter = CodexCLIExternalAgentAdapter(backend=backend, version="0.5.0")

        result = adapter.probe()

        assert result.available is True
        assert result.version == "0.5.0"
        assert backend.health_checks == 1
        assert adapter.capabilities.adapter_id == "codex-cli"
        assert adapter.capabilities.required_binaries == ("codex",)
        assert adapter.capabilities.can_edit_files is False

    def test_read_only_codex_plan_runs_through_backend_contract(self) -> None:
        backend = FakeLLMBackend()
        backend.response = BackendResponse(
            content="plan output",
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            model="gpt-5-codex",
            backend=backend.name,
            raw={"stdout": "token=abc123"},
        )
        adapter = CodexCLIExternalAgentAdapter(backend=backend)

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="codex-cli",
                operation=ExternalAgentOperation.PLAN,
                prompt="build a plan",
            )
        )

        assert result.status is ExternalAgentRunStatus.COMPLETED
        assert result.summary == "plan output"
        assert result.read_only is True
        assert result.changed_files == ()
        assert result.commands_run == ("codex exec --approval suggest --model gpt-5-codex",)
        assert result.cost_estimate_usd == pytest.approx(0.0002)
        assert backend.seen_messages[0][0].role.value == "system"
        assert "Do not edit files" in backend.seen_messages[0][0].content
        assert result.metadata["raw"]["stdout"] == "token=***"

    def test_codex_wrapper_declares_implement_unsupported_until_write_mode_exists(
        self, tmp_path: Path
    ) -> None:
        adapter = CodexCLIExternalAgentAdapter(backend=FakeLLMBackend())

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="codex-cli",
                operation=ExternalAgentOperation.IMPLEMENT,
                prompt="edit files",
                workspace=tmp_path,
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.unavailable_reason == "unsupported operation: implement"

    def test_backend_unavailable_errors_are_structured_and_redacted(self) -> None:
        backend = FakeLLMBackend()
        backend.unavailable_error = BackendUnavailableError("token=abc123")
        adapter = CodexCLIExternalAgentAdapter(backend=backend)

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id="codex-cli",
                operation=ExternalAgentOperation.REVIEW,
                prompt="review",
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.summary == "Codex CLI backend unavailable: token=***"
        assert result.stderr_snippet == "token=***"


class TestReadOnlyBackendExternalAgentAdapters:
    def test_generic_wrapper_exposes_backend_as_read_only_agent(self) -> None:
        backend = FakeLLMBackend()
        adapter = BackendReadOnlyExternalAgentAdapter(
            backend=backend,
            adapter_id="fake-cli",
            display_name="Fake CLI",
            model="fake-model",
            command_hint="fake run <prompt>",
            probe_info=("fake --version",),
            capability_tags=frozenset({"cli", "fake"}),
            required_binaries=("fake",),
        )

        assert adapter.capabilities.adapter_id == "fake-cli"
        assert adapter.capabilities.supports(ExternalAgentOperation.REVIEW) is True
        assert adapter.capabilities.supports(ExternalAgentOperation.IMPLEMENT) is False
        assert adapter.capabilities.can_edit_files is False
        assert adapter.capabilities.required_binaries == ("fake",)

    @pytest.mark.parametrize(
        ("factory", "adapter_id", "expected_hint"),
        (
            (ContinueCLIExternalAgentAdapter, "continue-cli", "cn ask <prompt>"),
            (
                ClaudeCodeCLIExternalAgentAdapter,
                "claude-code-cli",
                "claude -p <prompt> --model test-model --output-format json",
            ),
            (
                JulesCLIExternalAgentAdapter,
                "jules-cli",
                "jules ask <prompt> --output-format json",
            ),
        ),
    )
    def test_cli_wrappers_run_read_only_operations_through_backend(
        self,
        factory: type[BackendReadOnlyExternalAgentAdapter],
        adapter_id: str,
        expected_hint: str,
    ) -> None:
        backend = FakeLLMBackend()
        adapter = factory(backend=backend, model="test-model")  # type: ignore[call-arg]

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id=adapter_id,
                operation=ExternalAgentOperation.REVIEW,
                prompt="review the patch",
            )
        )

        assert result.status is ExternalAgentRunStatus.COMPLETED
        assert result.read_only is True
        assert result.changed_files == ()
        assert result.commands_run == (expected_hint,)
        assert backend.seen_messages[0][0].role.value == "system"
        assert "Do not edit files" in backend.seen_messages[0][0].content

    @pytest.mark.parametrize(
        ("factory", "adapter_id"),
        (
            (ContinueCLIExternalAgentAdapter, "continue-cli"),
            (ClaudeCodeCLIExternalAgentAdapter, "claude-code-cli"),
            (JulesCLIExternalAgentAdapter, "jules-cli"),
        ),
    )
    def test_cli_wrappers_reject_write_operations_until_policy_exists(
        self,
        factory: type[BackendReadOnlyExternalAgentAdapter],
        adapter_id: str,
        tmp_path: Path,
    ) -> None:
        adapter = factory(backend=FakeLLMBackend())  # type: ignore[call-arg]

        result = adapter.run(
            ExternalAgentRunContext(
                adapter_id=adapter_id,
                operation=ExternalAgentOperation.IMPLEMENT,
                prompt="edit files",
                workspace=tmp_path,
            )
        )

        assert result.status is ExternalAgentRunStatus.UNAVAILABLE
        assert result.unavailable_reason == "unsupported operation: implement"
