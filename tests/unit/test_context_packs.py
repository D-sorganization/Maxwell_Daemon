"""Tests for deterministic local context-pack generation."""

from __future__ import annotations

import json
from pathlib import Path

from maxwell_daemon.context.packs import (
    ContextPackPolicy,
    build_context_pack,
)


def _write(root: Path, relpath: str, body: bytes | str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8", newline="\n")
    return path


class TestContextPackFiles:
    async def test_file_entries_are_deterministically_ordered(self, tmp_path: Path) -> None:
        _write(tmp_path, "zeta.py", "def zeta() -> None: ...\n")
        _write(tmp_path, "alpha.py", "def alpha() -> None: ...\n")
        _write(tmp_path, "pkg/beta.md", "# Beta\n")
        _write(tmp_path, ".git/config", "ignored")

        pack = await build_context_pack(tmp_path, provider_names=())

        assert [entry.path for entry in pack.included_files()] == [
            "alpha.py",
            "pkg/beta.md",
            "zeta.py",
        ]
        assert ".git/config" not in [entry.path for entry in pack.files]
        assert pack.metadata.file_count == 3
        assert pack.metadata.included_file_count == 3

    async def test_size_total_binary_and_type_policy_are_recorded(self, tmp_path: Path) -> None:
        _write(tmp_path, "big.py", "x" * 10)
        _write(tmp_path, "binary.txt", b"abc\x00def")
        _write(tmp_path, "ok.py", "ab")
        _write(tmp_path, "overflow.md", "cd")
        _write(tmp_path, "archive.zip", "zip-like")

        pack = await build_context_pack(
            tmp_path,
            policy=ContextPackPolicy(max_file_bytes=5, max_total_bytes=3),
            provider_names=(),
        )
        by_path = {entry.path: entry for entry in pack.files}

        assert by_path["ok.py"].included is True
        assert by_path["big.py"].skip_reason == "file_size_limit"
        assert by_path["binary.txt"].skip_reason == "file_size_limit"
        assert by_path["overflow.md"].skip_reason == "total_size_limit"
        assert by_path["archive.zip"].skip_reason == "unsupported_file_type"
        assert pack.metadata.total_included_bytes == 2

    async def test_binary_text_candidate_is_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "binary.txt", b"abc\x00def")

        pack = await build_context_pack(tmp_path, provider_names=())

        assert pack.files[0].path == "binary.txt"
        assert pack.files[0].included is False
        assert pack.files[0].skip_reason == "binary_file"


class TestContextPackManifest:
    async def test_stable_manifest_json_is_repeatable(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# Project\n")
        _write(tmp_path, "src/app.py", "def main() -> None: ...\n")

        first = await build_context_pack(tmp_path, provider_names=())
        second = await build_context_pack(tmp_path, provider_names=())

        assert first.stable_manifest_json() == second.stable_manifest_json()
        manifest = json.loads(first.stable_manifest_json())
        assert manifest["metadata"] == {
            "file_count": 2,
            "included_file_count": 2,
            "provider_names": [],
            "repo_name": tmp_path.name,
            "schema_version": "context-pack.v1",
            "total_included_bytes": 34,
        }
        assert [file["path"] for file in manifest["files"]] == ["README.md", "src/app.py"]
        assert "text" not in manifest["files"][0]

    async def test_manifest_can_include_text_when_requested(self, tmp_path: Path) -> None:
        _write(tmp_path, "README.md", "# Project\n")

        pack = await build_context_pack(tmp_path, provider_names=())
        manifest = json.loads(pack.stable_manifest_json(include_text=True))

        assert manifest["files"][0]["text"] == "# Project\n"


class TestContextPackProviders:
    async def test_default_pack_includes_local_provider_sections(self, tmp_path: Path) -> None:
        _write(tmp_path, "CONTRIBUTING.md", "# Contributing\n")
        _write(tmp_path, "pkg/core.py", "class Service:\n    def run(self) -> None: ...\n")

        pack = await build_context_pack(tmp_path)

        assert [section.name for section in pack.sections] == ["docs", "repo_schematic"]
        assert "Contributing" in pack.sections[0].text
        assert "pkg/core.py" in pack.sections[1].text
