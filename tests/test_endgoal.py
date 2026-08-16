from __future__ import annotations

import json
import sqlite3
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from flask import Flask

from zoofan.endgoal import (
    _atomic_write_json,
    _article_rows,
    _classification_info,
    _classify_crawl_results,
    _close_playwright_handles,
    _dedup_report,
    _discovery_bounds,
    _expected_duplicate_candidates,
    _empty_zoo_report,
    _human_banner,
    _rendered_exact,
    _raw_categories_from_evidence,
    _prepare_dashboard_screenshot,
    _build_static_report,
    _prepare_static_report_screenshot,
    _validate_enabled_sources,
    _validate_live_registry,
    _validate_run_source_evidence,
    _validate_run_zoo_evidence,
    _validate_articles,
    _run_live_acceptance,
    _REQUIRED_DATABASE_MATCHES,
    emit_final_endgoal_result,
    evaluate_dashboard_observation,
    evaluate_static_report_observation,
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


def _dedup_fixture(entries):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE articles ("
        "canonical_url TEXT, normalized_url TEXT, title TEXT, content_hash TEXT, "
        "content_identity_key TEXT)"
    )
    connection.executemany(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?)", entries
    )
    return connection


def test_dedup_allows_content_hash_collision_when_titles_differ():
    entries = [
        ("https://zoo.example/a", "https://zoo.example/a", "First story", "a" * 64, "key-a"),
        ("https://zoo.example/b", "https://zoo.example/b", "Second story", "a" * 64, "key-b"),
    ]
    connection = _dedup_fixture(entries)
    try:
        report = _dedup_report(
            connection,
            before_total=0,
            after_run1_total=2,
            after_run2_total=2,
            run1_canonicals={entry[0] for entry in entries},
            run2_canonicals={entry[0] for entry in entries},
        )
    finally:
        connection.close()

    assert report["status"] == "PASS"
    assert report["content_hash_duplicates"] == [{"value": "a" * 64, "count": 2}]
    assert report["content_identity_duplicates"] == []
    assert report["content_hash_collision_warning"] is True
    assert report["warnings"]


def test_dedup_rejects_duplicate_composite_content_identity():
    entries = [
        ("https://zoo.example/a", "https://zoo.example/a", "Same story", "b" * 64, "key-a"),
        ("https://zoo.example/b", "https://zoo.example/b", " same   story ", "b" * 64, "key-b"),
    ]
    connection = _dedup_fixture(entries)
    try:
        report = _dedup_report(
            connection,
            before_total=0,
            after_run1_total=2,
            after_run2_total=2,
            run1_canonicals={entry[0] for entry in entries},
            run2_canonicals={entry[0] for entry in entries},
        )
    finally:
        connection.close()

    assert report["status"] == "FAIL"
    assert report["content_identity_duplicates"] == [
        {"value": f"{'b' * 64}\x00same story", "count": 2}
    ]


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
    ("evidence", "classification"),
    [
        ({"status": "success_no_items"}, "SUPPORTED_NO_CURRENT_NEWS"),
        ({"status": "failed", "http_status": 404, "error": "HTTP 404"}, "SOURCE_NOT_FOUND"),
        ({"status": "error", "error_category": "robots_disallowed"}, "ROBOTS_DISALLOWED"),
        ({"status": "failed", "error_category": "javascript_required"}, "JAVASCRIPT_REQUIRED"),
        ({"status": "failed", "error_category": "blocked"}, "BLOCKED"),
        ({"status": "failed", "error_category": "unsupported"}, "UNSUPPORTED"),
    ],
    ids=[
        "no-current-news", "source-not-found", "robots", "javascript", "blocked", "unsupported",
    ],
)
def test_external_limitations_are_stable_nonblocking_classifications(evidence, classification):
    info = _classification_info(evidence)

    assert info["public_classification"] == classification
    assert info["raw_categories"] == _raw_categories_from_evidence(evidence)
    assert info["nonblocking"] is True
    assert info["blocking_categories"] == []


def test_honest_partial_result_is_nonblocking_but_parser_partial_is_blocking():
    partial = _classification_info({"status": "partial"})
    assert partial["public_classification"] == "PARTIALLY_SUPPORTED"
    assert partial["nonblocking"] is True

    parser_partial = _classification_info(
        {"status": "partial", "error_category": "parse_error"}
    )
    assert parser_partial["public_classification"] == "FAILED"
    assert parser_partial["nonblocking"] is False


@pytest.mark.parametrize(
    "category",
    ["parse_error", "date_parse_error", "content_parse_error", "configuration_error"],
)
def test_parser_date_content_and_configuration_failures_remain_blocking(category):
    info = _classification_info({"status": "failed", "error_category": category})

    assert info["public_classification"] == "FAILED"
    assert info["nonblocking"] is False
    assert category in info["blocking_categories"]


def test_classification_counts_emit_all_nine_labels_and_isolate_blocking_zoo():
    zoos = [
        Zoo(id="zoo-external", slug="external", name="External"),
        Zoo(id="zoo-code", slug="code", name="Code"),
    ]
    config = SimpleNamespace(zoos=zoos, sources=[])
    result = SimpleNamespace(
        zoo_results=[
            {
                "zoo_id": "zoo-external",
                "status": "failed",
                "error_category": "robots_disallowed",
            },
            {
                "zoo_id": "zoo-code",
                "status": "failed",
                "error_category": "parse_error",
            },
        ]
    )

    classified = _classify_crawl_results(config, result)

    assert set(classified["classification_counts"]) == {
        "SUPPORTED", "SUPPORTED_NO_CURRENT_NEWS", "PARTIALLY_SUPPORTED",
        "SOURCE_NOT_FOUND", "ROBOTS_DISALLOWED", "JAVASCRIPT_REQUIRED",
        "BLOCKED", "UNSUPPORTED", "FAILED",
    }
    assert classified["classification_counts"]["ROBOTS_DISALLOWED"] == 1
    assert classified["classification_counts"]["FAILED"] == 1
    assert classified["by_slug"]["external"]["nonblocking"] is True
    assert classified["by_slug"]["code"]["nonblocking"] is False
    assert classified["status"] == "FAIL"


@pytest.mark.parametrize(
    ("results", "failure_fragment", "zoo_two_classification"),
    [
        ([{"zoo_id": "zoo-1", "status": "success"}], "zoo zoo-2: expected one result, got 0", "FAILED"),
        (
            [
                {"zoo_id": "zoo-1", "status": "success"},
                {"zoo_id": "zoo-1", "status": "success"},
                {"zoo_id": "zoo-2", "status": "success"},
            ],
            "zoo zoo-1: expected one result, got 2",
            "SUPPORTED",
        ),
    ],
    ids=["missing-result", "duplicate-result"],
)
def test_classification_requires_one_result_per_enabled_zoo(results, failure_fragment, zoo_two_classification):
    config = SimpleNamespace(
        zoos=[
            Zoo(id="zoo-1", slug="one", name="One"),
            Zoo(id="zoo-2", slug="two", name="Two"),
        ],
        sources=[],
    )

    classified = _classify_crawl_results(config, SimpleNamespace(zoo_results=results))

    assert classified["status"] == "FAIL"
    assert failure_fragment in classified["failures"]
    assert classified["by_slug"]["two"]["public_classification"] == zoo_two_classification


def _synthetic_73_roster():
    zoos = []
    sources = []
    for index in range(1, 70):
        zoo_id = f"zoo-{index:02d}"
        zoos.append(
            Zoo(
                id=zoo_id,
                slug=zoo_id,
                name=f"Zoo {index}",
                list_provenance=[{"fixture": zoo_id}],
            )
        )
        host = f"{zoo_id}.example"
        sources.append(
            Source(
                id=f"source-{index:02d}",
                zoo_id=zoo_id,
                url=f"https://{host}/news",
                config={"official_hosts": [host]},
            )
        )
    for index in range(1, 5):
        zoo_id = f"zoo-gap-{index}"
        zoos.append(
            Zoo(
                id=zoo_id,
                slug=zoo_id,
                name=f"No-site gap {index}",
                list_provenance=[{"fixture": zoo_id}],
            )
        )
    return SimpleNamespace(zoos=zoos, sources=sources)


def _synthetic_73_results():
    return [
        {"zoo_id": f"zoo-{index:02d}", "status": "success"}
        for index in range(1, 70)
    ] + [
        {
            "zoo_id": f"zoo-gap-{index}",
            "status": "failed",
            "error_category": "source_not_found",
        }
        for index in range(1, 5)
    ]


def test_73_roster_coverage_is_distinct_from_executable_sources_and_covers_no_site_gaps():
    config = _synthetic_73_roster()
    registry = _validate_live_registry(config)
    assert registry["status"] == "PASS"
    assert registry["roster_membership_count"] == 73
    assert registry["roster_membership_coverage"] is True
    assert registry["roster_provenance_count"] == 73
    assert registry["roster_provenance_coverage"] is True
    assert registry["enabled_source_count"] == 69
    assert registry["executable_source_count"] == 69
    assert registry["zoos_without_enabled_sources"] == [
        "zoo-gap-1", "zoo-gap-2", "zoo-gap-3", "zoo-gap-4"
    ]

    classified = _classify_crawl_results(
        config, SimpleNamespace(zoo_results=_synthetic_73_results())
    )
    assert classified["status"] == "PASS"
    assert len(classified["results"]) == 73
    assert sum(classified["classification_counts"].values()) == 73
    assert classified["classification_counts"]["SUPPORTED"] == 69
    assert classified["classification_counts"]["SOURCE_NOT_FOUND"] == 4
    assert all(
        classified["by_id"][f"zoo-gap-{index}"]["nonblocking"]
        for index in range(1, 5)
    )


def test_73_roster_provenance_gap_is_blocking_when_provenance_is_declared():
    config = _synthetic_73_roster()
    config.zoos[-1].list_provenance = []

    registry = _validate_live_registry(config)

    assert registry["status"] == "FAIL"
    assert registry["roster_provenance_count"] == 72
    assert registry["roster_provenance_coverage"] is False
    assert any("roster provenance is incomplete" in item for item in registry["failures"])


@pytest.mark.parametrize("mutation", ["missing", "duplicate"])
def test_73_roster_result_coverage_rejects_missing_or_duplicate_result(mutation):
    config = _synthetic_73_roster()
    results = _synthetic_73_results()
    if mutation == "missing":
        results.pop()
    else:
        results.insert(0, dict(results[0]))

    classified = _classify_crawl_results(
        config, SimpleNamespace(zoo_results=results)
    )

    assert classified["status"] == "FAIL"
    assert any("expected one result" in failure for failure in classified["failures"])


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


def test_golden_zoo_keeps_article_minimum_when_external_limitation_is_reported():
    connection, config, _content_hash = _article_validation_fixture("2" * 64)
    zoo = config.zoos[0]
    source = config.sources[0]
    zoo.id = "zoo-berlin"
    zoo.slug = "zoo-berlin"
    source.zoo_id = "zoo-berlin"
    connection.execute("UPDATE zoos SET id=?, slug=? WHERE id=?", (zoo.id, zoo.slug, "zoo-1"))
    connection.execute("UPDATE sources SET zoo_id=? WHERE id=?", (source.zoo_id, source.id))
    try:
        validation = _validate_articles(
            connection,
            config,
            minimum_per_zoo=3,
            accepted_zoo_classifications={"zoo-berlin": "SOURCE_NOT_FOUND"},
        )
    finally:
        connection.close()

    assert validation["status"] == "FAIL"
    assert validation["zoos"]["zoo-berlin"]["article_count"] == 1
    assert validation["zoos"]["zoo-berlin"]["status"] == "FAIL"


def test_validation_accepts_four_enabled_configured_zoos_when_all_are_covered():
    connection, config, content_hash = _article_validation_fixture("2" * 64)
    extra_zoos = []
    extra_sources = []
    for index in range(2, 5):
        zoo_id = f"zoo-{index}"
        slug = f"zoo-{index}"
        host = f"{slug}.example"
        source_id = f"source-{index}"
        article_id = f"article-{index}"
        canonical = f"https://{host}/news/article-{index}"
        connection.execute("INSERT INTO zoos VALUES (?, ?, ?)", (zoo_id, slug, f"Zoo {index}"))
        connection.execute(
            "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source_id, zoo_id, f"https://{host}/news", "rss", "completed", 1, None),
        )
        connection.execute(
            "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                article_id, canonical, canonical, f"Article {index}", None,
                content_hash, "2" * 64, "<html>exact bytes</html>", "{}",
                "2026-08-13T10:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO article_discoveries VALUES (?, ?, ?, ?, ?)",
            (f"discovery-{index}", article_id, source_id, canonical, "2026-08-13T10:00:00+00:00"),
        )
        extra_zoos.append(
            Zoo(
                id=zoo_id, slug=slug, name=f"Zoo {index}",
                website_url=f"https://{host}", country_code="DE", language="de",
            )
        )
        extra_sources.append(
            Source(
                id=source_id, zoo_id=zoo_id, url=f"https://{host}/news",
                kind="rss", language="de", config={"official_hosts": [host]},
            )
        )
    config.zoos.extend(extra_zoos)
    config.sources.extend(extra_sources)
    try:
        validation = _validate_articles(connection, config, minimum_per_zoo=1)
        report = _empty_zoo_report(config)
    finally:
        connection.close()

    assert validation["status"] == "PASS"
    assert set(validation["zoos"]) == {"zoo-one", "zoo-2", "zoo-3", "zoo-4"}
    assert set(report) == set(validation["zoos"])


def test_validation_rejects_missing_enabled_configured_zoo():
    connection, config, _content_hash = _article_validation_fixture("2" * 64)
    config.zoos.append(
        Zoo(
            id="zoo-4", slug="zoo-four", name="Zoo Four",
            website_url="https://four.example", country_code="DE", language="de",
        )
    )
    config.sources.append(
        Source(
            id="source-4", zoo_id="zoo-4", url="https://four.example/news",
            kind="rss", language="de", config={"official_hosts": ["four.example"]},
        )
    )
    try:
        validation = _validate_articles(connection, config, minimum_per_zoo=1)
    finally:
        connection.close()

    assert validation["status"] == "FAIL"
    assert validation["zoos"]["zoo-four"]["status"] == "FAIL"
    assert validation["zoos"]["zoo-four"]["article_count"] == 0


def test_validation_rejects_subdomain_source_unless_explicitly_configured():
    connection, config, _content_hash = _article_validation_fixture("2" * 64)
    source = config.sources[0]
    source.url = "https://news.zoo.example/news"
    connection.execute("UPDATE sources SET url=? WHERE id=?", (source.url, source.id))
    try:
        rejected = _validate_articles(connection, config, minimum_per_zoo=1)
        source.config["official_hosts"] = ["zoo.example", "news.zoo.example"]
        accepted = _validate_articles(connection, config, minimum_per_zoo=1)
    finally:
        connection.close()

    assert rejected["status"] == "FAIL"
    assert rejected["zoos"]["zoo-one"]["successful_official_sources"] == []
    assert accepted["status"] == "PASS"
    assert accepted["zoos"]["zoo-one"]["successful_official_sources"] == ["source-1"]


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


@pytest.mark.parametrize(
    ("status", "success", "last_success", "last_error", "last_http_status", "expected"),
    [
        ("error", 0, None, "robots.txt disallowed", None, "PASS"),
        ("partial", 1, None, "partial source coverage", 200, "PASS"),
        ("error", 0, None, "parse error in feed", None, "FAIL"),
    ],
    ids=["robots-is-external", "honest-partial", "parser-is-blocking"],
)
def test_source_health_distinguishes_external_or_partial_from_code_failure(
    status, success, last_success, last_error, last_http_status, expected
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE sources (
            id TEXT, status TEXT, success INTEGER, last_checked TEXT,
            last_success TEXT, last_error TEXT, last_http_status INTEGER
        )"""
    )
    connection.execute(
        "INSERT INTO sources VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("source-one", status, success, "2026-08-13", last_success, last_error, last_http_status),
    )
    config = type("Config", (), {
        "sources": [Source(id="source-one", zoo_id="zoo-one", url="https://zoo.example/news")]
    })()

    validation = _validate_enabled_sources(config, connection)
    connection.close()

    assert validation["status"] == expected


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


def _complete_static_report_observation(*, expanded_roster_available=False):
    if expanded_roster_available:
        scope_banner = (
            "Acceptance: PASS Scope: Authoritative Phase0 v0.1 roster · "
            "expanded roster available (73 zoos)."
        )
        expected_scope_label = "Authoritative Phase0 v0.1 roster"
        expected_zoo_count = 73
    else:
        scope_banner = (
            "Acceptance: PASS Scope: Configured registry only · no expanded roster is implied."
        )
        expected_scope_label = "Configured registry only"
        expected_zoo_count = 2
    return {
        "title": "ZooFanCrawler acceptance",
        "scope_banner": scope_banner,
        "expected_scope_label": expected_scope_label,
        "expanded_roster_available": expanded_roster_available,
        "expected_zoo_count": expected_zoo_count,
        "row_count": expected_zoo_count,
        "controls": {
            "country-filter": True,
            "group-filter": True,
            "status-filter": True,
            "name-filter": True,
            "sort-filter": True,
        },
        "search": {"ok": True, "exercised": True},
        "filters": {"ok": True, "exercised": True},
        "sort": {"ok": True, "exercised": True},
        "detail": {"ok": True, "exercised": True},
        "metric_labels": [
            "Configured zoos", "Enabled zoos", "Configured sources", "Enabled source checks",
            "Unique articles (cumulative)", "Source/article associations", "Discovered (latest run)",
            "Fetched (latest run)", "Parsed (latest run)", "Stored (latest run)",
            "Inserted (latest run)", "Updated (latest run)", "Already known (latest run)",
            "Duplicate filtered (latest run)", "Errors (latest run)", "Warnings",
            "Latest-run result coverage", "Wall duration (latest run)",
        ],
        "table_columns": [
            "Name", "Country", "Region", "Groups", "Official site", "News source URL",
            "Adapter", "Completion", "Run status", "HTTP", "Discovered", "Parsed",
            "Inserted", "Failed", "Latest errors", "Latest news date", "Duration",
            "Error summary",
        ],
        "category_labels": " ".join(
            [
                "SUPPORTED", "SUPPORTED_NO_CURRENT_NEWS", "PARTIALLY_SUPPORTED",
                "SOURCE_NOT_FOUND", "ROBOTS_DISALLOWED", "JAVASCRIPT_REQUIRED",
                "BLOCKED", "UNSUPPORTED", "FAILED",
            ]
        ),
        "raw_html_found": False,
        "generation_id": "fresh-generation",
        "expected_generation_id": "fresh-generation",
        "horizontal_overflow": {"desktop": False, "mobile": False},
        "browser_assertion_errors": [],
        "page_errors": [],
        "console_errors": [],
        "screenshot_requested": False,
        "screenshot_written_this_run": False,
    }


def test_static_report_observation_positive_control_is_strict():
    result = evaluate_static_report_observation(
        _complete_static_report_observation(),
        expected_zoo_count=2,
        expected_scope_label="Configured registry only",
    )

    assert result == {"status": "PASS", "failures": []}


def test_static_report_observation_accepts_the_expanded_73_row_roster():
    observation = _complete_static_report_observation(expanded_roster_available=True)
    observation.update(initial_row_count=73)

    result = evaluate_static_report_observation(
        observation,
        expected_zoo_count=73,
        expected_scope_label="Authoritative Phase0 v0.1 roster",
    )

    assert result == {"status": "PASS", "failures": []}


@pytest.mark.parametrize(
    "expanded_roster_available",
    [True, False],
    ids=["authoritative", "configured-only"],
)
def test_static_report_observation_uses_expanded_roster_branch_for_scope_banner(
    expanded_roster_available,
):
    observation = _complete_static_report_observation(
        expanded_roster_available=expanded_roster_available
    )
    if expanded_roster_available:
        observation["scope_banner"] = (
            "Acceptance: PASS Scope: Configured registry only · no expanded roster is implied."
        )
    else:
        observation["scope_banner"] = (
            "Acceptance: PASS Scope: Authoritative Phase0 v0.1 roster · "
            "expanded roster available (73 zoos)."
        )

    result = evaluate_static_report_observation(observation)

    assert result["status"] == "FAIL"
    assert any("static report scope banner" in item for item in result["failures"])


def test_static_report_observation_requires_current_enabled_source_checks_metric():
    observation = _complete_static_report_observation()
    observation["metric_labels"].remove("Enabled source checks")

    result = evaluate_static_report_observation(observation)

    assert result["status"] == "FAIL"
    assert "static report metrics: missing Enabled source checks" in result["failures"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.pop("expanded_roster_available"),
        lambda item: item.update(expanded_roster_available="false"),
    ],
    ids=["missing", "non-bool"],
)
def test_static_report_observation_requires_boolean_expanded_roster_evidence(mutate):
    observation = _complete_static_report_observation()
    mutate(observation)

    result = evaluate_static_report_observation(observation)

    assert result["status"] == "FAIL"
    assert (
        "static report scope banner: expanded-roster availability evidence is unavailable"
        in result["failures"]
    )


@pytest.mark.parametrize(
    ("mutate", "failure_fragment"),
    [
        (
            lambda item: item.update(title="wrong title"),
            "static report title",
        ),
        (
            lambda item: item.update(row_count=1),
            "static report table: expected 2 enabled-zoo rows",
        ),
        (
            lambda item: item["controls"].update({"name-filter": False}),
            "static report controls: missing required control 'name-filter'",
        ),
        (
            lambda item: item.update(search={"ok": False}),
            "static report interaction: search did not produce valid evidence",
        ),
        (
            lambda item: item.update(raw_html_found=True),
            "static report safety: raw_html is present",
        ),
        (
            lambda item: item["horizontal_overflow"].update({"mobile": True}),
            "static report layout: horizontal overflow at mobile viewport",
        ),
        (
            lambda item: item.update(expected_generation_id="new-generation"),
            "static report generation: browser opened a different generation",
        ),
        (
            lambda item: item.update(console_errors=["ReferenceError"]),
            "static report console error: ReferenceError",
        ),
        (
            lambda item: item["table_columns"].remove("Latest errors"),
            "static report table columns: missing Latest errors",
        ),
    ],
    ids=["title", "row-count", "control", "search", "raw-html", "overflow", "generation", "console", "table-column"],
)
def test_static_report_observation_failure_evidence_is_named(mutate, failure_fragment):
    observation = _complete_static_report_observation()
    mutate(observation)

    result = evaluate_static_report_observation(observation)

    assert result["status"] == "FAIL"
    assert any(failure_fragment in item for item in result["failures"])


def test_static_report_builder_identifies_fresh_generation(tmp_path, monkeypatch):
    import zoofan.reporting as reporting

    report_path = tmp_path / "reports" / "latest" / "acceptance" / "index.html"

    class Build:
        paths = {"acceptance/index.html": report_path}
        projection = {
            "generation_id": "fresh-generation",
            "scope": {
                "label": "Configured registry only",
                "expanded_roster_available": False,
            },
        }

    def fake_build_reports(**_kwargs):
        report_path.parent.mkdir(parents=True)
        report_path.write_text("<title>ZooFanCrawler acceptance</title>", encoding="utf-8")
        return Build()

    monkeypatch.setattr(reporting, "build_reports", fake_build_reports)
    result = _build_static_report(tmp_path / "zoos.yaml", tmp_path / "acceptance.db", tmp_path / "reports")

    assert result["status"] == "PASS"
    assert result["generation_id"] == "fresh-generation"
    assert result["scope_label"] == "Configured registry only"
    assert result["expanded_roster_available"] is False
    assert result["report_path"] == str(report_path.resolve())


def test_static_report_screenshot_cleanup_is_exact(tmp_path):
    screenshot = tmp_path / "static-acceptance.png"
    screenshot.write_bytes(b"stale")

    _prepare_static_report_screenshot(screenshot)

    assert not screenshot.exists()


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


def _run_zoo_evidence_fixture():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE crawl_zoo_results ("
        "id TEXT PRIMARY KEY, crawl_run_id TEXT, zoo_id TEXT"
        ")"
    )
    zoos = [
        Zoo(id="zoo-1", slug="one", name="One", enabled=True),
        Zoo(id="zoo-2", slug="two", name="Two", enabled=True),
    ]
    sources = [
        Source(id="source-1", zoo_id="zoo-1", url="https://one.example/feed"),
        Source(id="source-2", zoo_id="zoo-2", url="https://two.example/feed"),
    ]
    config = SimpleNamespace(zoos=zoos, sources=sources)
    connection.executemany(
        "INSERT INTO crawl_zoo_results VALUES (?, ?, ?)",
        [("result-1", "run-zoo", "zoo-1"), ("result-2", "run-zoo", "zoo-2")],
    )
    result = SimpleNamespace(
        metadata={
            "enabled": 2,
            "processed": 2,
            "selected": 2,
            "enabled_zoos": ["zoo-1", "zoo-2"],
            "processed_zoos": ["zoo-1", "zoo-2"],
            "selected_zoos": ["zoo-1", "zoo-2"],
        }
    )
    return connection, config, result


def _audit_run_zoo_fixture(connection, config, result):
    return _validate_run_zoo_evidence(
        config,
        connection,
        run_id="run-zoo",
        result=result,
    )


def test_current_run_zoo_evidence_requires_exact_enabled_coverage():
    connection, config, result = _run_zoo_evidence_fixture()
    try:
        validation = _audit_run_zoo_fixture(connection, config, result)
    finally:
        connection.close()

    assert validation["status"] == "PASS"
    assert validation["enabled_zoo_ids"] == ["zoo-1", "zoo-2"]
    assert validation["result_zoo_ids"] == ["zoo-1", "zoo-2"]
    assert validation["result_count"] == 2
    assert validation["failures"] == []


@pytest.mark.parametrize(
    ("mutate", "failure_fragment"),
    [
        (
            lambda db: db.execute("DELETE FROM crawl_zoo_results WHERE zoo_id='zoo-2'"),
            "missing zoo result for enabled zoo(s): zoo-2",
        ),
        (
            lambda db: db.execute(
                "INSERT INTO crawl_zoo_results VALUES ('result-extra', 'run-zoo', 'zoo-extra')"
            ),
            "unexpected zoo result ID(s): zoo-extra",
        ),
        (
            lambda db: db.execute(
                "INSERT INTO crawl_zoo_results VALUES ('result-duplicate', 'run-zoo', 'zoo-1')"
            ),
            "zoo result ID(s) have duplicate rows: zoo-1",
        ),
    ],
    ids=["missing-zoo", "extra-zoo", "duplicate-zoo"],
)
def test_current_run_zoo_evidence_adversarial_ids_are_named(mutate, failure_fragment):
    connection, config, result = _run_zoo_evidence_fixture()
    mutate(connection)
    try:
        validation = _audit_run_zoo_fixture(connection, config, result)
    finally:
        connection.close()

    assert validation["status"] == "FAIL"
    assert any(failure_fragment in item for item in validation["failures"])


def test_current_run_zoo_evidence_rejects_metadata_scope_mismatch():
    connection, config, result = _run_zoo_evidence_fixture()
    result.metadata["processed_zoos"] = ["zoo-1"]
    try:
        validation = _audit_run_zoo_fixture(connection, config, result)
    finally:
        connection.close()

    assert validation["status"] == "FAIL"
    assert any("metadata processed_zoos does not match enabled zoo IDs" in item for item in validation["failures"])


@pytest.mark.parametrize(
    ("config", "failure_fragment"),
    [
        (SimpleNamespace(zoos=[], sources=[]), "no enabled configured zoos"),
        (
            SimpleNamespace(
                zoos=[Zoo(id="zoo-1", slug="one", name="One", enabled=True)],
                sources=[Source(id="source-disabled", zoo_id="zoo-1", enabled=False)],
            ),
            "no enabled configured sources",
        ),
    ],
    ids=["no-enabled-zoos", "no-enabled-sources"],
)
def test_live_registry_rejects_empty_acceptance_scope(config, failure_fragment):
    validation = _validate_live_registry(config)

    assert validation["status"] == "FAIL"
    assert any(failure_fragment in item for item in validation["failures"])


def test_live_acceptance_rejects_empty_registry_before_crawling():
    connection = sqlite3.connect(":memory:")
    try:
        result = _run_live_acceptance(SimpleNamespace(zoos=[], sources=[]), connection)
    finally:
        connection.close()

    assert result["status"] == "FAIL"
    assert result["run1"] is None
    assert result["run2"] is None
    assert result["run_zoo_validation"] == {}
    assert any("no enabled configured zoos" in item for item in result["failures"])
    assert any("no enabled configured sources" in item for item in result["failures"])


def test_endgoal_main_loads_explicit_config_path_without_live_work(tmp_path, monkeypatch):
    import zoofan.endgoal as endgoal
    import zoofan.storage as storage_module

    config_path = tmp_path / "custom-zoos.yaml"
    config_path.write_text("custom: true\n")
    loaded_paths = []
    config = SimpleNamespace(zoos=[], sources=[])

    def load_config(path):
        loaded_paths.append(path)
        return config

    class FakeStorage:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    live = {
        "status": "PASS",
        "failures": [],
        "zoos": {},
        "dedup": {"status": "PASS", "failures": []},
    }
    pass_gate = {"status": "PASS", "failures": []}
    static_smoke_calls = []
    monkeypatch.setattr(endgoal, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(endgoal, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(endgoal, "DB_PATH", tmp_path / "data" / "acceptance.db")
    monkeypatch.setattr(endgoal, "REPORT_PATH", tmp_path / "artifacts" / "endgoal-report.json")
    monkeypatch.setattr(endgoal, "SCREENSHOT_PATH", tmp_path / "artifacts" / "dashboard-acceptance.png")
    monkeypatch.setattr(endgoal, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(endgoal, "_run_subprocess", lambda *args, **kwargs: pass_gate)
    monkeypatch.setattr(endgoal, "_run_live_acceptance", lambda loaded_config, connection: live)
    monkeypatch.setattr(endgoal, "_source_findings", lambda loaded_config, connection: pass_gate)
    monkeypatch.setattr(endgoal, "_failure_isolation", lambda: pass_gate)
    monkeypatch.setattr(endgoal, "_reserve_port", lambda: 8123)
    monkeypatch.setattr(endgoal, "_start_dashboard", lambda db_path, port: object())
    monkeypatch.setattr(endgoal, "_wait_for_dashboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(endgoal, "_dashboard_browser_smoke", lambda *args, **kwargs: pass_gate)
    monkeypatch.setattr(
        endgoal,
        "_build_static_report",
        lambda *args, **kwargs: {
            "status": "PASS",
            "report_path": str(tmp_path / "reports" / "latest" / "acceptance" / "index.html"),
            "generation_id": "fresh-generation",
            "scope_label": "Configured registry only",
            "expanded_roster_available": False,
            "files": {},
        },
    )

    def fake_static_report_browser_smoke(*args, **kwargs):
        static_smoke_calls.append(kwargs)
        return pass_gate

    monkeypatch.setattr(endgoal, "_static_report_browser_smoke", fake_static_report_browser_smoke)
    monkeypatch.setattr(endgoal, "_terminate_process", lambda process: None)
    monkeypatch.setattr(endgoal, "_db_connection", lambda path: sqlite3.connect(":memory:"))
    monkeypatch.setattr(storage_module, "SQLiteStorage", FakeStorage)

    import zoofan.config as config_module

    monkeypatch.setattr(config_module, "load_config", load_config)

    assert endgoal.main(config_path=config_path) == 0
    assert loaded_paths == [config_path]
    assert len(static_smoke_calls) == 1
    assert static_smoke_calls[0]["expected_expanded_roster_available"] is False
    report = json.loads((tmp_path / "artifacts" / "endgoal-report.json").read_text())
    assert report["config"] == str(config_path)
