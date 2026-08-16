"""Smoke checks for the server-rendered Operations Console shell."""

from __future__ import annotations

from zoofan.dashboard import create_app


def test_console_pages_render_loading_shells_and_safe_navigation():
    client = create_app(":memory:").test_client()
    for path in (
        "/console",
        "/console/overview",
        "/console/runs",
        "/console/runs/run-1",
        "/console/zoos/zoo-1",
        "/console/articles",
        "/console/events",
    ):
        response = client.get(path)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Loading console data" in body
        assert "data-console-page" in body
        assert "raw_html" not in body


def test_console_ui_declares_polling_and_incremental_event_contract():
    client = create_app(":memory:").test_client()
    body = client.get("/console/runs/run-1").get_data(as_text=True)
    assert "setInterval" in body
    assert "2500" in body
    assert "refreshRun(root)" in body
    assert "after_id" in body
    assert "No events recorded for this run" in body
    assert "noopener noreferrer" in body


def test_console_overview_formats_totals_and_surfaces_current_activity_dto():
    client = create_app(":memory:").test_client()
    body = client.get("/console").get_data(as_text=True)

    assert "completedTotal" in body
    assert "zoos_completed" in body
    assert "zoos_total" in body
    assert "sources_completed" in body
    assert "sources_total" in body
    assert "current_activity" in body
    assert "console-current-badge" in body
    assert "JSON.stringify(item.progress)" in body
    assert "textContent" in body


def test_legacy_dashboard_routes_remain_available():
    client = create_app(":memory:").test_client()
    for path in ("/", "/articles", "/sources", "/runs"):
        response = client.get(path)
        assert response.status_code == 200


def test_console_classified_status_contract():
    body = create_app(":memory:").test_client().get("/console").get_data(as_text=True)
    for key, label in (("success", "Success"), ("warning", "Warning"), ("failed", "Failed"), ("budget_hit", "Budget Hit"), ("running", "Running"), ("stale", "Stale"), ("unknown", "Unknown")):
        assert f'{key}: "{label}"' in body
    for field in ("classified_status", "display_status", "status_key", "status", "status_label"):
        assert field in body
    assert "statusTag(run)" in body
    assert "statusTag(current)" in body
    assert "statusTag(item)" in body
    assert "statusTag(run.status" not in body
    assert "source.status || \"unknown\"" in body
    assert "source.terminal_status" in body
    assert "source.terminal === true" in body
    assert "source.finished_at" in body
    assert "source.status_observable === false" in body
    assert "source.is_active !== false" in body
    assert "source.raw_status" not in body
    assert "source.run_status" not in body
