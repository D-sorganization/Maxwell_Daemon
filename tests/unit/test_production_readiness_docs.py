"""Production readiness documentation contract checks."""

from pathlib import Path

DOC = Path("docs/operations/production-readiness.md")


def test_production_readiness_doc_is_in_mkdocs_nav() -> None:
    mkdocs = Path("mkdocs.yml").read_text(encoding="utf-8")

    assert "Production release readiness: operations/production-readiness.md" in mkdocs


def test_production_readiness_covers_release_gates() -> None:
    doc = DOC.read_text(encoding="utf-8")

    for required in (
        "no critical beta bug remains open",
        "multi-day soak test",
        "Uptime target",
        "support response expectations",
        "Upgrade and rollback paths",
    ):
        assert required in doc


def test_production_readiness_covers_enterprise_and_artifact_scope() -> None:
    doc = DOC.read_text(encoding="utf-8")

    for required in (
        "SAML",
        "Role-based access control",
        "audit log export",
        "Versioned Docker image",
        "Versioned Helm chart",
        "Signed macOS and Windows desktop installers",
        "Pricing and licensing",
    ):
        assert required in doc
