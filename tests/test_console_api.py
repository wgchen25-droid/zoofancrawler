"""Contract tests for the read-only crawler operations console boundary."""

from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3

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


def test_callable_service_factory_is_request_scoped_reused_and_closed():
    created = []
    calls = []

    class RequestService:
        def __init__(self):
            self.closed = False
            created.append(self)

        def get_crawl_run(self, run_id):
            calls.append(("run", id(self), run_id))
            return {"id": run_id, "status": "completed"}

        def list_run_zoos(self, run_id, **_filters):
            calls.append(("zoos", id(self), run_id))
            return []

        def close(self):
            self.closed = True

    app = create_app(":memory:", console_service=lambda: RequestService())
    first = app.test_client().get("/api/crawl-runs/r1/zoos")
    second = app.test_client().get("/api/crawl-runs/r2/zoos")

    assert first.status_code == second.status_code == 200
    assert len(created) == 2
    assert all(instance.closed for instance in created)
    assert calls[0][1] == calls[1][1]
    assert calls[2][1] == calls[3][1]
    assert calls[0][1] != calls[2][1]


def test_direct_service_instance_has_external_lifetime():
    service = FakeConsoleService()
    service.closed = False
    service.close = lambda: setattr(service, "closed", True)
    response = create_app(":memory:", console_service=service).test_client().get(
        "/api/crawl-runs"
    )
    assert response.status_code == 200
    assert service.closed is False


def test_default_service_is_request_scoped_and_closed(monkeypatch):
    import zoofan.console_api as console_api

    created = []

    class DefaultService:
        def __init__(self):
            self.closed = False
            created.append(self)

        def list_crawl_runs(self, **_filters):
            return []

        def close(self):
            self.closed = True

    monkeypatch.setattr(console_api, "_default_service", lambda _path: DefaultService())
    client = create_app(":memory:").test_client()
    assert client.get("/api/crawl-runs").status_code == 200
    assert client.get("/api/crawl-runs").status_code == 200
    assert len(created) == 2
    assert created[0] is not created[1]
    assert all(instance.closed for instance in created)


def test_database_failure_is_stable_non_leaking_503():
    from zoofan.console_repository import ConsoleRepository
    from zoofan.console_service import ConsoleService

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE crawl_runs (id TEXT PRIMARY KEY, status TEXT)")
    repository = ConsoleRepository(connection)
    assert repository.list_runs() == []  # populate valid schema caches first
    service = ConsoleService(repository)
    connection.close()
    response = create_app(":memory:", console_service=service).test_client().get(
        "/api/crawl-runs"
    )
    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Crawler console data is temporarily unavailable",
        }
    }
    assert "closed" not in response.get_data(as_text=True).casefold()


def test_configured_factory_declared_unavailable_is_safe_503(caplog):
    from zoofan.console_api import ConsoleServiceUnavailable

    def broken_factory():
        raise ConsoleServiceUnavailable("secret constructor detail")

    response = create_app(":memory:", console_service=broken_factory).test_client().get(
        "/api/crawl-runs"
    )
    assert response.status_code == 503
    assert response.get_json()["error"] == {
        "code": "service_unavailable",
        "message": "Crawler console service is not configured",
    }
    assert "secret" not in response.get_data(as_text=True)
    assert "secret constructor detail" in caplog.text


def test_default_service_construction_failure_is_safe_503(monkeypatch):
    from zoofan.console_api import ConsoleServiceUnavailable
    import zoofan.console_service as console_service

    def broken_service(*_args, **_kwargs):
        raise ConsoleServiceUnavailable("secret default constructor detail")

    monkeypatch.setattr(console_service, "ConsoleService", broken_service)
    response = create_app(":memory:").test_client().get("/api/crawl-runs")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "service_unavailable"
    assert "secret" not in response.get_data(as_text=True)


@pytest.mark.parametrize("error", [RuntimeError("bug"), TypeError("bug"), ValueError("bug")])
def test_factory_programming_errors_are_not_converted_to_operational_503(error):
    def broken_factory():
        raise error

    app = create_app(":memory:", console_service=broken_factory)
    app.config["TESTING"] = True
    with pytest.raises(type(error), match="bug"):
        app.test_client().get("/api/crawl-runs")


def test_internal_service_import_failure_is_logged_and_safe_503(monkeypatch, caplog):
    import zoofan.console_api as console_api

    def broken_import(_name):
        raise ModuleNotFoundError(
            "No module named 'private_dependency'", name="private_dependency"
        )

    monkeypatch.setattr(console_api.importlib, "import_module", broken_import)
    response = create_app(":memory:").test_client().get("/api/crawl-runs")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "service_unavailable"
    assert "private_dependency" not in response.get_data(as_text=True)
    assert "private_dependency" in caplog.text


def test_exactly_missing_optional_service_module_keeps_compatible_empty_state(monkeypatch):
    import zoofan.console_api as console_api

    def missing_service(_name):
        raise ModuleNotFoundError(
            "No module named 'zoofan.console_service'", name="zoofan.console_service"
        )

    monkeypatch.setattr(console_api.importlib, "import_module", missing_service)
    response = create_app(":memory:").test_client().get("/api/crawl-runs")
    assert response.status_code == 200
    assert response.get_json()["items"] == []


def test_default_service_database_open_failure_is_safe_503(tmp_path, monkeypatch):
    import zoofan.console_repository as console_repository

    app = create_app(tmp_path / "not-created.db")

    def fail_connect(*_args, **_kwargs):
        raise sqlite3.OperationalError("secret database path")

    monkeypatch.setattr(console_repository.sqlite3, "connect", fail_connect)
    response = app.test_client().get("/api/crawl-runs")
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "service_unavailable"
    assert "secret" not in response.get_data(as_text=True)


def test_declared_cursor_routes_round_trip_next_page():
    class PagingService(FakeConsoleService):
        def _page(self, name, values, limit, offset):
            items = values[offset : offset + limit]
            next_offset = offset + len(items) if offset + len(items) < len(values) else None
            return {
                name: items,
                "pagination": {
                    "limit": limit,
                    "offset": offset,
                    "total": len(values),
                    "has_more": next_offset is not None,
                    "next_offset": next_offset,
                },
            }

        def list_crawl_runs(self, limit, offset, **_filters):
            values = [{"id": f"run-{number}"} for number in range(5)]
            return self._page("runs", values, limit, offset)

        def list_run_articles(self, run_id, limit, offset, **_filters):
            values = [
                {"id": f"article-{number}", "run_id": run_id}
                for number in range(5)
            ]
            return self._page("articles", values, limit, offset)

    client = create_app(":memory:", console_service=PagingService()).test_client()
    for path, item_key in (
        ("/api/crawl-runs", "runs"),
        ("/api/crawl-runs/run-1/articles", "articles"),
    ):
        first = client.get(f"{path}?limit=2").get_json()
        assert [item["id"] for item in first[item_key]] == [
            "run-0" if item_key == "runs" else "article-0",
            "run-1" if item_key == "runs" else "article-1",
        ]
        assert first["has_more"] is True
        second = client.get(f"{path}?limit=2&cursor={first['next_cursor']}").get_json()
        assert [item["id"] for item in second[item_key]] == [
            "run-2" if item_key == "runs" else "article-2",
            "run-3" if item_key == "runs" else "article-3",
        ]
        assert second["next_cursor"] == 4


@pytest.mark.parametrize("path", ["/api/crawl-runs", "/api/crawl-runs/run-1/articles"])
@pytest.mark.parametrize("query", ["cursor=-1", "cursor=bad", "cursor=1.5", "cursor=1&offset=0"])
def test_declared_cursor_routes_reject_invalid_or_ambiguous_tokens(client, path, query):
    response = client.get(f"{path}?{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    "path",
    [
        "/api/crawl-runs",
        "/api/crawl-runs/run-1/zoos",
        "/api/crawl-runs/run-1/sources",
        "/api/crawl-runs/run-1/articles",
        "/api/crawl-runs/run-1/events",
        "/api/zoos",
        "/api/zoos/zoo-1/crawl-history",
    ],
)
def test_list_api_envelope_is_stable_for_non_empty_results(client, path):
    payload = client.get(path).get_json()
    assert {"next_cursor", "has_more", "last_updated"} <= payload.keys()
    if path.endswith("/events"):
        assert "next_after_id" in payload


def test_empty_list_envelopes_and_event_cursor_are_stable():
    class EmptyCollections(FakeConsoleService):
        def list_crawl_runs(self, **_filters):
            return []

        def list_run_zoos(self, run_id, **_filters):
            return []

        def list_run_source_results(self, run_id, **_filters):
            return []

        def list_run_articles(self, run_id, **_filters):
            return []

        def list_run_events(self, run_id, **_filters):
            return []

        def list_zoos(self, **_filters):
            return []

        def list_zoo_crawl_history(self, zoo_id, **_filters):
            return []

    client = create_app(":memory:", console_service=EmptyCollections()).test_client()
    paths = [
        "/api/crawl-runs",
        "/api/crawl-runs/run-1/zoos",
        "/api/crawl-runs/run-1/sources",
        "/api/crawl-runs/run-1/articles",
        "/api/crawl-runs/run-1/events",
        "/api/zoos",
        "/api/zoos/zoo-1/crawl-history",
    ]
    for path in paths:
        payload = client.get(path).get_json()
        assert payload["items"] == []
        assert (payload["next_cursor"], payload["has_more"], payload["last_updated"]) == (
            None, False, None
        )
        if path.endswith("/events"):
            assert payload["next_after_id"] is None


def test_event_filter_vocabulary_is_case_normalized(client, service):
    response = client.get(
        "/api/crawl-runs/run-1/events?level=warning&component=FETCHER&type=HTTP_ERROR"
    )
    assert response.status_code == 200
    assert service.event_filters["level"] == "WARNING"
    assert service.event_filters["component"] == "fetcher"
    assert service.event_filters["event_type"] == "http_error"


@pytest.mark.parametrize(
    "query",
    ["level=debug", "component=network", "event_type=made_up"],
)
def test_unknown_event_filter_vocabulary_is_400(client, query):
    response = client.get(f"/api/crawl-runs/run-1/events?{query}")
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"


def test_threaded_product_requests_use_distinct_request_connections(tmp_path):
    from zoofan.console_service import ConsoleService
    from zoofan.models import CrawlRun, CrawlZooResult, Zoo
    from zoofan.storage import SQLiteStorage

    database = tmp_path / "console-threaded.db"
    with SQLiteStorage(database) as storage:
        storage.upsert_zoo(Zoo(id="z1", slug="alpha", name="Alpha Zoo"))
        storage.start_crawl_run(
            CrawlRun(id="run-1", status="completed", started_at="2026-08-16T00:00:00+00:00")
        )
        storage.upsert_zoo_run_result(
            CrawlZooResult(
                id="zr1", crawl_run_id="run-1", zoo_id="z1", status="completed"
            )
        )

    connections = []
    services = []
    overlap = Barrier(8)

    class TrackingService(ConsoleService):
        def __init__(self, database):
            super().__init__(database=database)
            connections.append(self.repository.connection)
            self.was_closed = False
            services.append(self)
            # Eight request-owned connections must all be live before any
            # request may continue, making overlap deterministic.
            overlap.wait(timeout=5)

        def close(self):
            super().close()
            self.was_closed = True

    app = create_app(database, console_service=TrackingService)
    paths = [
        "/api/crawl-runs",
        "/api/crawl-runs/run-1",
        "/api/crawl-runs/run-1/zoos",
    ] * 8

    def fetch(path):
        with app.test_client() as threaded_client:
            response = threaded_client.get(path)
            return path, response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(fetch, paths))

    assert len(responses) == 24
    for path, status, payload in responses:
        assert status == 200
        if path == "/api/crawl-runs":
            assert payload["total"] == 1
            assert [run["id"] for run in payload["runs"]] == ["run-1"]
        elif path.endswith("/zoos"):
            assert payload["count"] == 1
            assert payload["zoos"][0]["zoo_id"] == "z1"
        else:
            assert payload["run"]["id"] == "run-1"
    assert len(connections) == len(paths)
    assert len({id(connection) for connection in connections}) == len(paths)
    assert all(service.was_closed for service in services)
