"""Filesystem storage for eval run metadata and results."""

from __future__ import annotations

import json
from pathlib import Path

from maxwell_daemon.contracts import require
from maxwell_daemon.evals.models import EvalResult, EvalRun


class EvalRunStore:
    """Persist eval runs as JSON under a caller-selected directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser()
        self._root.mkdir(parents=True, exist_ok=True)

    def save(self, run: EvalRun, results: list[EvalResult]) -> Path:
        run_dir = self._root / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(run.model_dump_json(indent=2), encoding="utf-8")
        (run_dir / "results.json").write_text(
            "[\n" + ",\n".join(result.model_dump_json(indent=2) for result in results) + "\n]\n",
            encoding="utf-8",
        )
        return run_dir

    def load_run(self, run_id: str) -> EvalRun:
        path = self._root / run_id / "run.json"
        require(path.is_file(), f"eval run not found: {run_id}")
        return EvalRun.model_validate_json(path.read_text(encoding="utf-8"))

    def load_results(self, run_id: str) -> list[EvalResult]:
        path = self._root / run_id / "results.json"
        require(path.is_file(), f"eval results not found: {run_id}")
        raw = path.read_text(encoding="utf-8")
        return [EvalResult.model_validate(item) for item in json.loads(raw)]
