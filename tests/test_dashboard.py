"""Route-level checks for the read-only crawler dashboard."""

from __future__ import annotations

import json

import pytest

import zoofan.dashboard as dashboard
from zoofan.dashboard import create_app
from zoofan.storage import SQLiteStorage


NOW = "2026-08-12T12:00:00+00:00"


def _seed_database(path) -> None:
    storage = SQLiteStorage(path)
    db = storage.connection
    for zoo_id, slug, name in (
        ("z1", "alpha", "Alpha Zoo"),
        ("z2", "beta", "Beta Zoo"),
        ("z3", "gamma", "Gamma Zoo"),
    ):
        db.execute(
            """INSERT INTO zoos
               (id, slug, name, website_url, enabled, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, '{}', ?, ?)""",
            (zoo_id, slug, name, f"https://{slug}.example", NOW, NOW),
        )
    for source_id, zoo_id, kind, status, error in (
        ("s1", "z1", "rss", "ok", None),
        ("s2", "z2", "sitemap", "error", "feed timed out"),
        ("s3", "z3", "rss", "pending", None),
    ):
        db.execute(
            """INSERT INTO sources
               (id, zoo_id, url, kind, name, config_json, enabled, status,
                last_checked, last_success, last_error, last_http_status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_id,
                zoo_id,
                f"https://{zoo_id}.example/feed",
                kind,
                f"{kind} feed",
                json.dumps({"max_pages": 2}),
                status,
                NOW,
                NOW if status == "ok" else None,
                error,
                200,
                NOW,
                NOW,
            ),
        )
    for article_id, title, url, content, raw_html in (
        ("a1", "Alpha article", "https://alpha.example/a1", "<p>Alpha body</p>", "RAW HTML MUST NOT APPEAR"),
        ("a2", "Beta article", "https://beta.example/a2", "Beta body", None),
    ):
        db.execute(
            """INSERT INTO articles
               (id, canonical_url, normalized_url, source_url, title, published_at,
                content, raw_html, metadata_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)""",
            (article_id, url, url, url, title, NOW, content, raw_html, NOW, NOW),
        )
    for discovery_id, article_id, source_id in (("d1", "a1", "s1"), ("d2", "a2", "s2")):
        db.execute(
            """INSERT INTO article_discoveries
               (id, article_id, source_id, discovered_url, discovered_at, metadata_json)
               VALUES (?, ?, ?, ?, ?, '{}')""",
            (discovery_id, article_id, source_id, f"https://discover.example/{article_id}", NOW),
        )
    db.execute(
        """INSERT INTO crawl_runs
           (id, batch_id, started_at, finished_at, status, error, metadata_json)
           VALUES ('run-1', 'batch-1', ?, ?, 'completed', NULL, '{}')""",
        (NOW, NOW),
    )
    for stat_id, zoo_id, source_id, status, discovered, stored, errors, error, errors_json in (
        ("st1", "z1", "s1", "completed", 2, 1, 0, None, "[]"),
        ("st2", "z2", "s2", "completed", 1, 0, 1, "feed timed out", '["feed timed out"]'),
    ):
        db.execute(
            """INSERT INTO crawl_run_stats
               (id, crawl_run_id, zoo_id, source_id, status, discovered_count,
                fetched_count, stored_count, error_count, started_at, finished_at,
                error, errors_json, metadata_json)
               VALUES (?, 'run-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
            (stat_id, zoo_id, source_id, status, discovered, discovered, stored, errors, NOW, NOW, error, errors_json),
        )
    db.commit()
    storage.close()


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "crawler.sqlite"
    _seed_database(db_path)
    app = create_app(db_path)
    app.testing = True
    return app.test_client()


def test_all_dashboard_routes_render(client):
    for path in ("/", "/articles", "/articles/a1", "/sources", "/runs"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert b"Internal Server Error" not in response.data


def test_overview_statuses_metrics_and_safe_official_link(client):
    body = client.get("/").data
    assert b"HEALTHY" in body
    assert b"WARNING" in body
    assert b"NEVER CRAWLED" in body
    assert b"Alpha article" in body
    assert b'target="_blank"' in body
    assert b'rel="noopener noreferrer"' in body


def test_article_filters_and_detail_do_not_render_raw_html(client):
    alpha = client.get("/articles?zoo=alpha")
    assert alpha.status_code == 200
    assert b"Alpha article" in alpha.data
    assert b"Beta article" not in alpha.data

    sitemap = client.get("/articles?source_type=sitemap")
    assert b"Beta article" in sitemap.data
    assert b"Alpha article" not in sitemap.data

    detail = client.get("/articles/a1")
    assert b"Alpha article" in detail.data
    assert b"RAW HTML MUST NOT APPEAR" not in detail.data
    assert b"&lt;p&gt;Alpha body&lt;/p&gt;" in detail.data


def test_sources_and_runs_include_operational_details(client):
    sources = client.get("/sources").data
    assert b"Source ID" in sources
    assert b">s1<" in sources
    assert b"HTTP status" in sources
    assert b">200<" in sources
    assert b"max_pages" in sources
    assert b"feed timed out" in sources

    runs = client.get("/runs").data
    assert b"batch-1" in runs
    assert b"Alpha Zoo" in runs
    assert b"feed timed out" in runs


def test_missing_article_is_not_a_server_error(client):
    response = client.get("/articles/does-not-exist")
    assert response.status_code == 404


def test_running_status_is_warning_and_malformed_urls_are_ignored():
    assert dashboard._zoo_status({"status": "running"}, [], []) == "WARNING"
    assert dashboard._safe_external_url("https://[not-an-ipv6-address") is None
    assert dashboard._safe_external_url("javascript:alert(1)") is None


def test_control_navigation_requires_a_safe_external_url(tmp_path):
    db_path = tmp_path / "crawler.sqlite"
    _seed_database(db_path)

    valid_app = create_app(db_path, control_url="https://control.example/status")
    valid_response = valid_app.test_client().get("/")
    assert "爬虫控制" in valid_response.get_data(as_text=True)
    assert b'href="https://control.example/status"' in valid_response.data
    assert b'target="_blank"' in valid_response.data
    assert b'rel="noopener noreferrer"' in valid_response.data

    invalid_app = create_app(db_path, control_url="javascript:alert(1)")
    invalid_response = invalid_app.test_client().get("/")
    assert "爬虫控制" not in invalid_response.get_data(as_text=True)


def test_main_forwards_optional_control_url(monkeypatch):
    calls = []

    class DummyApp:
        def run(self, **kwargs):
            calls.append(kwargs)

    create_calls = []
    monkeypatch.setattr(
        dashboard,
        "create_app",
        lambda path, control_url=None: create_calls.append((path, control_url)) or DummyApp(),
    )
    dashboard.main("crawler.sqlite", "0.0.0.0", 8123, "https://control.example")
    assert create_calls == [("crawler.sqlite", "https://control.example")]
    assert calls == [{"host": "0.0.0.0", "port": 8123}]


def test_main_starts_server_with_defaults(monkeypatch):
    calls = []

    class DummyApp:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(dashboard, "create_app", lambda path, control_url=None: DummyApp())
    dashboard.main("crawler.sqlite", "0.0.0.0", 8123)
    assert calls == [{"host": "0.0.0.0", "port": 8123}]
