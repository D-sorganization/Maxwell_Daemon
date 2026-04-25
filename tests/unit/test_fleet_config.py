"""Unit tests for maxwell_daemon.config.fleet — declarative multi-repo fleet config.

``fleet.yaml`` is a separate file from ``maxwell-daemon.yaml``: it lists the repos
managed by the fleet plus shared defaults, and is loaded via a priority order
(explicit path → CWD → ``~/.maxwell-daemon/fleet.yaml`` → env var).
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from maxwell_daemon.config.fleet import (
    FleetDefaults,
    FleetManifest,
    FleetManifestError,
    FleetRepoEntry,
    load_fleet_manifest,
)


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).lstrip())
    return path


class TestFleetRepoEntry:
    def test_raw_fields_are_none_when_unset(self) -> None:
        """Unset inheritable fields stay ``None`` until resolve() folds in defaults."""
        entry = FleetRepoEntry.model_validate({"name": "Foo", "org": "acme"})
        assert entry.name == "Foo"
        assert entry.org == "acme"
        assert entry.slots is None
        assert entry.pr_target_branch is None
        assert entry.pr_fallback_to_default is None
        assert entry.watch_labels is None
        assert entry.budget_per_story is None
        # ``enabled`` is not inheritable — it's a per-repo decision.
        assert entry.enabled is True
        assert entry.full_name == "acme/Foo"

    def test_full_name_joins(self) -> None:
        entry = FleetRepoEntry.model_validate({"name": "Bar", "org": "acme"})
        assert entry.full_name == "acme/Bar"

    def test_rejects_missing_org(self) -> None:
        with pytest.raises(ValueError):
            FleetRepoEntry.model_validate({"name": "Foo"})

    def test_rejects_slot_below_one(self) -> None:
        with pytest.raises(ValueError):
            FleetRepoEntry.model_validate({"name": "Foo", "org": "acme", "slots": 0})

    def test_per_repo_overrides_applied(self) -> None:
        entry = FleetRepoEntry.model_validate(
            {
                "name": "Foo",
                "org": "acme",
                "slots": 5,
                "pr_target_branch": "main",
                "pr_fallback_to_default": False,
                "enabled": False,
                "watch_labels": ["deliver"],
                "budget_per_story": 1.50,
            }
        )
        assert entry.slots == 5
        assert entry.pr_target_branch == "main"
        assert entry.pr_fallback_to_default is False
        assert entry.enabled is False
        assert entry.watch_labels == ["deliver"]
        assert entry.budget_per_story == 1.50


class TestFleetManifestValidation:
    def test_happy_path_parses(self) -> None:
        manifest = FleetManifest.model_validate(
            {
                "version": 1,
                "fleet": {"name": "Test Fleet"},
                "repos": [
                    {"name": "R1", "org": "a"},
                    {"name": "R2", "org": "a", "enabled": False},
                ],
            }
        )
        assert manifest.version == 1
        assert manifest.fleet.name == "Test Fleet"
        assert len(manifest.repos) == 2

    def test_active_repos_filters_disabled(self) -> None:
        manifest = FleetManifest.model_validate(
            {
                "version": 1,
                "fleet": {"name": "f"},
                "repos": [
                    {"name": "R1", "org": "a", "enabled": True},
                    {"name": "R2", "org": "a", "enabled": False},
                    {"name": "R3", "org": "a"},
                ],
            }
        )
        active = manifest.active_repos()
        assert [r.name for r in active] == ["R1", "R3"]

    def test_duplicate_repo_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            FleetManifest.model_validate(
                {
                    "version": 1,
                    "fleet": {"name": "f"},
                    "repos": [
                        {"name": "R1", "org": "a"},
                        {"name": "R1", "org": "b"},
                    ],
                }
            )

    def test_version_must_be_one(self) -> None:
        with pytest.raises(ValueError):
            FleetManifest.model_validate(
                {
                    "version": 99,
                    "fleet": {"name": "f"},
                    "repos": [],
                }
            )

    def test_defaults_apply_to_repos_on_lookup(self) -> None:
        manifest = FleetManifest.model_validate(
            {
                "version": 1,
                "fleet": {
                    "name": "f",
                    "default_slots": 4,
                    "default_pr_target_branch": "trunk",
                    "default_budget_per_story": "0.25",
                    "default_watch_labels": ["maxwell:ready"],
                },
                "repos": [
                    {"name": "R1", "org": "a"},  # inherits
                    {
                        "name": "R2",
                        "org": "a",
                        "slots": 9,
                        "pr_target_branch": "main",
                    },  # overrides
                ],
            }
        )
        r1 = manifest.resolve("R1")
        assert r1.slots == 4
        assert r1.pr_target_branch == "trunk"
        assert r1.budget_per_story == 0.25
        assert r1.watch_labels == ["maxwell:ready"]

        r2 = manifest.resolve("R2")
        assert r2.slots == 9
        assert r2.pr_target_branch == "main"
        # Unset fields still inherit.
        assert r2.budget_per_story == 0.25


class TestLoadFleetManifest:
    def test_loads_from_explicit_path(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "fleet.yaml",
            """
            version: 1
            fleet:
              name: Explicit
            repos:
              - {name: R, org: a}
            """,
        )
        manifest = load_fleet_manifest(path=path)
        assert manifest.fleet.name == "Explicit"

    def test_priority_cwd_over_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        home_maxwell = tmp_path / "home" / ".maxwell-daemon"
        home_maxwell.mkdir(parents=True)

        _write(
            cwd / "fleet.yaml",
            """
            version: 1
            fleet: {name: CWD}
            repos: []
            """,
        )
        _write(
            home_maxwell / "fleet.yaml",
            """
            version: 1
            fleet: {name: HOME}
            repos: []
            """,
        )
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        manifest = load_fleet_manifest()
        assert manifest.fleet.name == "CWD"

    def test_falls_back_to_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        empty_cwd = tmp_path / "empty"
        empty_cwd.mkdir()
        home_maxwell = tmp_path / "home" / ".maxwell-daemon"
        home_maxwell.mkdir(parents=True)
        _write(
            home_maxwell / "fleet.yaml",
            """
            version: 1
            fleet: {name: HOME}
            repos: []
            """,
        )
        monkeypatch.chdir(empty_cwd)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("MAXWELL_FLEET_CONFIG", raising=False)
        manifest = load_fleet_manifest()
        assert manifest.fleet.name == "HOME"

    def test_env_override_wins_over_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        _write(
            cwd / "fleet.yaml",
            """
            version: 1
            fleet: {name: CWD}
            repos: []
            """,
        )
        env_dir = tmp_path / "env"
        env_dir.mkdir()
        env_file = _write(
            env_dir / "custom.yaml",
            """
            version: 1
            fleet: {name: ENV}
            repos: []
            """,
        )
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("MAXWELL_FLEET_CONFIG", str(env_file))
        manifest = load_fleet_manifest()
        assert manifest.fleet.name == "ENV"

    def test_missing_everywhere_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        monkeypatch.setenv("HOME", str(empty))
        monkeypatch.delenv("MAXWELL_FLEET_CONFIG", raising=False)
        with pytest.raises(FleetManifestError, match=r"no fleet\.yaml found"):
            load_fleet_manifest()

    def test_malformed_yaml_surfaces_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "fleet.yaml"
        bad.write_text("not: [valid: yaml")
        with pytest.raises(FleetManifestError):
            load_fleet_manifest(path=bad)


class TestFleetDefaults:
    def test_baseline_defaults(self) -> None:
        d = FleetDefaults.model_validate({"name": "f"})
        assert d.default_slots == 2
        assert d.default_pr_target_branch == "staging"
        assert d.default_budget_per_story == 0.50
        assert d.discovery_interval_seconds == 300
        assert d.auto_promote_staging is False
