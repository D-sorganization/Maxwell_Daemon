"""gRPC status documentation contract checks."""

from pathlib import Path

COVERAGE_DOC = Path("docs/community/documentation-coverage.md")
GRPC_DOC = Path("docs/reference/grpc.md")


def test_grpc_status_page_covers_generated_client_guidance() -> None:
    doc = GRPC_DOC.read_text(encoding="utf-8")

    assert "Generated-client Guidance" in doc
    assert 'pip install "maxwell-daemon[grpc]"' in doc
    assert "python -m grpc_tools.protoc" in doc
    assert "buf generate" in doc
    assert "git diff --exit-code" in doc
    assert "roadmap-only" in doc


def test_documentation_coverage_tracks_grpc_status_as_shipped() -> None:
    coverage = COVERAGE_DOC.read_text(encoding="utf-8")
    normalized = " ".join(coverage.lower().split())

    assert "| gRPC reference |" in coverage
    assert "tests/unit/test_grpc_status_docs.py" in coverage
    assert "shipped" in normalized
    assert "roadmap-only boundary explicit" in normalized
