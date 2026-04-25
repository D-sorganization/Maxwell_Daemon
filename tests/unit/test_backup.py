from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from maxwell_daemon.core.backup import BackupManager, RestoreError


def test_restore_rejects_path_traversal_members(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    payload = tmp_path / "payload.txt"
    payload.write_text("unsafe", encoding="utf-8")

    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")

    manager = BackupManager(config_path=tmp_path / "config.yaml", data_dir=tmp_path / "data")
    with pytest.raises(RestoreError, match="unsafe archive member path"):
        manager.restore(archive)


def test_export_json_rejects_unsafe_sqlite_identifiers(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "ledger.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute('CREATE TABLE "table with space" (id INTEGER PRIMARY KEY, value TEXT)')
        conn.execute('INSERT INTO "table with space" (value) VALUES (?)', ("ok",))
        conn.commit()
    finally:
        conn.close()

    manager = BackupManager(config_path=tmp_path / "config.yaml", data_dir=data_dir)
    with pytest.raises(RestoreError, match="unsafe SQLite table name"):
        manager.export_json("ledger")
