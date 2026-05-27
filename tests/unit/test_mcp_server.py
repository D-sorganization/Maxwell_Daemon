"""Unit tests for the in-process MCP server (HTTP server, routing, and tools)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from maxwell_daemon.mcp.server import (
    build_mcp_registry,
    mock_search_chembl,
    mock_search_clinical_trials,
    mock_search_pubmed,
    search_chembl,
    search_clinical_trials,
    search_pubmed,
    start_mcp_http_server,
)


@pytest.mark.unit
def test_build_mcp_registry(tmp_path: Path) -> None:
    """Verify that build_mcp_registry registers both default and custom science tools."""
    registry = build_mcp_registry(tmp_path)
    names = registry.names()
    assert "search_clinical_trials" in names
    assert "search_pubmed" in names
    assert "search_chembl" in names
    assert "read_file" in names


@pytest.mark.unit
def test_mock_search_functions() -> None:
    """Verify the behavior of mock search fallbacks."""
    # Test clinical trials mock
    res_ct = mock_search_clinical_trials("Cancer", 2)
    assert "Pembrolizumab" in res_ct
    assert "NCT04512345" in res_ct

    # Test PubMed mock
    res_pm = mock_search_pubmed("Diabetes", 1)
    assert "metformin" in res_pm.lower()
    assert "PMID: 35890123" in res_pm

    # Test ChEMBL mock
    res_cb = mock_search_chembl("Aspirin", 1)
    assert "ASPIRIN" in res_cb
    assert "CHEMBL25" in res_cb


@pytest.mark.unit
@pytest.mark.asyncio
async def test_science_tools_api_fallbacks() -> None:
    """Verify that science tools handle exceptions gracefully and fall back to mock data."""
    # Force API failure by mocking httpx.AsyncClient.get to throw an error
    with patch("httpx.AsyncClient.get", side_effect=httpx.HTTPError("Connection failed")):
        res_ct = await search_clinical_trials("Cancer", 1)
        assert "NCT ID:" in res_ct
        assert "Clinical Trials API request failed" in res_ct or "Pembrolizumab" in res_ct

        res_pm = await search_pubmed("Diabetes", 1)
        assert "PMID:" in res_pm
        assert "PubMed API request failed" in res_pm or "metformin" in res_pm.lower()

        res_cb = await search_chembl("Aspirin", 1)
        assert "ChEMBL ID:" in res_cb
        assert "ChEMBL API request failed" in res_cb or "ASPIRIN" in res_cb


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mcp_http_server_lifecycle_and_auth(tmp_path: Path) -> None:
    """Verify start_mcp_http_server starts cleanly, enforces bearer token auth, and shuts down."""
    # Create a minimal yaml configuration
    config_file = tmp_path / "maxwell-daemon.yaml"
    config_file.write_text(
        json.dumps(
            {
                "backends": {
                    "claude": {"type": "claude", "model": "claude-sonnet-4-6"},
                },
                "memory": {
                    "workspace_path": str(tmp_path),
                },
            }
        ),
        encoding="utf-8",
    )

    async with start_mcp_http_server(config_file) as (temp_json_path, server_url):
        # Verify temporary config file exists
        assert temp_json_path.exists()

        # Read the temporary mcp-config.json
        with open(temp_json_path, encoding="utf-8") as f:
            mcp_config = json.load(f)

        assert "maxwell-daemon" in mcp_config["mcpServers"]
        server_entry = mcp_config["mcpServers"]["maxwell-daemon"]
        assert server_entry["type"] == "http"
        assert server_entry["url"] == server_url

        headers = server_entry["headers"]
        assert "Authorization" in headers
        auth_header = headers["Authorization"]
        assert auth_header.startswith("Bearer ")
        token = auth_header.split(" ")[1]

        # Test auth checks with httpx client
        async with httpx.AsyncClient() as client:
            # 1. No Authorization header -> 401
            resp = await client.get(server_url)
            assert resp.status_code == 401
            assert resp.text == "Unauthorized"

            # 2. Bad Authorization header -> 401
            resp = await client.get(server_url, headers={"Authorization": "Bearer badtoken"})
            assert resp.status_code == 401
            assert resp.text == "Unauthorized"

            # 3. Good Authorization header -> 200 or 404 (not 401)
            resp = await client.get(server_url, headers={"Authorization": f"Bearer {token}"})
            assert resp.status_code != 401

    # Verify temp config cleanup after exit
    assert not temp_json_path.exists()
