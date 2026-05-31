"""Integration tests for the backup / restore round-trip.

Deliverable 3.4 of #896: ``maxwell_daemon/core/backup.py`` exports all
daemon state to a single ``.tar.gz`` and restores it on a fresh machine.
The unit suite (``tests/unit/test_backup.py``) only pins the security edge
cases (path-traversal, unsafe SQLite identifiers); this module exercises the
*real* export → restore round-trip end-to-end against a populated data
directory, plus a handful of contract tests for ``export_json`` and the
restore guards.

These tests use only local SQLite + the filesystem — no external services —
so they belong in the integration lane per ``CLAUDE.md``.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from maxwell_daemon.core.backup import (
    BackupManager,
    BackupManifest,
    RestoreError,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_sqlite_db(path: Path, *, rows: list[tuple[str, int]]) -> None:
    """Create a tiny SQLite DB with a single ``items`` table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE items (name TEXT, value INTEGER)")
        conn.executemany("INSERT INTO items (name, value) VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def _populate_state(data_dir: Path, config_path: Path) -> None:
    """Populate a realistic data dir + config that BackupManager understands."""
    data_dir.mkdir(parents=True, exist_ok=True)

    # A couple of the known SQLite components.
    _make_sqlite_db(data_dir / "tasks.db", rows=[("alpha", 1), ("beta", 2)])
    _make_sqlite_db(data_dir / "ledger.db", rows=[("cost", 42)])

    # Audit log (hash-chained jsonl — content is opaque to backup).
    (data_dir / "audit.jsonl").write_text('{"event":"one"}\n{"event":"two"}\n', encoding="utf-8")

    # Artifact blob tree.
    blob_dir = data_dir / "artifacts" / "sub"
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / "blob.bin").write_bytes(b"\x00\x01\x02artifact-bytes")

    # Memory markdown tree.
    mem_dir = data_dir / ".maxwell" / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / "MEMORY.md").write_text("# durable memory\n", encoding="utf-8")

    # Config YAML with a plaintext secret that must be redacted.
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "backends:\n  primary:\n    type: anthropic\n    api_key: sk-secret-plaintext-value\n",
        encoding="utf-8",
    )


@pytest.fixture
def populated(tmp_path: Path) -> tuple[BackupManager, Path, Path]:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "config" / "maxwell-daemon.yaml"
    _populate_state(data_dir, config_path)
    mgr = BackupManager(config_path=config_path, data_dir=data_dir)
    return mgr, data_dir, config_path


# ── round-trip (headline) ─────────────────────────────────────────────────────


class TestRoundTrip:
    def test_export_then_restore_reproduces_all_state(
        self, populated: tuple[BackupManager, Path, Path], tmp_path: Path
    ) -> None:
        mgr, _src_data, _src_config = populated

        archive = mgr.export(tmp_path / "backup.tar.gz")
        assert archive.exists()

        # Restore into a brand-new, empty data dir + config location.
        dst_data = tmp_path / "restored_data"
        dst_config = tmp_path / "restored_config" / "maxwell-daemon.yaml"
        restore_mgr = BackupManager(config_path=dst_config, data_dir=dst_data)
        restore_mgr.restore(archive)

        # SQLite DBs round-trip with their rows intact.
        conn = sqlite3.connect(str(dst_data / "tasks.db"))
        try:
            rows = conn.execute("SELECT name, value FROM items ORDER BY name").fetchall()
        finally:
            conn.close()
        assert rows == [("alpha", 1), ("beta", 2)]

        # Audit log content preserved byte-for-byte.
        assert (dst_data / "audit.jsonl").read_text(
            encoding="utf-8"
        ) == '{"event":"one"}\n{"event":"two"}\n'

        # Artifact blob bytes preserved.
        assert (
            dst_data / "artifacts" / "sub" / "blob.bin"
        ).read_bytes() == b"\x00\x01\x02artifact-bytes"

        # Memory markdown preserved.
        assert (dst_data / ".maxwell" / "memory" / "MEMORY.md").read_text(
            encoding="utf-8"
        ) == "# durable memory\n"

        # Config restored, but the plaintext secret was redacted at export time.
        restored_config = dst_config.read_text(encoding="utf-8")
        assert "sk-secret-plaintext-value" not in restored_config
        assert "<REDACTED>" in restored_config

    def test_restore_refuses_to_overwrite_without_force(
        self, populated: tuple[BackupManager, Path, Path], tmp_path: Path
    ) -> None:
        mgr, _data, _config = populated
        archive = mgr.export(tmp_path / "backup.tar.gz")

        # Restoring back over the *same* (already-populated) locations must
        # refuse without force, protecting live state.
        with pytest.raises(RestoreError, match="already exists"):
            mgr.restore(archive)

    def test_restore_with_force_overwrites_existing_state(
        self, populated: tuple[BackupManager, Path, Path], tmp_path: Path
    ) -> None:
        mgr, data_dir, _config = populated
        archive = mgr.export(tmp_path / "backup.tar.gz")

        # Mutate live state, then force-restore the archive on top of it.
        (data_dir / "tasks.db").unlink()
        _make_sqlite_db(data_dir / "tasks.db", rows=[("mutated", 999)])

        mgr.restore(archive, force=True)

        conn = sqlite3.connect(str(data_dir / "tasks.db"))
        try:
            rows = conn.execute("SELECT name, value FROM items ORDER BY name").fetchall()
        finally:
            conn.close()
        assert rows == [("alpha", 1), ("beta", 2)]


# ── contract tests ─────────────────────────────────────────────────────────────


class TestExportJsonContract:
    def test_export_json_dumps_sqlite_rows(
        self, populated: tuple[BackupManager, Path, Path]
    ) -> None:
        mgr, _data, _config = populated
        dump = mgr.export_json("tasks")
        assert dump["component"] == "tasks"
        assert dump["tables"]["items"] == [
            {"name": "alpha", "value": 1},
            {"name": "beta", "value": 2},
        ]

    def test_export_json_redacts_config_secrets(
        self, populated: tuple[BackupManager, Path, Path]
    ) -> None:
        mgr, _data, _config = populated
        dump = mgr.export_json("config")
        assert dump["backends"]["primary"]["api_key"] == "<REDACTED>"
        assert dump["backends"]["primary"]["type"] == "anthropic"

    def test_export_json_rejects_unknown_component(
        self, populated: tuple[BackupManager, Path, Path]
    ) -> None:
        mgr, _data, _config = populated
        with pytest.raises(ValueError, match="unknown component"):
            mgr.export_json("nonexistent")


class TestRestoreGuards:
    def test_restore_rejects_unknown_schema_version(
        self, populated: tuple[BackupManager, Path, Path], tmp_path: Path
    ) -> None:
        mgr, _data, _config = populated
        archive = mgr.export(tmp_path / "backup.tar.gz")

        # Rebuild the archive with a bumped, unsupported schema version.
        tampered = tmp_path / "tampered.tar.gz"
        _rewrite_manifest_schema(archive, tampered, schema_version="999")

        dst = BackupManager(
            config_path=tmp_path / "rc" / "maxwell-daemon.yaml",
            data_dir=tmp_path / "rd",
        )
        with pytest.raises(RestoreError, match="unsupported backup schema version"):
            dst.restore(tampered)

    def test_restore_detects_corrupted_archive_hash(
        self, populated: tuple[BackupManager, Path, Path], tmp_path: Path
    ) -> None:
        mgr, _data, _config = populated
        archive = mgr.export(tmp_path / "backup.tar.gz")

        tampered = tmp_path / "corrupt.tar.gz"
        _corrupt_audit_member(archive, tampered)

        dst = BackupManager(
            config_path=tmp_path / "rc2" / "maxwell-daemon.yaml",
            data_dir=tmp_path / "rd2",
        )
        with pytest.raises(RestoreError, match="hash mismatch"):
            dst.restore(tampered)

    def test_missing_archive_raises(self, tmp_path: Path) -> None:
        mgr = BackupManager(config_path=tmp_path / "c.yaml", data_dir=tmp_path / "d")
        with pytest.raises(RestoreError, match="archive not found"):
            mgr.restore(tmp_path / "does-not-exist.tar.gz")

    def test_manifest_round_trips_through_dict(self) -> None:
        manifest = BackupManifest.create(
            hashes={"tasks": "abc"},
            config_path=Path("/tmp/cfg.yaml"),
            data_dir=Path("/tmp/data"),
        )
        clone = BackupManifest.from_dict(manifest.to_dict())
        assert clone.schema_version == manifest.schema_version
        assert clone.hashes == {"tasks": "abc"}


# ── archive-tampering utilities (used by guard tests) ──────────────────────────


def _rewrite_manifest_schema(src: Path, dst: Path, *, schema_version: str) -> None:
    """Repack *src* archive with the manifest's schema_version overwritten."""
    with tarfile.open(src, "r:gz") as tar, tarfile.open(dst, "w:gz") as out:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if member.name.endswith("manifest.json"):
                manifest = json.loads(data.decode("utf-8"))
                manifest["schema_version"] = schema_version
                data = json.dumps(manifest).encode("utf-8")
                member.size = len(data)
            import io

            out.addfile(member, io.BytesIO(data))


def _corrupt_audit_member(src: Path, dst: Path) -> None:
    """Repack *src* with the audit.jsonl bytes mutated so its hash mismatches."""
    with tarfile.open(src, "r:gz") as tar, tarfile.open(dst, "w:gz") as out:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            data = extracted.read() if extracted is not None else b""
            if member.name.endswith("audit/audit.jsonl"):
                data = data + b"tampered-extra-bytes\n"
                member.size = len(data)
            import io

            out.addfile(member, io.BytesIO(data))


def test_archive_is_gzip_compressed_tar(
    populated: tuple[BackupManager, Path, Path], tmp_path: Path
) -> None:
    """Sanity: the produced archive really is a gzip-compressed tar."""
    mgr, _data, _config = populated
    archive = mgr.export(tmp_path / "backup.tar.gz")
    with gzip.open(archive, "rb") as fh:
        head = fh.read(2)
    # ustar/gnu tar magic appears at offset 257, but a successful tarfile open
    # is the real contract:
    assert head  # decompresses without error
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert any(n.endswith("manifest.json") for n in names)
