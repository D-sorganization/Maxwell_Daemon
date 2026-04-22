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
        task_graph_store_path=tmp_path / "task_graphs.db",
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

    def test_has_control_plane_sections(self, client: TestClient) -> None:
        html = client.get("/ui/").text

        for expected in (
            'data-view="work-items"',
            'data-view="approvals"',
            'data-view="artifacts"',
            'data-view="graphs"',
            'data-view="checks"',
            "view-work-items",
            "view-approvals",
            "view-artifacts",
            "view-graphs",
            "view-checks",
        ):
            assert expected in html

    def test_references_control_plane_endpoints_in_js(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text

        for expected in (
            "/api/v1/work-items",
            "/api/v1/actions?status=proposed",
            "/api/v1/task-graphs",
            "/api/v1/artifacts/",
            "/api/v1/check-runs",
            "/api/v1/control-plane/gauntlet",
        ):
            assert expected in js

    def test_control_plane_rendering_escapes_untrusted_text(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text

        for expected in (
            "escapeHtml(item.title)",
            "escapeHtml(action.summary)",
            "escapeHtml(artifact.name)",
            "pre.textContent",
            "JSON.stringify(record, null, 2)",
            "controlPlaneError",
        ):
            assert expected in js

    def test_gate_actions_use_server_contract(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text

        assert "submitGateAction" in js
        assert "fetch(action.path" in js
        assert "data-gate-action" in js
        assert "prompt(`Who is waiving" in js
        assert "confirm(`Retry" in js

    def test_gauntlet_view_ships_empty_and_error_states(self, client: TestClient) -> None:
        html = client.get("/ui/").text
        js = client.get("/ui/app.js").text
        css = client.get("/ui/style.css").text

        assert "view-gauntlet" in html
        assert "No work items have reached the control plane yet." in js
        assert "No delegate sessions recorded yet." in js
        assert "Gate gauntlet unavailable" in js
        assert "gauntlet-error" in css

    def test_deferred_test_output_keeps_selected_task_context(self, client: TestClient) -> None:
        js = client.get("/ui/app.js").text

        assert "const selectedAtSchedule = p.task_id;" in js
        assert "state.selected === selectedAtSchedule" in js
        assert 'state.testOutput.get(selectedAtSchedule) || "(no streamed output)"' in js
        assert "state.testOutput.get(state.selected)" not in js

    def test_unfiltered_task_fetch_failure_resets_all_tasks_snapshot(
        self, client: TestClient
    ) -> None:
        js = client.get("/ui/app.js").text

        assert "if (allR.ok)" in js
        assert "state.allTasks = new Map(state.tasks);" in js

    def test_service_worker_limits_shell_cache_fallback_to_navigations(
        self, client: TestClient
    ) -> None:
        sw = client.get("/ui/sw.js").text

        assert "request.mode === 'navigate'" in sw
        assert "request.destination === 'document'" in sw
        assert "url.pathname === '/ui/'" in sw
        assert "url.pathname === '/ui/index.html'" in sw
        assert "url.pathname.startsWith('/ui/')" not in sw


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
