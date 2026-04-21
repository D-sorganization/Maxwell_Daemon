"""Declarative multi-repo fleet configuration.

``fleet.yaml`` is a separate file from ``maxwell-daemon.yaml``: it lists the repos
the fleet manages plus shared defaults. The priority order is explicit path →
``MAXWELL_FLEET_CONFIG`` env var → ``./fleet.yaml`` → ``~/.maxwell-daemon/fleet.yaml``.
The CLI and the remote-dispatch workflow both read from the same manifest so
"which repos are we managing" has exactly one source of truth.

See GitHub issue #67 for the full schema spec.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FleetDefaults",
    "FleetManifest",
    "FleetManifestError",
    "FleetRepoEntry",
    "load_fleet_manifest",
]


class FleetManifestError(RuntimeError):
    """Raised when a fleet manifest can't be located or parsed."""


class FleetDefaults(BaseModel):
    """Fleet-wide defaults applied to every repo unless overridden."""

    model_config = ConfigDict(extra="forbid")

    name: str
    auto_promote_staging: bool = False
    discovery_interval_seconds: int = Field(300, ge=10)
    default_slots: int = Field(2, ge=1, le=32)
    default_budget_per_story: float = Field(0.50, ge=0)
    default_pr_target_branch: str = "staging"
    default_pr_fallback_to_default: bool = True
    default_watch_labels: list[str] = Field(default_factory=list)


class FleetRepoEntry(BaseModel):
    """One repo managed by the fleet. Any field left unset inherits from FleetDefaults."""

    model_config = ConfigDict(extra="forbid")

    name: str
    org: str
    slots: int | None = Field(None, ge=1, le=32)
    budget_per_story: float | None = Field(None, ge=0)
    pr_target_branch: str | None = None
    pr_fallback_to_default: bool | None = None
    watch_labels: list[str] | None = None
    enabled: bool = True

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.name}"


class _ResolvedRepo(BaseModel):
    """A FleetRepoEntry with all defaults folded in — guaranteed non-None fields.

    Returned from ``FleetManifest.resolve()`` so callers never have to check for
    ``None`` on inheritable fields.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    org: str
    slots: int
    budget_per_story: float
    pr_target_branch: str
    pr_fallback_to_default: bool
    watch_labels: list[str]
    enabled: bool

    @property
    def full_name(self) -> str:
        return f"{self.org}/{self.name}"


class FleetManifest(BaseModel):
    """Top-level ``fleet.yaml`` schema."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    fleet: FleetDefaults
    repos: list[FleetRepoEntry] = Field(default_factory=list)

    @field_validator("repos")
    @classmethod
    def _reject_duplicate_names(
        cls, repos: list[FleetRepoEntry]
    ) -> list[FleetRepoEntry]:
        seen: set[str] = set()
        for r in repos:
            if r.name in seen:
                raise ValueError(f"duplicate repo entry: {r.name!r}")
            seen.add(r.name)
        return repos

    def active_repos(self) -> list[FleetRepoEntry]:
        return [r for r in self.repos if r.enabled]

    def resolve(self, name: str) -> _ResolvedRepo:
        """Return the repo with all defaults folded in.

        Raises ``KeyError`` when ``name`` isn't in the manifest.
        """
        entry = next((r for r in self.repos if r.name == name), None)
        if entry is None:
            raise KeyError(name)
        d = self.fleet
        return _ResolvedRepo(
            name=entry.name,
            org=entry.org,
            slots=entry.slots if entry.slots is not None else d.default_slots,
            budget_per_story=(
                entry.budget_per_story
                if entry.budget_per_story is not None
                else d.default_budget_per_story
            ),
            pr_target_branch=entry.pr_target_branch or d.default_pr_target_branch,
            pr_fallback_to_default=(
                entry.pr_fallback_to_default
                if entry.pr_fallback_to_default is not None
                else d.default_pr_fallback_to_default
            ),
            watch_labels=(
                list(entry.watch_labels)
                if entry.watch_labels is not None
                else list(d.default_watch_labels)
            ),
            enabled=entry.enabled,
        )


_DEFAULT_HOME_SUBPATH = Path(".maxwell-daemon") / "fleet.yaml"
_CWD_FILENAME = "fleet.yaml"
_ENV_VAR = "MAXWELL_FLEET_CONFIG"


def _candidate_paths() -> list[Path]:
    """Resolution order: env var → ./fleet.yaml → ~/.maxwell-daemon/fleet.yaml."""
    paths: list[Path] = []
    env = os.environ.get(_ENV_VAR)
    if env:
        paths.append(Path(env))
    paths.append(Path.cwd() / _CWD_FILENAME)
    home = os.environ.get("HOME")
    if home:
        paths.append(Path(home) / _DEFAULT_HOME_SUBPATH)
    return paths


def load_fleet_manifest(*, path: Path | None = None) -> FleetManifest:
    """Load and validate a fleet manifest.

    When ``path`` is given, load it directly (raises ``FleetManifestError`` if
    missing). Otherwise walk the candidate paths and use the first one that
    exists.
    """
    if path is not None:
        if not path.is_file():
            raise FleetManifestError(f"fleet manifest not found: {path}")
        return _load_from_file(path)

    for candidate in _candidate_paths():
        if candidate.is_file():
            return _load_from_file(candidate)

    raise FleetManifestError(
        "no fleet.yaml found in MAXWELL_FLEET_CONFIG, cwd, or ~/.maxwell-daemon"
    )


def _load_from_file(path: Path) -> FleetManifest:
    try:
        raw: Any = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise FleetManifestError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise FleetManifestError(f"{path} must contain a mapping at top level")
    try:
        return FleetManifest.model_validate(raw)
    except Exception as e:  # pydantic ValidationError or unexpected
        raise FleetManifestError(f"{path} is not a valid fleet manifest: {e}") from e
