from __future__ import annotations

import json
import sqlite3
from html.parser import HTMLParser

import pytest
from flask import Flask

from zoofan.endgoal import (
    _atomic_write_json,
    _article_rows,
    _close_playwright_handles,
    _dedup_report,
    _discovery_bounds,
    _expected_duplicate_candidates,
    _human_banner,
    _rendered_exact,
    _prepare_dashboard_screenshot,
    _validate_enabled_sources,
    _validate_run_source_evidence,
    _validate_articles,
    _REQUIRED_DATABASE_MATCHES,
    emit_final_endgoal_result,
    evaluate_dashboard_observation,
    finalize_endgoal_report,
    navigation_false_positive,
)
from zoofan.models import Source, Zoo


def _complete_dashboard_observation():
    return {
        "routes": {
            "/": {
                "status": 200,
                "body": "Overview with database values",
                "labels": [
                    "Sources", "Articles", "Last crawl", "Stored", "Already known / skipped",
                    "Duplicate candidates", "Errors",
                ],
            },
            "/articles": {
                "status": 200,
                "body": "Article row from the database",
                "labels": [
                    "Title", "Zoo", "Published at source", "Updated at source", "Discovered at", "Source", "Language",
                    "Crawl status", "Article HTTP status", "Canonical URL",
                ],
            },
            "/sources": {
                "status": 200,
                "body": "Source row from the database",
                "labels": [
                    "Zoo", "Source", "Kind", "Language", "Status", "Last check",
                    "HTTP status", "Last error",
                ],
            },
            "/runs": {
                "status": 200,
                "body": "Run row from the database",
                "labels": [
                    "Start", "End", "Duration", "Status", "Discovered", "Fetched", "Stored",
                    "Already known / skipped", "Duplicate candidates", "Errors",
                ],
            },
            "/articles/article-1": {
                "status": 200,
                "body": "Complete article evidence from the database",
                "labels": [
                    "Article ID", "Title", "Zoo", "Source", "Original URL", "Canonical URL",
                    "Language", "Article HTTP status", "Crawl status", "HTML hash",
                    "Published at source", "Updated at source", "First discovered",
                    "Last discovered", "Fetched at", "Stored at", "Storage updated at",
                ],
            },
        },
        "database_matches": {name: True for name in _REQUIRED_DATABASE_MATCHES},
        "null_publication": {"rendered": "Unknown", "storage_created": "2026-08-13T12:00:00Z"},
        "browser_assertion_errors": [],
        "page_errors": [],
        "console_errors": [],
        "screenshot_written_this_run": True,
    }


def _finalize_dashboard(observation):
    dashboard = evaluate_dashboard_observation(observation)
    report = {
        "unit_tests": {"status": "PASS"},
        "live": {"status": "PASS"},
        "dedup": {"status": "PASS"},
        "failure_isolation": {"status": "PASS"},
        "dashboard": dashboard,
        "errors": [],
    }
    return dashboard, finalize_endgoal_report(report)


def test_navigation_gate_rejects_navigation_and_honors_explicit_allow():
    source = {"url": "https://zoo.example/news", "config": {}}
    assert navigation_false_positive("https://zoo.example/", source)
    assert navigation_false_positive("https://zoo.example/tickets", source)
    assert navigation_false_positive("https://zoo.example/events", source)
    allowed = {"url": source["url"], "config": {"allow_regex": r"/events/"}}
    assert navigation_false_positive("https://zoo.example/events/story", allowed) is None


def test_atomic_report_write_and_dedup_report(tmp_path):
    destination = tmp_path / "nested" / "report.json"
    _atomic_write_json(destination, {"status": "PASS", "value": 1})
    assert json.loads(destination.read_text()) == {"status": "PASS", "value": 1}

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE articles (canonical_url TEXT, normalized_url TEXT)")
    connection.executemany(
        "INSERT INTO articles VALUES (?, ?)",
        [("https://zoo.example/a", "https://zoo.example/a"), ("https://zoo.example/b", "https://zoo.example/b")],
    )
    report = _dedup_report(
        connection,
        before_total=0,
        after_run1_total=2,
        after_run2_total=2,
        run1_canonicals={"https://zoo.example/a", "https://zoo.example/b"},
        run2_canonicals={"https://zoo.example/a", "https://zoo.example/b"},
    )
    assert report["status"] == "PASS"
    assert report["run1_new"] == 2
    assert report["run2_new"] == 0
    connection.close()


def test_playwright_handles_close_in_order_before_driver_exit():
    closed = []

    class Handle:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    _close_playwright_handles(Handle("page"), Handle("context"), Handle("browser"))
    assert closed == ["page", "context", "browser"]


def test_human_banner_is_exact_acceptance_string():
    assert _human_banner("PASS") == "ZOOFAN CRAWLER PROTOTYPE: PASS"
    assert _human_banner("FAIL") == "ZOOFAN CRAWLER PROTOTYPE: FAIL"


@pytest.mark.parametrize(
    ("mutate", "named_failure"),
    [
        (
            lambda item: item["routes"]["/articles"]["labels"].remove("Discovered at"),
            "route /articles: missing mandatory field 'Discovered at'",
        ),
        (
            lambda item: item["routes"]["/articles/article-1"]["labels"].remove("Canonical URL"),
            "article detail /articles/article-1: missing mandatory field 'Canonical URL'",
        ),
        (
            lambda item: item.update(
                null_publication={
                    "rendered": "2026-08-13T12:00:00Z",
                    "storage_created": "2026-08-13T12:00:00Z",
                }
            ),
            "NULL publication: storage created time was rendered as source publication",
        ),
        (
            lambda item: item["routes"]["/runs"]["labels"].remove("Duration"),
            "route /runs: missing mandatory field 'Duration'",
        ),
        (
            lambda item: item["routes"]["/sources"]["labels"].remove("Language"),
            "route /sources: missing mandatory field 'Language'",
        ),
        (
            lambda item: item["browser_assertion_errors"].append("deliberate browser assertion failure"),
            "browser assertion: deliberate browser assertion failure",
        ),
    ],
    ids=[
        "route-200-missing-field",
        "detail-missing-canonical",
        "null-date-is-storage-created",
        "runs-missing-duration",
        "sources-missing-language",
        "browser-assertion-failure",
    ],
)
def test_strict_dashboard_negative_controls_return_nonzero_without_pass(mutate, named_failure):
    observation = _complete_dashboard_observation()
    mutate(observation)

    dashboard, (exit_code, lines) = _finalize_dashboard(observation)

    assert dashboard["status"] == "FAIL"
    assert named_failure in dashboard["failures"]
    assert exit_code != 0
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines
    assert any(named_failure in line for line in lines)


def test_strict_dashboard_positive_control_prints_exact_pass_once():
    dashboard, (exit_code, lines) = _finalize_dashboard(_complete_dashboard_observation())

    assert dashboard == {"status": "PASS", "failures": []}
    assert exit_code == 0
    assert lines.count("ZOOFAN CRAWLER PROTOTYPE: PASS") == 1
    assert lines[-1] == "ZOOFAN CRAWLER PROTOTYPE: PASS"


def test_missing_database_matches_is_a_named_failure():
    observation = _complete_dashboard_observation()
    observation.pop("database_matches")

    dashboard, (exit_code, lines) = _finalize_dashboard(observation)

    assert dashboard["status"] == "FAIL"
    assert "database evidence: missing required match 'detail.canonical_url'" in dashboard["failures"]
    assert "database evidence: missing required match 'run.duration'" in dashboard["failures"]
    assert exit_code == 1
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines


def test_generic_http_status_does_not_satisfy_article_http_status():
    observation = _complete_dashboard_observation()
    labels = observation["routes"]["/articles"] ["labels"]
    labels[labels.index("Article HTTP status")] = "HTTP status"

    result = evaluate_dashboard_observation(observation)

    assert "route /articles: missing mandatory field 'Article HTTP status'" in result["failures"]


class _RenderedLabels(HTMLParser):
    def __init__(self):
        super().__init__()
        self._capture = False
        self.labels = []
        self.body = []

    def handle_starttag(self, tag, _attrs):
        self._capture = tag in {"th", "dt", "label"}

    def handle_endtag(self, tag):
        if tag in {"th", "dt", "label"}:
            self._capture = False

    def handle_data(self, data):
        value = data.strip()
        if value:
            self.body.append(value)
            if self._capture:
                self.labels.append(value)


def test_rendered_flask_dashboard_missing_detail_canonical_fails_strict_acceptance():
    complete = _complete_dashboard_observation()
    app = Flask(__name__)

    def render_labels(path):
        labels = list(complete["routes"][path]["labels"])
        if path == "/articles/article-1":
            labels.remove("Canonical URL")
        return "<html><body>" + "".join(f"<dl><dt>{label}</dt><dd>database value</dd></dl>" for label in labels) + "</body></html>"

    app.add_url_rule("/", "home", lambda: render_labels("/"))
    app.add_url_rule("/articles", "articles", lambda: render_labels("/articles"))
    app.add_url_rule("/sources", "sources", lambda: render_labels("/sources"))
    app.add_url_rule("/runs", "runs", lambda: render_labels("/runs"))
    app.add_url_rule("/articles/article-1", "detail", lambda: render_labels("/articles/article-1"))

    routes = {}
    with app.test_client() as client:
        for path in complete["routes"]:
            response = client.get(path)
            parser = _RenderedLabels()
            parser.feed(response.get_data(as_text=True))
            routes[path] = {"status": response.status_code, "body": " ".join(parser.body), "labels": parser.labels}
    observation = {**complete, "routes": routes}

    dashboard, (exit_code, lines) = _finalize_dashboard(observation)

    failure = "article detail /articles/article-1: missing mandatory field 'Canonical URL'"
    assert dashboard["status"] == "FAIL"
    assert failure in dashboard["failures"]
    assert exit_code == 1
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines


def test_final_stdout_integration_returns_nonzero_prints_failures_and_no_pass(capsys):
    observation = _complete_dashboard_observation()
    observation["null_publication"] = {
        "rendered": "2026-08-13T12:00:00Z",
        "storage_created": "2026-08-13T12:00:00Z",
    }
    dashboard = evaluate_dashboard_observation(observation)
    report = {
        "unit_tests": {"status": "PASS"}, "live": {"status": "PASS"},
        "dedup": {"status": "PASS"}, "failure_isolation": {"status": "PASS"},
        "dashboard": dashboard, "errors": [],
    }

    exit_code = emit_final_endgoal_result(report)
    stdout = capsys.readouterr().out

    assert exit_code == 1
    assert "ENDGOAL FAILED ITEM: NULL publication: storage created time was rendered as source publication" in stdout
    assert "ZOOFAN CRAWLER PROTOTYPE: FAIL" in stdout
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in stdout


@pytest.mark.parametrize(
    ("match_name", "expected_failure"),
    [
        (
            "articles.canonical_url",
            "database evidence: rendered value does not match 'articles.canonical_url'",
        ),
        (
            "run.fetched",
            "database evidence: rendered value does not match 'run.fetched'",
        ),
    ],
    ids=["article-list-wrong-canonical-value", "run-wrong-fetched-value"],
)
def test_exact_list_and_run_database_value_negative_controls(match_name, expected_failure):
    observation = _complete_dashboard_observation()
    observation["database_matches"][match_name] = False

    dashboard, (exit_code, lines) = _finalize_dashboard(observation)

    assert dashboard["status"] == "FAIL"
    assert expected_failure in dashboard["failures"]
    assert exit_code == 1
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines


def test_current_schema_last_discovered_uses_persisted_last_value(tmp_path):
    database = tmp_path / "current.db"
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE articles (id TEXT PRIMARY KEY, published_at TEXT)")
    connection.execute(
        "CREATE TABLE article_discoveries (article_id TEXT, discovered_at TEXT, last_discovered_at TEXT)"
    )
    connection.execute("INSERT INTO articles VALUES ('article-null-date', NULL)")
    connection.executemany(
        "INSERT INTO article_discoveries VALUES (?, ?, ?)",
        [
            ("article-null-date", "2026-08-01T09:00:00+00:00", "2026-08-12T18:30:00+00:00"),
            ("article-null-date", "2026-08-02T10:00:00+00:00", "2026-08-10T11:00:00+00:00"),
        ],
    )
    rows = [dict(row) for row in connection.execute(
        "SELECT * FROM article_discoveries WHERE article_id='article-null-date'"
    )]
    connection.close()

    first, last = _discovery_bounds(rows, has_last_discovered_at=True)

    assert first == "2026-08-01T09:00:00+00:00"
    assert last == "2026-08-12T18:30:00+00:00"
    assert _rendered_exact("2026-08-12 18:30", last, timestamp=True)
    observation = _complete_dashboard_observation()
    observation["database_matches"]["detail.last_discovered"] = _rendered_exact(
        "2026-08-12 18:30", last, timestamp=True
    )
    assert evaluate_dashboard_observation(observation)["status"] == "PASS"


def test_legacy_discovery_and_duplicate_metrics_allow_only_unknown_display():
    discoveries = [
        {"discovered_at": "2026-08-01T09:00:00+00:00"},
        {"discovered_at": "2026-08-02T10:00:00+00:00"},
    ]

    first, last = _discovery_bounds(discoveries, has_last_discovered_at=False)
    duplicate_count = _expected_duplicate_candidates(
        [{"stored_count": 2}], has_persisted_column=False
    )

    assert first == "2026-08-01T09:00:00+00:00"
    assert last == "2026-08-02T10:00:00+00:00"
    assert duplicate_count is None
    assert _rendered_exact("Unknown", duplicate_count)
    assert _rendered_exact("—", duplicate_count)
    assert not _rendered_exact("0", duplicate_count)


def test_current_schema_duplicate_metrics_require_exact_persisted_number():
    duplicate_count = _expected_duplicate_candidates(
        [{"duplicate_candidate_count": 0}, {"duplicate_candidate_count": 2}],
        has_persisted_column=True,
    )

    assert duplicate_count == 2
    assert _rendered_exact("2", duplicate_count)
    assert not _rendered_exact("Unknown", duplicate_count)


def _article_validation_fixture(html_hash):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos (id TEXT, slug TEXT, name TEXT);
        CREATE TABLE sources (
            id TEXT, zoo_id TEXT, url TEXT, kind TEXT, status TEXT, success INTEGER,
            last_error TEXT
        );
        CREATE TABLE articles (
            id TEXT, canonical_url TEXT, normalized_url TEXT, title TEXT,
            published_at TEXT, content_hash TEXT, html_hash TEXT, raw_html TEXT,
            metadata_json TEXT, created_at TEXT
        );
        CREATE TABLE article_discoveries (
            id TEXT, article_id TEXT, source_id TEXT, discovered_url TEXT,
            discovered_at TEXT
        );
        """
    )
    content_hash = "1" * 64
    connection.execute("INSERT INTO zoos VALUES ('zoo-1', 'zoo-one', 'Zoo One')")
    connection.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("source-1", "zoo-1", "https://zoo.example/news", "rss", "completed", 1, None),
    )
    connection.execute(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "article-1", "https://zoo.example/news/article-1",
            "https://zoo.example/news/article-1", "Article One", None,
            content_hash, html_hash, "<html>exact bytes</html>", "{}",
            "2026-08-13T10:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO article_discoveries VALUES (?, ?, ?, ?, ?)",
        (
            "discovery-1", "article-1", "source-1",
            "https://zoo.example/news/article-1", "2026-08-13T10:00:00+00:00",
        ),
    )
    zoo = Zoo(
        id="zoo-1", slug="zoo-one", name="Zoo One",
        website_url="https://zoo.example", country_code="DE", language="de",
    )
    source = Source(
        id="source-1", zoo_id="zoo-1", url="https://zoo.example/news",
        kind="rss", language="de", config={"official_hosts": ["zoo.example"]},
    )
    config = type("Config", (), {"zoos": [zoo], "sources": [source]})()
    return connection, config, content_hash


@pytest.mark.parametrize("html_hash", [None, "f" * 63, "A" * 64])
def test_live_article_validation_rejects_missing_or_invalid_actual_html_hash(html_hash):
    connection, config, _content_hash = _article_validation_fixture(html_hash)
    try:
        validation = _validate_articles(connection, config, minimum_per_zoo=1)
    finally:
        connection.close()
    report = {
        "unit_tests": {"status": "PASS"},
        "live": {"status": validation["status"]},
        "dedup": {"status": "PASS"},
        "failure_isolation": {"status": "PASS"},
        "dashboard": {"status": "PASS", "failures": []},
        "errors": [],
    }

    exit_code, lines = finalize_endgoal_report(report)

    assert validation["status"] == "FAIL"
    assert "missing or invalid html_hash SHA-256" in validation["invalid_articles"][0]["reasons"]
    assert exit_code == 1
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines


def test_live_article_validation_keeps_content_and_html_hash_evidence_distinct():
    html_hash = "2" * 64
    connection, config, content_hash = _article_validation_fixture(html_hash)
    try:
        row = _article_rows(connection)[0]
        validation = _validate_articles(connection, config, minimum_per_zoo=1)
    finally:
        connection.close()

    assert row["content_hash"] == content_hash
    assert row["html_hash"] == html_hash
    assert row["content_hash"] != row["html_hash"]
    assert validation["status"] == "PASS"


def test_enabled_source_failure_fails_even_when_other_source_and_articles_succeed():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE sources (
            id TEXT, status TEXT, success INTEGER, last_checked TEXT,
            last_success TEXT, last_error TEXT, last_http_status INTEGER
        )"""
    )
    connection.executemany(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("source-good", "completed", 1, "2026-08-13", "2026-08-13", None, 200),
            ("source-failed", "error", 0, "2026-08-13", None, "timeout", None),
        ],
    )
    sources = [
        Source(id="source-good", zoo_id="zoo-1", url="https://zoo.example/feed", language="de"),
        Source(id="source-failed", zoo_id="zoo-1", url="https://zoo.example/archive", language="de"),
    ]
    config = type("Config", (), {"sources": sources})()

    validation = _validate_enabled_sources(config, connection)
    connection.close()
    report = {
        "unit_tests": {"status": "PASS"},
        "live": {"status": validation["status"], "failures": validation["failures"]},
        "dedup": {"status": "PASS"},
        "failure_isolation": {"status": "PASS"},
        "dashboard": {"status": "PASS", "failures": []},
        "errors": [],
    }

    exit_code, lines = finalize_endgoal_report(report)

    assert validation["sources"]["source-good"]["status"] == "PASS"
    assert validation["sources"]["source-failed"]["status"] == "FAIL"
    assert any("enabled source source-failed:" in item for item in validation["failures"])
    assert exit_code == 1
    assert any("source-failed" in line for line in lines)
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines


def test_stale_preexisting_screenshot_is_removed_and_cannot_satisfy_observation(tmp_path):
    screenshot = tmp_path / "dashboard-acceptance.png"
    screenshot.write_bytes(b"stale screenshot")

    _prepare_dashboard_screenshot(screenshot)
    observation = _complete_dashboard_observation()
    observation["screenshot_written_this_run"] = False
    result = evaluate_dashboard_observation(observation)

    assert not screenshot.exists()
    assert result["status"] == "FAIL"
    assert "dashboard screenshot: not written during current browser run" in result["failures"]


def _run_source_evidence_fixture():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE sources (
            id TEXT, status TEXT, success INTEGER, last_checked TEXT,
            last_success TEXT, last_error TEXT, last_http_status INTEGER
        );
        CREATE TABLE crawl_run_stats (
            id TEXT, crawl_run_id TEXT, source_id TEXT, status TEXT,
            discovered_count INTEGER, error_count INTEGER, error TEXT,
            errors_json TEXT
        );
        """
    )
    source = Source(
        id="source-zero", zoo_id="zoo-1", url="https://zoo.example/health",
        kind="registry_only", language="de",
    )
    config = type("Config", (), {"sources": [source]})()
    connection.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "source-zero", "completed", 1,
            "2026-08-13T10:00:05+00:00", "2026-08-13T10:00:05+00:00",
            None, 204,
        ),
    )
    connection.execute(
        "INSERT INTO crawl_run_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("stat-1", "run-1", "source-zero", "completed", 0, 0, None, "[]"),
    )
    return connection, config


def _audit_run_source_fixture(connection, config):
    return _validate_run_source_evidence(
        config,
        connection,
        run_id="run-1",
        started_at="2026-08-13T10:00:00+00:00",
        finished_at="2026-08-13T10:00:10+00:00",
    )


def test_completed_zero_candidate_source_has_valid_current_run_evidence():
    connection, config = _run_source_evidence_fixture()
    try:
        result = _audit_run_source_fixture(connection, config)
    finally:
        connection.close()

    assert result["status"] == "PASS"
    assert result["stat_source_ids"] == ["source-zero"]
    assert result["failures"] == []


@pytest.mark.parametrize(
    ("mutate", "failure_fragment"),
    [
        (
            lambda db: db.execute(
                "UPDATE sources SET last_checked='2026-08-12T10:00:05+00:00', last_success='2026-08-12T10:00:05+00:00'"
            ),
            "source source-zero last_checked is outside current run window",
        ),
        (
            lambda db: db.execute("DELETE FROM crawl_run_stats WHERE source_id='source-zero'"),
            "missing stat for enabled source source-zero",
        ),
        (
            lambda db: db.execute(
                "INSERT INTO crawl_run_stats VALUES ('stat-extra','run-1','source-extra','completed',0,0,NULL,'[]')"
            ),
            "unexpected stat source source-extra",
        ),
        (
            lambda db: db.execute(
                "UPDATE crawl_run_stats SET status='partial', error_count=1, error='article failed'"
            ),
            "source source-zero stat status is 'partial', expected 'completed'",
        ),
    ],
    ids=["stale-healthy-source", "missing-source-stat", "extra-source-stat", "partial-source-stat"],
)
def test_current_run_source_evidence_adversarial_failures_are_named(mutate, failure_fragment):
    connection, config = _run_source_evidence_fixture()
    mutate(connection)
    try:
        validation = _audit_run_source_fixture(connection, config)
    finally:
        connection.close()
    report = {
        "unit_tests": {"status": "PASS"},
        "live": {"status": validation["status"], "failures": validation["failures"]},
        "dedup": {"status": "PASS"},
        "failure_isolation": {"status": "PASS"},
        "dashboard": {"status": "PASS", "failures": []},
        "errors": [],
    }

    exit_code, lines = finalize_endgoal_report(report)

    assert validation["status"] == "FAIL"
    assert any(failure_fragment in item for item in validation["failures"])
    assert exit_code == 1
    assert any(failure_fragment in line for line in lines)
    assert "ZOOFAN CRAWLER PROTOTYPE: PASS" not in lines
