"""Filter preset storage — ~/.config/maxwell-daemon/presets.json."""

from __future__ import annotations

from pathlib import Path

import pytest

from maxwell_daemon.core.presets import FilterPreset, PresetStore


@pytest.fixture
def store(tmp_path: Path) -> PresetStore:
    return PresetStore(tmp_path / "presets.json")


class TestFilterPreset:
    def test_defaults_to_none(self) -> None:
        p = FilterPreset(name="x")
        assert p.status is None
        assert p.kind is None

    def test_equality(self) -> None:
        a = FilterPreset(name="x", status="failed")
        b = FilterPreset(name="x", status="failed")
        assert a == b


class TestPresetStore:
    def test_empty_store_lists_nothing(self, store: PresetStore) -> None:
        assert store.list() == []

    def test_save_and_get(self, store: PresetStore) -> None:
        p = FilterPreset(name="my-triage", status="failed", kind="issue")
        store.save(p)
        loaded = store.get("my-triage")
        assert loaded == p

    def test_overwrite(self, store: PresetStore) -> None:
        store.save(FilterPreset(name="x", status="queued"))
        store.save(FilterPreset(name="x", status="failed"))
        assert store.get("x").status == "failed"  # type: ignore[union-attr]

    def test_list_alphabetical(self, store: PresetStore) -> None:
        for n in ("zeta", "alpha", "mu"):
            store.save(FilterPreset(name=n))
        assert [p.name for p in store.list()] == ["alpha", "mu", "zeta"]

    def test_delete(self, store: PresetStore) -> None:
        store.save(FilterPreset(name="x"))
        assert store.delete("x") is True
        assert store.get("x") is None

    def test_delete_missing_returns_false(self, store: PresetStore) -> None:
        assert store.delete("ghost") is False

    def test_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        s1 = PresetStore(path)
        s1.save(FilterPreset(name="x", repo="owner/r"))
        s2 = PresetStore(path)
        assert s2.get("x").repo == "owner/r"  # type: ignore[union-attr]

    def test_invalid_name_rejected(self, store: PresetStore) -> None:
        for bad in ("", "has space", "x/y", "-leading", "dot."):
            with pytest.raises(ValueError):
                store.save(FilterPreset(name=bad))

    def test_corrupted_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text("not json {{{{")
        store = PresetStore(path)
        assert store.list() == []


class TestPresetCLI:
    def _runner(self):  # type: ignore[no-untyped-def]
        from typer.testing import CliRunner

        return CliRunner()

    def test_save_and_list(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from maxwell_daemon.cli import tasks as tasks_cli
        from maxwell_daemon.cli.main import app

        runner = self._runner()  # type: ignore[no-untyped-call]
        presets_path = tmp_path / "presets.json"
        monkeypatch.setattr(tasks_cli, "_presets_path", lambda: presets_path)

        r = runner.invoke(
            app,
            [
                "tasks",
                "preset",
                "save",
                "triage",
                "--status",
                "failed",
                "--kind",
                "issue",
            ],
        )
        assert r.exit_code == 0
        r = runner.invoke(app, ["tasks", "preset", "list"])
        assert r.exit_code == 0
        assert "triage" in r.stdout

    def test_list_preset_applies_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from maxwell_daemon.cli import tasks as tasks_cli
        from maxwell_daemon.cli.main import app

        runner = self._runner()  # type: ignore[no-untyped-call]
        presets_path = tmp_path / "presets.json"
        monkeypatch.setattr(tasks_cli, "_presets_path", lambda: presets_path)

        # Seed a preset directly.
        from maxwell_daemon.core.presets import FilterPreset, PresetStore

        PresetStore(presets_path).save(FilterPreset(name="qf", status="failed", kind="issue"))

        captured: list[str] = []

        def fake_get(url: str, **_: object) -> object:
            captured.append(url)

            class _R:
                status_code = 200

                def raise_for_status(self) -> None: ...
                def json(self) -> list:  # type: ignore[type-arg]
                    return []

            return _R()

        from unittest.mock import patch

        import httpx

        with patch.object(httpx, "get", fake_get):
            r = runner.invoke(app, ["tasks", "list", "--preset", "qf"])

        assert r.exit_code == 0, r.stdout
        assert any("status=failed" in u for u in captured)
        assert any("kind=issue" in u for u in captured)
