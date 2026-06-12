"""Guard that ``docs/reference/openapi.md`` stays in sync with the live schema.

This replaces the orphaned root-level ``test_schema.py`` scratch script (#983),
which was never collected because ``testpaths = ["tests"]``. The documented
"Live route inventory" must list exactly the operator routes the FastAPI app
actually exposes — drift in either direction is a documentation bug.
"""

from __future__ import annotations

import re
from pathlib import Path

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENAPI_DOC = _REPO_ROOT / "docs" / "reference" / "openapi.md"


class _OpenAPIDocsDaemon:
    """Minimal daemon stand-in: ``create_app`` only needs ``_config`` to build
    the schema, so we avoid booting a real Daemon/event loop here."""

    def __init__(self, config: MaxwellDaemonConfig) -> None:
        self._config = config


def _documented_paths() -> set[str]:
    doc = _OPENAPI_DOC.read_text(encoding="utf-8")
    section = doc.split("## Live route inventory", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    return set(re.findall(r"`(/[^`]+)`", section))


def _make_config() -> MaxwellDaemonConfig:
    # A minimally-valid config: the schema only needs a registered backend so
    # ``default_backend`` validation passes; no backend is actually invoked.
    return MaxwellDaemonConfig.model_validate(
        {
            "backends": {"primary": {"type": "recording", "model": "docs-sync"}},
            "agent": {"default_backend": "primary"},
        }
    )


def _schema_paths() -> set[str]:
    app = create_app(_OpenAPIDocsDaemon(_make_config()))
    return set(app.openapi()["paths"])


def test_documented_routes_exist_in_schema() -> None:
    documented = _documented_paths()
    schema = _schema_paths()
    missing = documented - schema
    assert not missing, f"Documented routes absent from live schema: {sorted(missing)}"


def test_schema_routes_are_documented() -> None:
    documented = _documented_paths()
    schema = _schema_paths()
    undocumented = schema - documented
    assert not undocumented, (
        "Live routes missing from docs/reference/openapi.md "
        f"'Live route inventory': {sorted(undocumented)}"
    )
