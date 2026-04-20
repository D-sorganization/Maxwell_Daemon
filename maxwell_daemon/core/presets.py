"""Named filter presets for ``maxwell-daemon tasks list``.

Persisted as a JSON file under the user's config dir. No server-side state —
purely a convenience to avoid typing ``--status failed --kind issue --repo …``
repeatedly.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

__all__ = ["FilterPreset", "PresetStore"]


_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


@dataclass(slots=True, frozen=True)
class FilterPreset:
    name: str
    status: str | None = None
    kind: str | None = None
    repo: str | None = None
    limit: int | None = None


class PresetStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def save(self, preset: FilterPreset) -> None:
        if not _NAME_RE.match(preset.name):
            raise ValueError(
                f"Invalid preset name {preset.name!r}: use letters, digits, _ or -; "
                "must start with a letter."
            )
        data = self._load()
        data[preset.name] = asdict(preset)
        self._write(data)

    def get(self, name: str) -> FilterPreset | None:
        data = self._load().get(name)
        return FilterPreset(**data) if data else None

    def list(self) -> list[FilterPreset]:
        return sorted(
            (FilterPreset(**v) for v in self._load().values()),
            key=lambda p: p.name,
        )

    def delete(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.is_file():
            return {}
        try:
            data: dict[str, dict[str, Any]] = json.loads(self._path.read_text())
            return data
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self._path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
