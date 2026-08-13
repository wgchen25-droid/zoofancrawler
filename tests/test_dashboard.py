"""Route-level checks for the read-only crawler dashboard."""

from __future__ import annotations

import json
import sqlite3
from html.parser import HTMLParser

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
    db.execute("UPDATE zoos SET country_code='DE', language='de'")
    db.execute("UPDATE sources SET language='de'")
    db.execute(
        """UPDATE articles SET published_at='2026-08-10T08:00:00+00:00',
           updated_at_source='2026-08-10T09:00:00+00:00', language='de',
           http_status=200, crawl_status='stored', html_hash='html-alpha',
           last_fetched_at='2026-08-11T10:00:00+00:00',
           created_at='2026-08-01T06:00:00+00:00', updated_at='2026-08-11T11:00:00+00:00'
           WHERE id='a1'"""
    )
    db.execute(
        """UPDATE articles SET published_at=NULL, updated_at_source=NULL, language='de',
           http_status=200, crawl_status='already_known', html_hash='html-beta',
           last_fetched_at='2026-08-12T10:00:00+00:00',
           created_at='2026-08-02T06:00:00+00:00', updated_at='2026-08-12T11:00:00+00:00'
           WHERE id='a2'"""
    )
    db.execute("UPDATE article_discoveries SET discovered_at='2026-08-12T07:00:00+00:00', last_discovered_at='2026-08-13T08:00:00+00:00' WHERE id='d1'")
    db.execute("UPDATE article_discoveries SET discovered_at='2026-08-13T07:00:00+00:00' WHERE id='d2'")
    db.execute(
        """UPDATE crawl_runs SET started_at='2026-08-12T11:59:58+00:00',
           finished_at='2026-08-12T12:00:00+00:00', duration_ms=2000 WHERE id='run-1'"""
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
    db.execute("UPDATE crawl_run_stats SET already_known_count=3, duplicate_candidate_count=4 WHERE id='st1'")
    db.execute("UPDATE crawl_run_stats SET already_known_count=0, duplicate_candidate_count=2 WHERE id='st2'")
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


class _DefinitionFields(HTMLParser):
    def __init__(self):
        super().__init__()
        self.term = None
        self.capture = None
        self.buffer = []
        self.fields = {}

    def handle_starttag(self, tag, _attrs):
        if tag in {"dt", "dd"}:
            self.capture = tag
            self.buffer = []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag != self.capture:
            return
        value = "".join(self.buffer).strip()
        if tag == "dt":
            self.term = value
        elif self.term:
            self.fields[self.term] = value
        self.capture = None


def _definition_fields(response):
    parser = _DefinitionFields()
    parser.feed(response.get_data(as_text=True))
    return parser.fields


def test_article_list_keeps_source_and_storage_timestamps_distinct(client):
    body = client.get("/articles").get_data(as_text=True)
    assert "Published at source" in body
    assert "Updated at source" in body
    assert "First discovered" in body
    assert "2026-08-10 08:00" in body
    assert "2026-08-10 09:00" in body
    assert "2026-08-12 07:00" in body
    # The NULL publication row is sorted by discovery but its publication cell
    # stays unknown; it never borrows its storage timestamp.
    beta_row = body.split('data-acceptance-id="a2"', 1)[1].split("</tr>", 1)[0]
    assert "2026-08-02 06:00" not in beta_row
    assert 'data-acceptance-field="published-at-source">—</td>' in beta_row


def test_date_only_source_dates_do_not_invent_midnight(tmp_path):
    path = tmp_path / "date-only.sqlite"
    _seed_database(path)
    db = sqlite3.connect(path)
    db.execute(
        "UPDATE articles SET published_at='2026-08-10', updated_at_source='2026-08-11' WHERE id='a1'"
    )
    db.commit()
    db.close()
    client = create_app(path).test_client()

    article_row = client.get("/articles").get_data(as_text=True).split(
        'data-acceptance-id="a1"', 1
    )[1].split("</tr>", 1)[0]
    detail = _definition_fields(client.get("/articles/a1"))
    assert 'data-acceptance-field="published-at-source">2026-08-10</td>' in article_row
    assert 'data-acceptance-field="updated-at-source">2026-08-11</td>' in article_row
    assert detail["Published at source"] == "2026-08-10"
    assert detail["Updated at source"] == "2026-08-11"
    assert "2026-08-10 00:00" not in article_row
    assert "2026-08-11 00:00" not in article_row


def test_detail_renders_complete_database_evidence(client):
    fields = _definition_fields(client.get("/articles/a1"))
    assert fields == {
        "Article ID": "a1",
        "Title": "Alpha article",
        "Zoo": "Alpha Zoo",
        "Source": "rss feed",
        "Original URL": "https://alpha.example/a1",
        "Canonical URL": "https://alpha.example/a1",
        "Language": "de",
        "Article HTTP status": "200",
        "Crawl status": "stored",
        "HTML hash": "html-alpha",
        "Published at source": "2026-08-10 08:00",
        "Updated at source": "2026-08-10 09:00",
        "First discovered": "2026-08-12 07:00",
        "Last discovered": "2026-08-13 08:00",
        "Fetched at": "2026-08-11 10:00",
        "Stored at": "2026-08-01 06:00",
        "Storage updated at": "2026-08-11 11:00",
    }
    assert _definition_fields(client.get("/articles/a2"))["Published at source"] == "—"


def test_latest_source_publication_excludes_null_date_and_metrics_are_consistent(client):
    home = client.get("/").get_data(as_text=True)
    runs = client.get("/runs").get_data(as_text=True)
    assert "Latest by source publication" in home
    assert "Alpha article" in home
    beta_card = home.split('data-acceptance-id="z2"', 1)[1].split("</article>", 1)[0]
    assert "Beta article" not in beta_card
    assert "No articles with a source publication date" in beta_card
    for label, value in (
        ("Stored", "1"),
        ("Already known / skipped", "3"),
        ("Duplicate candidates", "4"),
        ("Errors", "0"),
    ):
        field = {
            "Stored": "stored", "Already known / skipped": "already-known",
            "Duplicate candidates": "duplicate-candidates", "Errors": "errors",
        }[label]
        assert f'<dt>{label}</dt><dd data-acceptance-field="{field}">{value}</dd>' in home
        assert f"<th>{label}</th>" in runs
    assert '<dt>Duration</dt><dd data-acceptance-field="run-duration">2000 ms</dd>' in runs


def test_source_context_and_favicon(client):
    sources = client.get("/sources").get_data(as_text=True)
    assert "Zoo country" in sources and ">DE<" in sources
    assert "Zoo language" in sources and ">de<" in sources
    assert "<th>Kind</th>" in sources and "<th>Language</th>" in sources
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.mimetype == "image/svg+xml"
    assert 'rel="icon" href="/favicon.ico"' in client.get("/").get_data(as_text=True)


def test_stable_acceptance_hooks_cover_mandatory_values(client):
    article_list = client.get("/articles").get_data(as_text=True)
    detail = client.get("/articles/a1").get_data(as_text=True)
    sources = client.get("/sources").get_data(as_text=True)
    runs = client.get("/runs").get_data(as_text=True)
    for field in (
        "title", "zoo", "sources", "published-at-source", "updated-at-source",
        "first-discovered", "language", "crawl-status", "article-http-status", "canonical-url",
    ):
        assert f'data-acceptance-field="{field}"' in article_list
    for field in (
        "article-id", "original-url", "html-hash", "last-discovered", "fetched-at",
        "stored-at", "storage-updated-at",
    ):
        assert f'data-acceptance-field="{field}"' in detail
    for field in ("source-id", "kind", "status", "last-check", "http-status", "last-error"):
        assert f'data-acceptance-field="{field}"' in sources
    for field in (
        "run-start", "run-end", "run-duration", "run-status", "discovered", "fetched",
        "stored", "already-known", "duplicate-candidates", "errors",
    ):
        assert f'data-acceptance-field="{field}"' in runs


def test_invalid_article_urls_are_plain_text_not_links(tmp_path):
    path = tmp_path / "invalid.sqlite"
    _seed_database(path)
    db = sqlite3.connect(path)
    db.execute("UPDATE articles SET canonical_url='javascript:alert(1)', source_url='file:///tmp/source' WHERE id='a1'")
    db.commit()
    db.close()
    app = create_app(path)
    list_body = app.test_client().get("/articles").get_data(as_text=True)
    detail_body = app.test_client().get("/articles/a1").get_data(as_text=True)
    assert 'href="javascript:alert(1)"' not in list_body + detail_body
    assert 'href="file:///tmp/source"' not in detail_body


def test_legacy_missing_optional_columns_and_tables_do_not_500(tmp_path):
    path = tmp_path / "legacy.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE zoos (id TEXT PRIMARY KEY, slug TEXT, name TEXT)")
    db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT, kind TEXT, name TEXT)")
    db.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, canonical_url TEXT, title TEXT, created_at TEXT)")
    db.execute("CREATE TABLE crawl_runs (id TEXT PRIMARY KEY, started_at TEXT, status TEXT)")
    db.execute("INSERT INTO zoos VALUES ('z1', 'legacy', 'Legacy Zoo')")
    db.execute("INSERT INTO sources VALUES ('s1', 'z1', 'https://legacy.example/feed', 'rss', 'Legacy feed')")
    db.execute("INSERT INTO articles VALUES ('a1', 'https://legacy.example/a1', 'Legacy article', '2020-01-01')")
    db.execute("INSERT INTO crawl_runs VALUES ('r1', '2020-01-01', 'completed')")
    db.commit()
    db.close()
    client = create_app(path).test_client()
    for route in ("/", "/articles", "/articles/a1", "/sources", "/runs"):
        assert client.get(route).status_code == 200


def test_legacy_duplicate_metric_is_not_relabelled_as_duplicate_candidates(tmp_path):
    path = tmp_path / "legacy-duplicates.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE zoos (id TEXT PRIMARY KEY, slug TEXT, name TEXT)")
    db.execute("CREATE TABLE sources (id TEXT PRIMARY KEY, zoo_id TEXT, kind TEXT, name TEXT)")
    db.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, title TEXT, published_at TEXT)")
    db.execute("CREATE TABLE crawl_runs (id TEXT PRIMARY KEY, batch_id TEXT, started_at TEXT, finished_at TEXT, status TEXT)")
    db.execute("""CREATE TABLE crawl_run_stats (
        id TEXT PRIMARY KEY, crawl_run_id TEXT, zoo_id TEXT, source_id TEXT,
        status TEXT, discovered_count INTEGER, fetched_count INTEGER,
        stored_count INTEGER, already_known_count INTEGER, duplicate_count INTEGER,
        error_count INTEGER)""")
    db.execute("INSERT INTO zoos VALUES ('z1', 'legacy', 'Legacy Zoo')")
    db.execute("INSERT INTO sources VALUES ('s1', 'z1', 'rss', 'Legacy feed')")
    db.execute("INSERT INTO crawl_runs VALUES ('r1', 'b1', '2020-01-01', '2020-01-02', 'completed')")
    db.execute("INSERT INTO crawl_run_stats VALUES ('st1', 'r1', 'z1', 's1', 'completed', 9, 8, 7, 6, 55, 0)")
    db.commit()
    db.close()

    client = create_app(path).test_client()
    home = client.get("/").get_data(as_text=True)
    runs = client.get("/runs").get_data(as_text=True)
    assert 'data-acceptance-field="duplicate-candidates">—<' in home
    assert 'data-acceptance-field="duplicate-candidates">—<' in runs
    assert ">55<" not in home + runs


def test_missing_database_is_not_created_and_dashboard_stays_empty(tmp_path):
    path = tmp_path / "does-not-exist" / "crawler.sqlite"
    client = create_app(path).test_client()
    for route in ("/", "/articles", "/sources", "/runs"):
        response = client.get(route)
        assert response.status_code == 200
        assert b"Internal Server Error" not in response.data
    assert client.get("/articles/a1").status_code == 404
    assert not path.exists()
    assert not path.parent.exists()


def test_dashboard_connection_rejects_writes(tmp_path):
    path = tmp_path / "crawler.sqlite"
    _seed_database(path)
    connection = dashboard._connect(str(path))
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        connection.execute("UPDATE zoos SET name='changed'")
    connection.close()


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
