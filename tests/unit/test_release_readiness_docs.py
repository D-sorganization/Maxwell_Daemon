"""Release readiness documentation contract checks."""

from __future__ import annotations

from pathlib import Path

DOC = Path("docs/operations/release-readiness.md")


def test_release_readiness_doc_is_in_mkdocs_nav() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "Beta release readiness: operations/release-readiness.md" in mkdocs


def test_beta_release_readiness_covers_required_artifacts() -> None:
    doc = DOC.read_text(encoding="utf-8")

    for required in (
        "PyPI",
        "Docker image",
        "Helm chart",
        "macOS DMG",
        "Windows MSI",
        "AppImage/Snap",
        "Documentation site",
    ):
        assert required in doc


def test_beta_release_readiness_covers_go_no_go_gates() -> None:
    doc = DOC.read_text(encoding="utf-8")

    for required in (
        "GitHub Actions CI is green",
        "Coverage",
        "mkdocs build --strict",
        "Security review",
        "Zero-to-production smoke path",
    ):
        assert required in doc
