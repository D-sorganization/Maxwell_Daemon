"""Web UI served at /ui/ — static HTML/JS/CSS bundled with the daemon."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maxwell_daemon.api import create_app
from maxwell_daemon.config import MaxwellDaemonConfig
from maxwell_daemon.daemon import Daemon


@pytest.fixture
def client(minimal_config: MaxwellDaemonConfig, tmp_path: Path) -> Iterator[TestClient]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    d = Daemon(
        minimal_config,
        ledger_path=tmp_path / "ledger.db",
        task_store_path=tmp_path / "tasks.db",
        work_item_store_path=tmp_path / "work_items.db",
        artifact_store_path=tmp_path / "artifacts.db",
        artifact_blob_root=tmp_path / "artifacts",
        action_store_path=tmp_path / "actions.db",
    )
    try:
        with TestClient(create_app(d)) as c:
            yield c
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestUIRoutes:
    def test_index_returns_html(self, client: TestClient) -> None:
        r = client.get("/ui/")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/html")
        assert "Maxwell-Daemon" in r.text
        assert "<title>" in r.text

    def test_js_served(self, client: TestClient) -> None:
        r = client.get("/ui/app.js")
        assert r.status_code == 200
        ct = r.headers["content-type"]
        assert "javascript" in ct or "text" in ct
        assert "fetch" in r.text or "WebSocket" in r.text

    def test_css_served(self, client: TestClient) -> None:
        r = client.get("/ui/style.css")
        assert r.status_code == 200
        ct = r.headers["content-type"]
        assert "css" in ct or "text" in ct

    def test_bare_ui_redirects_to_trailing_slash(self, client: TestClient) -> None:
        # `/ui` vs `/ui/` — we accept either for discoverability.
        r = client.get("/ui", follow_redirects=False)
        # Either a direct 200 or a 3xx redirect to /ui/
        assert r.status_code in (200, 301, 307, 308)

    def test_nonexistent_asset_404(self, client: TestClient) -> None:
        r = client.get("/ui/does-not-exist.png")
        assert r.status_code == 404


class TestHTMLContent:
    def test_has_dashboard_sections(self, client: TestClient) -> None:
        html = client.get("/ui/").text
        for expected in ("Tasks", "Cost", "Backends"):
            assert expected in html

    def test_has_vs_code_like_shell_regions(self, client: TestClient) -> None:
        html = client.get("/ui/").text
        for expected in (
            "activity-bar",
            "sidebar",
            "editor-area",
            "terminal-panel",
            "status-bar",
            "command-palette",
        ):
            assert expected in html

    def test_references_api_endpoints_in_js(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text
        assert "/api/v1/tasks" in js
        assert "/api/v1/events" in js or "WebSocket" in js
        assert "openCommandPalette" in js
        assert "terminal-log" in js

    def test_deferred_test_output_keeps_selected_task_context(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text

        assert "const selectedAtSchedule = p.task_id;" in js
        assert "state.selected === selectedAtSchedule" in js
        assert 'state.testOutput.get(selectedAtSchedule) || "(no streamed output)"' in js
        assert "state.testOutput.get(state.selected)" not in js


class TestNewTaskDialog:
    def test_dialog_present(self, client: TestClient) -> None:
        html = client.get("/ui/").text
        assert "new-task-dialog" in html
        assert "new-task-btn" in html

    def test_js_parses_issue_references(self, client: TestClient) -> None:
        """Smoke-test: the JS ships the dispatch helper + POST endpoint."""
        js = client.get("/ui/app.js").text
        assert "parseIssueRef" in js
        assert "/api/v1/issues/dispatch" in js
