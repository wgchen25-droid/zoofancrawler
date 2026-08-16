"""Contract tests for the read-only crawler operations console boundary."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from zoofan.dashboard import create_app
from zoofan.console_api import _project_status_mapping


@dataclass
class RunDto:
    id: str
    status: str = "completed"
    started_at: str = "2026-08-16T08:00:00+00:00"
    finished_at: str | None = "2026-08-16T08:01:00+00:00"
    stored_count: int = 2
    raw_html: str = "MUST NEVER CROSS THE CONSOLE BOUNDARY"


class FakeConsoleService:
    def __init__(self) -> None:
        self.event_filters = None
        self.run_filters = None
        self.run = RunDto("run-1")

    def get_crawler_status(self):
        return {
            "status": "running",
            "active_run": {
                "id": "run-1",
                "status": "running",
                "started_at": "2026-08-16T08:00:00+00:00",
                "metrics": {"discovered": 4, "stored": 2},
            },
            "metrics": {"zoos_completed": 1, "zoos_total": 2, "discovered": 4},
            "last_updated": "2026-08-16T08:01:00+00:00",
        }

    def list_crawl_runs(self, **filters):
        self.run_filters = filters
        return {"runs": [self.run], "total": 1}

    def get_crawl_run(self, run_id):
        return self.run if run_id == self.run.id else None

    def list_run_zoos(self, run_id, **_filters):
        return [{"run_id": run_id, "zoo_id": "zoo-1", "zoo_name": "Alpha Zoo", "status": "completed", "stored": 2}]

    def list_run_source_results(self, run_id, **_filters):
        return [{
            "run_id": run_id,
            "zoo_id": "zoo-1",
            "zoo_name": "Alpha Zoo",
            "source_id": "source-failure",
            "source_name": "Broken feed",
            "status": "error",
            "http_status": 503,
            "error_category": "http_error",
            "error_summary": "HTTP 503 from Broken feed",
            "stop_reason": "http_error",
        }]

    def list_run_articles(self, run_id, **_filters):
        return [{
            "run_id": run_id,
            "id": "article-1",
            "title": "Safe article",
            "canonical_url": "https://example.com/article-1",
            "raw_html": "MUST NEVER APPEAR",
            "language": "en",
        }]

    def list_run_events(self, run_id, **filters):
        self.event_filters = filters
        return {
            "events": [{
                "id": 8,
                "run_id": run_id,
                "level": "WARNING",
                "component": "fetcher",
                "event_type": "http_error",
                "message": "HTTP 503",
                "metadata": {"retry": 3},
            }],
            "next_after_id": 8,
        }

    def list_zoos(self, **_filters):
        return [{"id": "zoo-1", "name": "Alpha Zoo", "website_url": "https://alpha.example"}]

    def get_zoo(self, zoo_id):
        return {"id": zoo_id, "name": "Alpha Zoo"} if zoo_id == "zoo-1" else None

    def list_zoo_crawl_history(self, zoo_id, **_filters):
        return [{"zoo_id": zoo_id, "status": "completed", "stored": 2}]


@pytest.fixture
def service():
    return FakeConsoleService()


@pytest.fixture
def client(service):
    return create_app(":memory:", console_service=service).test_client()


def test_status_and_run_collections_use_service_dtos(client, service):
    status = client.get("/api/crawler/status")
    assert status.status_code == 200
    assert status.get_json()["status"]["active_run"]["id"] == "run-1"

    runs = client.get("/api/crawl-runs?status=running&limit=12&offset=2")
    assert runs.status_code == 200
    assert runs.get_json()["items"][0]["id"] == "run-1"
    assert service.run_filters == {"status": "running", "limit": 12, "offset": 2}


@pytest.mark.parametrize("raw, classified", [
    ("finished", "success"), ("completed", "success"), ("failed", "failed"),
    ("running", "running"), ("stale", "stale"), ("mystery", "unknown"),
    ("budget_hit", "budget_hit"),
])
def test_api_status_projection_preserves_raw_and_disables_stale_polling(raw, classified):
    value = _project_status_mapping({"status": raw, "active": raw == "running",
                                     "active_run": {"id": "r"},
                                     "poll_hint": {"enabled": True}})
    assert value["classified_status"] == classified
    assert value["status_key"] == classified
    assert value["display_status"] == classified
    assert value["raw_status"] == raw
    if classified != "running":
        assert value["active"] is False
        assert value["active_run"] is None
        assert value["poll_hint"]["enabled"] is False


def test_run_subresources_apply_filters_and_incremental_event_cursor(client, service):
    zoos = client.get("/api/crawl-runs/run-1/zoos")
    articles = client.get("/api/crawl-runs/run-1/articles?limit=3&search=safe")
    events = client.get(
        "/api/crawl-runs/run-1/events?after_id=7&level=WARNING&zoo=zoo-1"
        "&source=source-1&component=fetcher&event_type=retry_exhausted&search=403&limit=4"
    )

    assert zoos.status_code == 200
    assert zoos.get_json()["zoos"][0]["zoo_id"] == "zoo-1"
    assert articles.status_code == 200
    assert articles.get_json()["articles"][0]["title"] == "Safe article"
    assert events.status_code == 200
    assert events.get_json()["events"][0]["id"] == 8
    assert events.get_json()["next_after_id"] == 8
    assert service.event_filters == {
        "after_id": 7,
        "level": "WARNING",
        "zoo": "zoo-1",
        "source": "source-1",
        "component": "fetcher",
        "event_type": "retry_exhausted",
        "search": "403",
        "limit": 4,
    }


def test_failure_source_evidence_survives_api_and_run_detail_render_contract(client):
    source_response = client.get("/api/crawl-runs/run-1/source-results")
    event_response = client.get("/api/crawl-runs/run-1/events")
    source = source_response.get_json()["sources"][0]
    event = event_response.get_json()["events"][0]
    page = client.get("/console/runs/run-1").get_data(as_text=True)

    assert source_response.status_code == 200
    assert source["http_status"] == 503
    assert source["error_category"] == "http_error"
    assert event_response.status_code == 200
    assert event["event_type"] == "http_error"
    assert "data-sources-url" in page
    assert "number(http)" in page
    assert "event.event_type || event.type" in page
    assert "metadataText" in page
    assert "raw_html" not in page


def test_zoo_endpoints_and_empty_collections_are_read_only(client):
    zoos = client.get("/api/zoos")
    zoo = client.get("/api/zoos/zoo-1")
    history = client.get("/api/zoos/zoo-1/crawl-history")
    assert zoos.status_code == 200 and zoos.get_json()["items"]
    assert zoo.status_code == 200 and zoo.get_json()["zoo"]["id"] == "zoo-1"
    assert history.status_code == 200 and history.get_json()["history"]

    empty = create_app(":memory:").test_client()
    assert empty.get("/api/crawl-runs").get_json()["items"] == []
    assert empty.get("/api/zoos").get_json()["items"] == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/crawl-runs/missing",
        "/api/crawl-runs/missing/zoos",
        "/api/crawl-runs/missing/articles",
        "/api/crawl-runs/missing/events",
        "/api/zoos/missing",
        "/api/zoos/missing/crawl-history",
    ],
)
def test_missing_parent_records_are_404(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "path",
    [
        "/api/crawl-runs?limit=0",
        "/api/crawl-runs?limit=not-a-number",
        "/api/crawl-runs/run-1/events?after_id=-1",
        "/api/crawl-runs/run-1/events?after_id=abc",
    ],
)
def test_invalid_pagination_is_400(client, path):
    response = client.get(path)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"


def test_transport_projection_drops_raw_html_and_unsafe_urls(client):
    response = client.get("/api/crawl-runs/run-1/articles")
    payload = response.get_json()
    assert "raw_html" not in payload["articles"][0]
    assert "MUST NEVER APPEAR" not in response.get_data(as_text=True)


def test_console_api_does_not_import_or_query_sqlite_directly():
    from pathlib import Path

    source = Path("zoofan/console_api.py").read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "SELECT " not in source
