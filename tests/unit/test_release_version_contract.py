"""Release/version single-source contract checks (#989)."""

from __future__ import annotations

import re
from pathlib import Path

import tomllib


def _project_version() -> str:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    return version


def test_pyproject_is_the_only_package_version_source() -> None:
    """The dead top-level VERSION file must not reappear."""
    assert not Path("VERSION").exists()
    assert re.fullmatch(r"\d+\.\d+\.\d+", _project_version())


def test_changelog_has_entry_for_project_version() -> None:
    version = _project_version()
    changelog = Path("CHANGELOG.md").read_text(encoding="utf-8")

    assert f"## [{version}] - 2026-06-14" in changelog


def test_deploy_examples_track_current_release_version() -> None:
    version = _project_version()
    docs = {
        Path("docs/reference/api.md"): f'"version": "{version}"',
        Path("docs/operations/wsl2-node-deployment.md"): f'"version": "{version}"',
        Path("deploy/ansible/install-maxwell.yml"): f"maxwell_version={version}",
    }

    for path, expected in docs.items():
        text = path.read_text(encoding="utf-8")
        assert expected in text
        assert "maxwell_version=0.1.0" not in text
