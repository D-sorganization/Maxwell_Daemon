"""Backup, restore, and JSON-export for all Maxwell-Daemon state.

Components covered
------------------
- Config file  (~/.config/maxwell-daemon/maxwell-daemon.yaml)
- SQLite databases: tasks, work items, task graphs, actions, artifacts,
  delegate sessions, auth sessions, cost ledger, memory
- Audit log     (.jsonl hash-chained file)
- Artifact blobs (directory tree)
- Memory markdowns (.maxwell/memory & .maxwell/raw_logs)

Archive format
--------------
A gzip-compressed tar archive containing:
  manifest.json        — schema version, component paths, BLAKE3→SHA-256 hashes, timestamp
  secrets.env.example  — placeholder listing secret refs that must be re-entered after restore
  config/              — config YAML (secrets stripped)
  data/                — all SQLite DBs (via sqlite3 Online Backup API)
  audit/               — audit JSONL
  artifacts/           — blob files
  memory/              — markdown & raw-log files

Usage::

    mgr = BackupManager()
    path = mgr.export(Path("~/maxwell-backup.tar.gz"))
    mgr.restore(path)
    data = mgr.export_json("ledger")
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "BackupManager",
    "BackupManifest",
    "RestoreError",
]

_SCHEMA_VERSION = "1"

# Default data-store root (mirrors daemon/runner.py defaults)
_DEFAULT_DATA_DIR = Path.home() / ".local/share/maxwell-daemon"
_DEFAULT_CONFIG_PATH = Path.home() / ".config/maxwell-daemon/maxwell-daemon.yaml"

# Components that correspond to SQLite DBs inside the data dir
_SQLITE_COMPONENTS: dict[str, str] = {
    "tasks": "tasks.db",
    "work_items": "work_items.db",
    "task_graphs": "task_graphs.db",
    "actions": "actions.db",
    "artifacts_db": "artifacts.db",
    "delegate_sessions": "delegate_sessions.db",
    "auth_sessions": "auth_sessions.db",
    "ledger": "ledger.db",
    "memory_db": "memory.db",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_dir(directory: Path) -> dict[str, str]:
    """Return {relative_posix_path: sha256} for all files under *directory*."""
    result: dict[str, str] = {}
    for p in sorted(directory.rglob("*")):
        if p.is_file():
            result[p.relative_to(directory).as_posix()] = _sha256_file(p)
    return result


def _safe_sqlite_backup(src: Path, dst: Path) -> None:
    """Copy *src* SQLite database to *dst* using the Online Backup API."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_conn = sqlite3.connect(str(src), uri=True)
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()


class RestoreError(RuntimeError):
    """Raised when restore validation fails."""


class BackupManifest:
    """Lightweight manifest written into and read from the archive."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @classmethod
    def create(cls, hashes: dict[str, Any], config_path: Path, data_dir: Path) -> BackupManifest:
        return cls(
            {
                "schema_version": _SCHEMA_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "config_path": str(config_path),
                "data_dir": str(data_dir),
                "hashes": hashes,
            }
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BackupManifest:
        return cls(d)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)

    @property
    def schema_version(self) -> str:
        return str(self._data.get("schema_version", ""))

    @property
    def config_path(self) -> Path:
        return Path(self._data.get("config_path", str(_DEFAULT_CONFIG_PATH)))

    @property
    def data_dir(self) -> Path:
        return Path(self._data.get("data_dir", str(_DEFAULT_DATA_DIR)))

    @property
    def hashes(self) -> dict[str, Any]:
        return dict(self._data.get("hashes", {}))


class BackupManager:
    """Backup, restore, and JSON-export controller for Maxwell-Daemon state.

    Parameters
    ----------
    config_path:
        Path to ``maxwell-daemon.yaml``.  Defaults to the XDG/home standard location.
    data_dir:
        Parent directory that holds the SQLite databases, audit log, artifact blobs,
        and memory markdowns.  Defaults to ``~/.local/share/maxwell-daemon``.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self._config_path = (config_path or _DEFAULT_CONFIG_PATH).expanduser()
        self._data_dir = (data_dir or _DEFAULT_DATA_DIR).expanduser()

    # ── public API ────────────────────────────────────────────────────────────

    def export(self, out: Path | str | None = None) -> Path:
        """Create a timestamped .tar.gz backup archive.

        Parameters
        ----------
        out:
            Destination path.  If omitted a timestamped file is created in the
            current working directory.

        Returns
        -------
        Path
            Absolute path to the created archive.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = Path(out).expanduser() if out else Path(f"maxwell-backup-{stamp}.tar.gz")
        dest = dest.resolve()

        with tempfile.TemporaryDirectory(prefix="maxwell-backup-") as tmp_str:
            tmp = Path(tmp_str)
            hashes: dict[str, Any] = {}

            # 1. Config (secrets stripped)
            self._export_config(tmp / "config", hashes)

            # 2. SQLite databases
            for component, filename in _SQLITE_COMPONENTS.items():
                src = self._data_dir / filename
                if src.exists():
                    dst = tmp / "data" / filename
                    _safe_sqlite_backup(src, dst)
                    hashes[component] = _sha256_file(dst)

            # 3. Audit log
            self._export_audit(tmp / "audit", hashes)

            # 4. Artifact blobs
            self._export_artifacts(tmp / "artifacts", hashes)

            # 5. Memory markdowns
            self._export_memory(tmp / "memory", hashes)

            # 6. Manifest
            manifest = BackupManifest.create(hashes, self._config_path, self._data_dir)
            manifest_path = tmp / "manifest.json"
            manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

            # 7. secrets.env.example
            self._write_secrets_example(tmp / "secrets.env.example")

            # 8. Pack
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(dest, "w:gz") as tar:
                tar.add(tmp, arcname="maxwell-backup")

        return dest

    def restore(self, archive: Path | str, *, force: bool = False) -> None:
        """Restore state from a backup archive.

        Parameters
        ----------
        archive:
            Path to the ``.tar.gz`` produced by :meth:`export`.
        force:
            If *False* (default) the restore is aborted when existing data would
            be overwritten and the archive predates the current files.

        Raises
        ------
        RestoreError
            When the manifest is missing, the schema version is unrecognised, or
            any file hash fails verification.
        """
        archive = Path(archive).expanduser().resolve()
        if not archive.exists():
            raise RestoreError(f"archive not found: {archive}")

        with tempfile.TemporaryDirectory(prefix="maxwell-restore-") as tmp_str:
            tmp = Path(tmp_str)

            # Unpack
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(tmp)

            root = tmp / "maxwell-backup"

            # Load manifest
            manifest_path = root / "manifest.json"
            if not manifest_path.exists():
                raise RestoreError("archive is missing manifest.json")
            manifest = BackupManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )

            # Schema check
            if manifest.schema_version != _SCHEMA_VERSION:
                raise RestoreError(
                    f"unsupported backup schema version {manifest.schema_version!r} "
                    f"(expected {_SCHEMA_VERSION!r})"
                )

            # Verify hashes
            self._verify_hashes(root, manifest.hashes)

            # Restore each component
            self._restore_config(root / "config", force=force)
            self._restore_sqlite(root / "data", force=force)
            self._restore_audit(root / "audit", force=force)
            self._restore_artifacts(root / "artifacts", force=force)
            self._restore_memory(root / "memory", force=force)

    def export_json(self, component: str) -> dict[str, Any]:
        """Export a single component to a JSON-serialisable dict.

        Parameters
        ----------
        component:
            One of: ``config``, ``audit``, ``ledger``, ``tasks``, ``work_items``,
            ``task_graphs``, ``actions``, ``artifacts_db``, ``delegate_sessions``,
            ``auth_sessions``, ``memory_db``.

        Returns
        -------
        dict
            JSON-serialisable representation of the component.
        """
        component = component.lower().strip()

        if component == "config":
            return self._export_config_json()
        if component == "audit":
            return self._export_audit_json()
        if component in _SQLITE_COMPONENTS:
            return self._export_sqlite_json(component)
        raise ValueError(
            f"unknown component {component!r}. "
            f"Valid values: config, audit, {', '.join(sorted(_SQLITE_COMPONENTS))}"
        )

    # ── export helpers ────────────────────────────────────────────────────────

    def _export_config(self, dst_dir: Path, hashes: dict[str, Any]) -> None:
        """Copy config YAML (with secrets redacted) into the staging area."""
        if not self._config_path.exists():
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / self._config_path.name
        raw = self._redact_config(self._config_path.read_text(encoding="utf-8"))
        dst.write_text(raw, encoding="utf-8")
        hashes["config"] = _sha256_file(dst)

    def _export_audit(self, dst_dir: Path, hashes: dict[str, Any]) -> None:
        audit_src = self._data_dir / "audit.jsonl"
        if not audit_src.exists():
            return
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / "audit.jsonl"
        shutil.copy2(audit_src, dst)
        hashes["audit"] = _sha256_file(dst)

    def _export_artifacts(self, dst_dir: Path, hashes: dict[str, Any]) -> None:
        artifacts_src = self._data_dir / "artifacts"
        if not artifacts_src.exists():
            return
        shutil.copytree(artifacts_src, dst_dir, dirs_exist_ok=True)
        hashes["artifacts"] = _sha256_dir(dst_dir)

    def _export_memory(self, dst_dir: Path, hashes: dict[str, Any]) -> None:
        memory_src = self._data_dir / ".maxwell"
        if not memory_src.exists():
            return
        shutil.copytree(memory_src, dst_dir / ".maxwell", dirs_exist_ok=True)
        hashes["memory"] = _sha256_dir(dst_dir)

    # ── restore helpers ───────────────────────────────────────────────────────

    def _restore_config(self, src_dir: Path, *, force: bool) -> None:
        if not src_dir.exists():
            return
        for src_file in src_dir.iterdir():
            dst = self._config_path.parent / src_file.name
            if dst.exists() and not force:
                raise RestoreError(f"config file {dst} already exists; pass --force to overwrite")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst)

    def _restore_sqlite(self, src_dir: Path, *, force: bool) -> None:
        if not src_dir.exists():
            return
        for src_db in src_dir.glob("*.db"):
            dst = self._data_dir / src_db.name
            if dst.exists() and not force:
                raise RestoreError(f"database {dst} already exists; pass --force to overwrite")
            dst.parent.mkdir(parents=True, exist_ok=True)
            _safe_sqlite_backup(src_db, dst)

    def _restore_audit(self, src_dir: Path, *, force: bool) -> None:
        if not src_dir.exists():
            return
        src = src_dir / "audit.jsonl"
        if not src.exists():
            return
        dst = self._data_dir / "audit.jsonl"
        if dst.exists() and not force:
            raise RestoreError(f"audit log {dst} already exists; pass --force to overwrite")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    def _restore_artifacts(self, src_dir: Path, *, force: bool) -> None:
        if not src_dir.exists():
            return
        dst_root = self._data_dir / "artifacts"
        if dst_root.exists() and not force:
            raise RestoreError(
                f"artifacts directory {dst_root} already exists; pass --force to overwrite"
            )
        shutil.copytree(src_dir, dst_root, dirs_exist_ok=True)

    def _restore_memory(self, src_dir: Path, *, force: bool) -> None:
        if not src_dir.exists():
            return
        maxwell_src = src_dir / ".maxwell"
        if not maxwell_src.exists():
            return
        dst_root = self._data_dir / ".maxwell"
        if dst_root.exists() and not force:
            raise RestoreError(
                f"memory directory {dst_root} already exists; pass --force to overwrite"
            )
        shutil.copytree(maxwell_src, dst_root, dirs_exist_ok=True)

    # ── JSON export helpers ───────────────────────────────────────────────────

    def _export_config_json(self) -> dict[str, Any]:
        if not self._config_path.exists():
            return {}
        import yaml  # already a dep via config loader

        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        return self._redact_config_dict(raw)

    def _export_audit_json(self) -> dict[str, Any]:
        from maxwell_daemon.audit import AuditLogger

        path = self._data_dir / "audit.jsonl"
        if not path.exists():
            return {"entries": []}
        logger = AuditLogger(path)
        # Paginate with a generous limit
        entries = logger.entries(limit=100_000, offset=0)
        return {"entries": entries, "count": len(entries)}

    def _export_sqlite_json(self, component: str) -> dict[str, Any]:
        """Dump every table from a SQLite DB as a list of row-dicts."""
        filename = _SQLITE_COMPONENTS[component]
        db_path = self._data_dir / filename
        if not db_path.exists():
            return {"tables": {}, "component": component}
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        tables: dict[str, list[dict[str, Any]]] = {}
        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            for (table_name,) in cursor.fetchall():
                rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()
                tables[table_name] = [dict(r) for r in rows]
        finally:
            conn.close()
        return {"component": component, "tables": tables}

    # ── hash verification ─────────────────────────────────────────────────────

    def _verify_hashes(self, root: Path, hashes: dict[str, Any]) -> None:
        """Check every recorded hash against the unpacked files."""
        # Config
        if "config" in hashes:
            cfg_file = root / "config" / self._config_path.name
            if cfg_file.exists():
                actual = _sha256_file(cfg_file)
                if actual != hashes["config"]:
                    raise RestoreError(
                        f"config hash mismatch: expected {hashes['config']}, got {actual}"
                    )

        # SQLite DBs
        for component, filename in _SQLITE_COMPONENTS.items():
            if component in hashes:
                db_file = root / "data" / filename
                if db_file.exists():
                    actual = _sha256_file(db_file)
                    if actual != hashes[component]:
                        raise RestoreError(
                            f"{component} hash mismatch: expected {hashes[component]}, got {actual}"
                        )

        # Audit log
        if "audit" in hashes:
            audit_file = root / "audit" / "audit.jsonl"
            if audit_file.exists():
                actual = _sha256_file(audit_file)
                if actual != hashes["audit"]:
                    raise RestoreError(
                        f"audit hash mismatch: expected {hashes['audit']}, got {actual}"
                    )

        # Artifact blobs (per-file hash map)
        if "artifacts" in hashes and isinstance(hashes["artifacts"], dict):
            artifacts_dir = root / "artifacts"
            if artifacts_dir.exists():
                actual_hashes = _sha256_dir(artifacts_dir)
                for rel, expected in hashes["artifacts"].items():
                    if rel not in actual_hashes:
                        raise RestoreError(f"artifact blob missing from archive: {rel}")
                    if actual_hashes[rel] != expected:
                        raise RestoreError(
                            f"artifact blob hash mismatch for {rel}: "
                            f"expected {expected}, got {actual_hashes[rel]}"
                        )

        # Memory files (per-file hash map)
        if "memory" in hashes and isinstance(hashes["memory"], dict):
            memory_dir = root / "memory"
            if memory_dir.exists():
                actual_hashes = _sha256_dir(memory_dir)
                for rel, expected in hashes["memory"].items():
                    if rel not in actual_hashes:
                        raise RestoreError(f"memory file missing from archive: {rel}")
                    if actual_hashes[rel] != expected:
                        raise RestoreError(
                            f"memory file hash mismatch for {rel}: "
                            f"expected {expected}, got {actual_hashes[rel]}"
                        )

    # ── secrets helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _redact_config(yaml_text: str) -> str:
        """Strip plaintext api_key values from YAML text."""
        import re

        # Replace literal api_key values that are NOT env-var references
        return re.sub(
            r"(api_key\s*:\s*)(?!\$\{)(.+)",
            r"\1<REDACTED>",
            yaml_text,
        )

    @staticmethod
    def _redact_config_dict(raw: dict[str, Any]) -> dict[str, Any]:
        """Recursively redact sensitive keys from a config dict."""
        sensitive_keys = {"api_key", "password", "token", "secret"}
        result: dict[str, Any] = {}
        for key, value in raw.items():
            if key.lower() in sensitive_keys and isinstance(value, str):
                result[key] = "<REDACTED>"
            elif isinstance(value, dict):
                result[key] = BackupManager._redact_config_dict(value)
            else:
                result[key] = value
        return result

    def _write_secrets_example(self, dst: Path) -> None:
        """Write a placeholder file listing secret refs the user must re-enter."""
        if not self._config_path.exists():
            return
        try:
            import yaml

            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return

        lines = [
            "# Re-enter the following secrets after restoring on a new machine.",
            "# Set each as an environment variable or re-run `maxwell-daemon init`.",
            "",
        ]
        backends = raw.get("backends", {})
        if isinstance(backends, dict):
            for name, cfg in backends.items():
                if isinstance(cfg, dict):
                    ref = cfg.get("api_key_secret_ref")
                    if ref:
                        lines.append(f"# backend '{name}' — secret ref: {ref}")
                    elif cfg.get("api_key"):
                        lines.append(f"# backend '{name}' — set ANTHROPIC_API_KEY (or equivalent)")
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
