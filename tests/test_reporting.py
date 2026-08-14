"""Deterministic checks for the read-only report projection and artifacts."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from zoofan.config import load_config
from zoofan.models import Article, CrawlRun, CrawlRunStat, CrawlZooResult, Source
from zoofan.reporting import (
    KNOWN_ADAPTERS,
    PUBLIC_COMPLETION_CLASSIFICATIONS,
    SUPPORTED_ADAPTERS,
    _source_navigation_match,
    build_projection,
    build_reports,
)
from zoofan.storage import SQLiteStorage


CONFIG = """
zoos:
  - id: alpha
    slug: alpha
    name: Alpha <Zoo>
    groups: [pilot]
    source_status: configured
    country_code: DE
    language: de
    website_url: https://alpha.example/
    metadata:
      groups: [pilot]
      arbitrary: should-not-be-exported
    sources:
      - id: alpha-feed
        name: Feed
        language: de
        kind: rss
        url: https://alpha.example/feed.xml
        config:
          official_host: alpha.example
      - id: alpha-archive
        name: Archive
        language: de
        kind: archive
        url: https://alpha.example/archive/
  - id: empty
    slug: empty
    name: Empty Zoo
    groups: []
    source_status: configured
    country_code: NL
    language: nl
    website_url: https://empty.example/
    sources: []
"""


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "zoos.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _populated_db(tmp_path: Path, config_path: Path) -> Path:
    db_path = tmp_path / "crawler.db"
    config = load_config(config_path)
    with SQLiteStorage(db_path) as storage:
        for zoo in config.zoos:
            storage.upsert_zoo(zoo)
        for source in config.sources:
            storage.upsert_source(source)
        run = storage.start_crawl_run(CrawlRun(batch_id="batch-1", started_at="2025-01-01T00:00:00Z"))
        source = config.sources[0]
        storage.record_run_stat(
            CrawlRunStat(
                crawl_run_id=run.id,
                zoo_id=source.zoo_id,
                source_id=source.id,
                status="completed",
                discovered_count=2,
                fetched_count=2,
                stored_count=1,
                already_known_count=1,
            )
        )
        assert run.id is not None
        storage.finish_crawl_run(run.id, finished_at="2025-01-01T00:01:00Z")
        storage.upsert_article(
            Article(
                id="article-1",
                canonical_url="https://alpha.example/article-1",
                title="Alpha article",
                content="A body",
                published_at="2024-12-31T00:00:00Z",
                last_fetched_at="2025-01-01T00:00:00Z",
                raw_html="<script>must never appear in reports</script>",
            ),
            source_id=source.id,
        )
    return db_path


def test_projection_includes_empty_configured_zoo_and_safe_fields(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-01T00:00:00Z", git_commit="abc123")

    assert [zoo["id"] for zoo in projection["zoos"]] == ["alpha", "empty"]
    assert projection["zoos"][1]["status"] in {"no_data", "missing_result"}
    assert projection["run"]["counts"]["discovered"] == 2
    assert projection["adapter_stats"]["rss"]["configured_count"] == 1
    assert projection["git_commit"] == "abc123"
    encoded = json.dumps(projection, ensure_ascii=False)
    assert "raw_html" not in encoded
    assert "must never appear" not in encoded
    assert "arbitrary" not in encoded


def test_all_artifacts_are_derived_from_one_projection_and_keep_unrelated_file(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    report_root = tmp_path / "reports"
    sentinel = report_root / "keep.txt"
    report_root.mkdir()
    sentinel.write_text("preserve", encoding="utf-8")
    result = build_reports(config_path=config_path, db_path=db_path, output_dir=report_root, now="2025-01-01T00:00:00Z", git_commit="abc123")

    assert result.output_dir == report_root / "latest"
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    for relative in ("run-summary.json", "run-summary.md", "zoo-status.csv", "articles.json", "acceptance/index.html"):
        assert result[relative].is_file()
    summary = json.loads(result["run-summary.json"].read_text(encoding="utf-8"))
    articles = json.loads(result["articles.json"].read_text(encoding="utf-8"))
    assert articles["articles"] == summary["articles"]
    rows = list(csv.DictReader(result["zoo-status.csv"].read_text(encoding="utf-8").splitlines()))
    assert {row["zoo_id"] for row in rows} == {"alpha", "empty"}
    assert "Alpha article" in result["run-summary.md"].read_text(encoding="utf-8")
    assert "raw_html" not in result["acceptance/index.html"].read_text(encoding="utf-8")


def test_html_escapes_xss_and_only_creates_safe_external_links(tmp_path):
    config_path = tmp_path / "xss.yaml"
    config_path.write_text(
        CONFIG.replace("Alpha <Zoo>", "<img src=x onerror=alert(1)>")
        .replace("https://alpha.example/article-1", "javascript:alert(1)"),
        encoding="utf-8",
    )
    db_path = _populated_db(tmp_path, config_path)
    result = build_reports(config_path=config_path, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-01T00:00:00Z", git_commit="xss")
    html = result["acceptance/index.html"].read_text(encoding="utf-8")
    assert "<img src=x" not in html
    assert "<script>must never appear" not in html
    assert "noopener noreferrer" in html
    assert "safeUrl" in html


def test_missing_empty_and_legacy_databases_are_non_fatal(tmp_path):
    config_path = _config_file(tmp_path)
    missing = build_projection(config_path=config_path, db_path=tmp_path / "missing.db", now="2025-01-01T00:00:00Z", git_commit="x")
    assert len(missing["zoos"]) == 2
    assert missing["database"]["available"] is False

    empty_path = tmp_path / "empty.db"
    sqlite3.connect(empty_path).close()
    empty = build_projection(config_path=config_path, db_path=empty_path, now="2025-01-01T00:00:00Z", git_commit="x")
    assert len(empty["zoos"]) == 2
    assert empty["counts"]["articles"] == 0

    legacy_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(legacy_path)
    connection.execute("CREATE TABLE zoos (id TEXT, slug TEXT, name TEXT)")
    connection.execute("INSERT INTO zoos VALUES ('alpha', 'alpha', 'Legacy Alpha')")
    connection.execute("CREATE TABLE articles (id TEXT, url TEXT, title TEXT)")
    connection.execute("INSERT INTO articles VALUES ('a', 'https://alpha.example/a', 'Legacy')")
    connection.commit()
    connection.close()
    legacy = build_projection(config_path=config_path, db_path=legacy_path, now="2025-01-01T00:00:00Z", git_commit="x")
    assert len(legacy["zoos"]) == 2
    assert legacy["database"]["legacy_schema"] is True
    assert legacy["articles"][0]["title"] == "Legacy"


def test_quality_warning_categories_are_deterministic(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = tmp_path / "quality.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE articles (id TEXT, zoo_id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT)")
    connection.executemany(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("quality-1", "alpha", "https://outside.example/list?page=1", "", "", "2026-01-02T00:00:00Z", "2020-01-01T00:00:00Z"),
            ("quality-2", "alpha", "https://outside.example/list?page=1", "", "", "2026-01-02T00:00:00Z", "2020-01-01T00:00:00Z"),
        ],
    )
    connection.commit()
    connection.close()
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-01T00:00:00Z", git_commit="x")
    codes = set(projection["articles"][0]["warning_codes"])
    assert {"empty_title", "future_date", "duplicate_url", "empty_content", "non_official_host", "stale", "list_page_suspect"}.issubset(codes)
    # Repeated dates are normal feed evidence; the anomaly is only emitted at
    # zoo/run scope when all of a larger dated set collapses to one date.
    assert "same_date" not in codes
    assert "same_date" not in projection["zoos"][0]["warning_codes"]


def test_navigation_quality_uses_article_content_and_title_not_url_tokens():
    source = Source(
        id="prague-news",
        zoo_id="prague-zoo",
        url="https://www.zoopraha.cz/en/about-zoo/news",
        config={"official_host": "www.zoopraha.cz"},
    )

    # URLPolicy already rejects source/navigation identities.  A valid
    # article path may still contain those words, so URL tokens alone must not
    # create a blocking content-quality finding.
    assert not _source_navigation_match(
        source,
        "https://www.zoopraha.cz/en/about-zoo/news/16312-ground-hornbill",
        "Southern Ground-hornbill Chick: Exceptionally on View to Visitors This Year",
        "A southern ground-hornbill chick can exceptionally be seen by visitors at Prague Zoo.",
    )

    # These prefixes model the known body contamination cases: breadcrumb/site
    # chrome before prose, rather than ordinary article mentions of "news".
    assert _source_navigation_match(
        source,
        "https://www.burgerszoo.com/news/2026/08/seagrass",
        "Burgers' Zoo developing the world's largest seagrass aquarium",
        "Homepage News Go back Burgers' Zoo developing the world's largest seagrass aquarium.",
    )
    assert _source_navigation_match(
        source,
        "https://www.apenheul.nl/nieuws/2026/07/monkeys",
        "Zes doodshoofdaapjes geboren",
        "Openingstijden Tickets Contact Route Directions Download Zes doodshoofdaapjes geboren.",
    )
    assert not _source_navigation_match(
        source,
        "https://www.zoopraha.cz/en/about-zoo/news/normal-story",
        "A normal news article",
        "This normal article discusses news from the zoo and the animals' daily care.",
    )

    # An unmistakable navigation title remains a quality finding even when
    # the body itself is ordinary prose.
    assert _source_navigation_match(
        source,
        "https://www.zoopraha.cz/en/about-zoo/news/contact",
        "Contact",
        "The article body is present and readable.",
    )


def _two_run_result_db(tmp_path: Path, config_path: Path, *, articles: int = 0) -> Path:
    """Create two deterministic runs with one configured source stat each."""

    db_path = tmp_path / "two-runs.db"
    config = load_config(config_path)
    with SQLiteStorage(db_path) as storage:
        for zoo in config.zoos:
            storage.upsert_zoo(zoo)
        for source in config.sources:
            storage.upsert_source(source)
        source = config.sources[0]
        for index, started in enumerate(("2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z"), start=1):
            run = storage.start_crawl_run(CrawlRun(batch_id=f"batch-{index}", started_at=started))
            for stat_source in config.sources:
                storage.record_run_stat(
                    CrawlRunStat(
                        crawl_run_id=run.id,
                        zoo_id=stat_source.zoo_id,
                        source_id=stat_source.id,
                        status="completed",
                        discovered_count=2,
                        fetched_count=2,
                        stored_count=1,
                    )
                )
            assert run.id is not None
            storage.upsert_zoo_run_result(
                CrawlZooResult(
                    crawl_run_id=run.id,
                    zoo_id=source.zoo_id,
                    status="success",
                    source_status="success",
                    # Legacy rows used this value for successful results;
                    # reporting must keep it out of error_category.
                    error_category="success",
                    discovered=2,
                    parsed=2,
                    inserted=1,
                )
            )
            storage.finish_crawl_run(run.id, finished_at=f"2025-01-0{index}T00:01:00Z")
        for index in range(articles):
            storage.upsert_article(
                Article(
                    id=f"same-date-{index}",
                    canonical_url=f"https://alpha.example/article-{index}",
                    title=("A " + "very long title " * 8) if index == 0 else f"Article {index}",
                    content="Body text for the article.",
                    summary="A useful summary.",
                    image_url="https://alpha.example/images/article.jpg",
                    parse_status="parsed",
                    published_at="2025-01-01T00:00:00Z",
                    last_fetched_at="2025-01-02T00:00:00Z",
                    raw_html="<script>must-not-appear</script>",
                ),
                source_id=source.id,
            )
    return db_path


def test_successful_result_separates_source_status_and_error_category(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path)
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    alpha = next(zoo for zoo in projection["zoos"] if zoo["id"] == "alpha")
    assert alpha["run_source_status"] == "success"
    assert alpha["run_error_category"] is None
    result = next(item for item in projection["run"]["zoo_results"] if item["zoo_id"] == "alpha")
    assert result["source_status"] == "success"
    assert result["error_category"] is None
    report = build_reports(config_path=config_path, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-02T00:00:00Z", git_commit="x")
    row = next(row for row in csv.DictReader(report["zoo-status.csv"].read_text(encoding="utf-8").splitlines()) if row["zoo_id"] == "alpha")
    assert row["source_status"] == "success"
    assert row["error_category"] == ""


def test_same_date_anomaly_is_zoo_level_and_long_title_is_generic(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path, articles=3)
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    alpha = next(zoo for zoo in projection["zoos"] if zoo["id"] == "alpha")
    assert "same_date" in alpha["warning_codes"]
    assert all("same_date" not in article["warning_codes"] for article in projection["articles"])
    assert "long_title" in projection["articles"][0]["warning_codes"]
    assert projection["run"]["anomalies"][0]["warning"]["code"] == "same_date"


def test_identical_runs_have_zero_adapter_changes_and_safe_article_detail(tmp_path):
    config_path = tmp_path / "single-zoo.yaml"
    config_path.write_text(CONFIG.split("  - id: empty", 1)[0].rstrip() + "\n", encoding="utf-8")
    db_path = _two_run_result_db(tmp_path, config_path, articles=1)
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    changes = projection["run"]["adapter_changes"]
    assert changes["available"] is True
    assert changes["changes"]["rss"] == 0
    assert changes["changes"]["archive"] == 0
    assert "unknown" not in changes["changes"]
    article = projection["articles"][0]
    alpha = next(zoo for zoo in projection["zoos"] if zoo["id"] == "alpha")
    assert projection["quality_warning_categories"] == {"long_title": 1}
    assert projection["counts"]["warnings"] == 1
    assert "long_title" in alpha["warning_codes"]
    assert article["body_summary"] == "A useful summary."
    assert article["image_url"] == "https://alpha.example/images/article.jpg"
    assert article["parse_status"] == "parsed"
    assert article["evidence"]["path"] == article["evidence_path"]
    report = build_reports(config_path=config_path, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-02T00:00:00Z", git_commit="x")
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "Body summary" in document
    assert "Image URL" in document
    assert "Parse status" in document
    assert 'setAttribute("loading", "lazy")' in document
    assert 'setAttribute("width", "320")' in document
    assert 'setAttribute("height", "180")' in document
    assert ".meta, .muted {" in document and "overflow-wrap:anywhere" in document and "word-break:break-word" in document
    assert ".report-panel { min-width:0;" in document
    assert "must-not-appear" not in document
    assert "raw_html" not in document


def test_article_detail_rejects_unsafe_image_and_protocol_relative_links(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path, articles=1)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE articles SET image_url=?, canonical_url=?",
        ("javascript:alert(1)", "//evil.example/article"),
    )
    connection.commit()
    connection.close()
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    article = projection["articles"][0]
    assert article["image_url"] is None
    assert article["safe_url"] is None
    report = build_reports(config_path=config_path, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-02T00:00:00Z", git_commit="x")
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "javascript:alert" not in document
    assert 'href="//evil.example' not in document


def test_official_host_requires_exact_match_but_allows_explicit_alias(tmp_path):
    config = {
        "zoos": [
            {
                "id": "official-zoo",
                "slug": "official-zoo",
                "name": "Official Zoo",
                "website_url": "https://official.example/",
                "sources": [
                    {
                        "id": "official-feed",
                        "kind": "rss",
                        "url": "https://official.example/feed.xml",
                        "config": {
                            "official_host": "official.example",
                            "official_host_aliases": ["alias.official.example"],
                        },
                    }
                ],
            }
        ]
    }
    db_path = tmp_path / "host-policy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE articles (id TEXT, zoo_id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT)"
    )
    connection.execute("CREATE TABLE article_discoveries (article_id TEXT, source_id TEXT)")
    connection.executemany(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("evil", "official-zoo", "https://evil.official.example/article", "Evil subdomain", "Body", None, "2025-01-01T00:00:00Z"),
            ("alias", "official-zoo", "https://alias.official.example/article", "Explicit alias", "Body", None, "2025-01-01T00:00:00Z"),
        ],
    )
    connection.executemany(
        "INSERT INTO article_discoveries VALUES (?, ?)",
        [("evil", "official-feed"), ("alias", "official-feed")],
    )
    connection.commit()
    connection.close()
    projection = build_projection(config=config, db_path=db_path, now="2025-01-01T00:00:00Z", git_commit="x")
    warnings_by_id = {article["id"]: set(article["warning_codes"]) for article in projection["articles"]}
    assert "non_official_host" in warnings_by_id["evil"]
    assert "non_official_host" not in warnings_by_id["alias"]


def test_latest_run_requires_exactly_one_result_per_enabled_zoo(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-01T00:00:00Z", git_commit="x")
    coverage = projection["coverage"]
    assert coverage["latest_run_enabled_zoo_count"] == 2
    assert coverage["latest_run_zoo_results"] == 0
    assert coverage["latest_run_zoo_result_coverage_percent"] == 0
    assert coverage["latest_run_zoo_result_coverage_percent"] <= 100
    criterion = next(item for item in projection["acceptance"]["criteria"] if item["id"] == "latest_run_zoo_result_coverage")
    assert criterion["passed"] is False
    assert projection["acceptance"]["passed"] is False


def test_generation_manifest_keeps_five_outputs_consistent_and_failed_stage_is_recoverable(tmp_path, monkeypatch):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    root = tmp_path / "reports"
    first = build_reports(config_path=config_path, db_path=db_path, output_dir=root, now="2025-01-01T00:00:00Z", git_commit="x")
    manifest_before = first["manifest.json"].read_bytes()
    generation_before = first.projection["generation_id"]
    import zoofan.reporting as reporting

    original = reporting._atomic_write
    calls = {"count": 0}

    def fail_second(path, content):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("injected stage failure")
        return original(path, content)

    monkeypatch.setattr(reporting, "_atomic_write", fail_second)
    try:
        build_reports(config_path=config_path, db_path=db_path, output_dir=root, now="2025-01-02T00:00:00Z", git_commit="y")
    except OSError:
        pass
    else:
        raise AssertionError("injected failure should propagate")
    assert first["manifest.json"].read_bytes() == manifest_before
    assert json.loads(first["manifest.json"].read_text())["generation_id"] == generation_before


def test_markdown_does_not_turn_malicious_url_into_link_and_html_rejects_protocol_relative(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE articles SET title=?, canonical_url=?", ("</script><img src=x onerror=1>", "//evil.example/article"))
    connection.commit()
    connection.close()
    result = build_reports(config_path=config_path, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-01T00:00:00Z", git_commit="x")
    markdown = result["run-summary.md"].read_text(encoding="utf-8")
    document = result["acceptance/index.html"].read_text(encoding="utf-8")
    assert "</script><img" not in document
    assert "\\u003c/script\\u003e" in document
    assert "<img src=x" not in document
    assert "new URL(raw)" in document
    assert "new URL(String(value), window.location.href)" not in document
    assert "[//evil.example/article]" not in markdown
    assert "`//evil.example/article`" in markdown


def _classification_config() -> dict:
    names = [
        "supported", "no-current-news", "partial", "source-not-found",
        "robots", "javascript", "blocked", "unsupported", "failed",
    ]
    return {
        "zoos": [
            {
                "id": name,
                "slug": name,
                "name": name.replace("-", " ").title(),
                "country_code": "DE",
                "language": "de",
                "website_url": f"https://{name}.example/",
                "sources": [{
                    "id": f"{name}-source",
                    "kind": "rss",
                    "url": f"https://{name}.example/feed.xml",
                }],
            }
            for name in names
        ]
    }


def _classification_db(tmp_path: Path) -> Path:
    path = tmp_path / "classifications.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE zoos (id TEXT, slug TEXT, name TEXT, website_url TEXT, country_code TEXT, language TEXT, groups_json TEXT, enabled INTEGER);
        CREATE TABLE sources (id TEXT, zoo_id TEXT, url TEXT, kind TEXT, status TEXT);
        CREATE TABLE articles (id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT);
        CREATE TABLE crawl_runs (id TEXT, batch_id TEXT, started_at TEXT, finished_at TEXT, duration_ms INTEGER, status TEXT, error TEXT);
        CREATE TABLE crawl_zoo_results (id TEXT, crawl_run_id TEXT, zoo_id TEXT, status TEXT, source_status TEXT, error_category TEXT, discovered INTEGER, parsed INTEGER, inserted INTEGER, updated INTEGER, failed INTEGER, duplicate_filtered INTEGER, duration_ms INTEGER);
        """
    )
    config = _classification_config()
    names = [item["id"] for item in config["zoos"]]
    connection.executemany(
        "INSERT INTO zoos VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        [(name, name, name.replace("-", " ").title(), f"https://{name}.example/", "DE", "de", "[]") for name in names],
    )
    connection.executemany(
        "INSERT INTO sources VALUES (?, ?, ?, 'rss', 'completed')",
        [(f"{name}-source", name, f"https://{name}.example/feed.xml") for name in names],
    )
    connection.execute(
        "INSERT INTO crawl_runs VALUES ('run-1', 'batch-1', '2025-01-01T00:00:00Z', '2025-01-01T00:00:09Z', 9000, 'completed', NULL)"
    )
    result_values = [
        ("supported", "success", "success", None),
        ("no-current-news", "success_no_items", "success_no_items", None),
        ("partial", "partial", "partial", "http_error"),
        ("source-not-found", "failed", "source_not_found", "source_not_found"),
        ("robots", "failed", "robots", "robots"),
        ("javascript", "failed", "javascript_required", "javascript_required"),
        ("blocked", "failed", "blocked", "blocked"),
        ("unsupported", "failed", "unsupported", "unsupported"),
        ("failed", "failed", "failed", "http_error"),
    ]
    connection.executemany(
        "INSERT INTO crawl_zoo_results VALUES (?, 'run-1', ?, ?, ?, ?, 0, 0, 0, 0, 0, 0, 1)",
        [(f"result-{name}", name, status, source_status, error_category) for name, status, source_status, error_category in result_values],
    )
    connection.commit()
    connection.close()
    return path


def test_public_completion_classes_and_cross_format_contract(tmp_path):
    config = _classification_config()
    db_path = _classification_db(tmp_path)
    report = build_reports(config=config, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-01T00:00:00Z", git_commit="test-commit")
    projection = report.projection
    assert {zoo["completion_classification"] for zoo in projection["zoos"]} == set(PUBLIC_COMPLETION_CLASSIFICATIONS)
    assert projection["completion_classifications"] == {key: 1 for key in PUBLIC_COMPLETION_CLASSIFICATIONS}
    assert sum(projection["completion_classifications"].values()) == projection["coverage"]["enabled_zoos"]
    # HTTP-backed FAILED/PARTIALLY_SUPPORTED fixtures remain visible as
    # evidence, but are external limitations rather than acceptance blockers.
    assert projection["acceptance"]["status"] == "PASS"
    assert projection["acceptance"]["external_limited"] is True
    assert projection["failure_reasons"]
    assert set(projection["failure_class_counts"]) <= {
        "blocked", "http_error", "javascript_required", "partial",
        "robots_disallowed", "source_not_found", "unsupported",
    }
    assert projection["blocking_failure_class_counts"] == {}
    assert set(projection["limitation_class_counts"]) == {
        "source_not_found", "robots_disallowed", "javascript_required", "blocked", "unsupported", "success_no_items",
    }
    assert {
        "Failed: status failed", "Partial: status partial", "Source Not Found: status failed",
        "Robots: status failed", "Javascript: status failed", "Blocked: status failed",
        "Unsupported: status failed",
    } <= set(projection["failure_reasons"])
    rows = list(csv.DictReader(report["zoo-status.csv"].read_text(encoding="utf-8").splitlines()))
    assert {row["completion_classification"] for row in rows} == set(PUBLIC_COMPLETION_CLASSIFICATIONS)
    markdown = report["run-summary.md"].read_text(encoding="utf-8")
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    for category in PUBLIC_COMPLETION_CLASSIFICATIONS:
        assert category in markdown
        assert category in document
    assert "Configured registry only" in markdown
    assert "source_article_associations" in json.dumps(projection)
    assert "table id=\"zoo-table\"" in document
    assert "sort-filter" in document and "selectedId = null" in document


def _single_result_fixture(
    tmp_path: Path,
    *,
    status: str = "failed",
    source_status: Optional[str] = "failed",
    error_category: Optional[str] = None,
    error_summary: Optional[str] = None,
    http_status: Optional[int] = None,
    run_status: str = "completed",
) -> tuple[dict, Path]:
    config = {
        "zoos": [
            {
                "id": "fixture-zoo",
                "slug": "fixture-zoo",
                "name": "Fixture Zoo",
                "website_url": "https://fixture.example/",
                "sources": [
                    {
                        "id": "fixture-source",
                        "kind": "rss",
                        "url": "https://fixture.example/feed.xml",
                    }
                ],
            }
        ]
    }
    db_path = tmp_path / f"{status}-{error_category or 'none'}-{http_status or 'none'}.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE zoos (id TEXT, slug TEXT, name TEXT, website_url TEXT, enabled INTEGER);
        CREATE TABLE sources (id TEXT, zoo_id TEXT, url TEXT, kind TEXT, status TEXT);
        CREATE TABLE articles (id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT);
        CREATE TABLE crawl_runs (id TEXT, status TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE crawl_zoo_results (
            id TEXT, crawl_run_id TEXT, zoo_id TEXT, status TEXT, source_status TEXT,
            error_category TEXT, discovered INTEGER, parsed INTEGER, inserted INTEGER,
            updated INTEGER, failed INTEGER, duplicate_filtered INTEGER, duration_ms INTEGER,
            error_summary TEXT, http_status INTEGER
        );
        """
    )
    connection.execute("INSERT INTO zoos VALUES ('fixture-zoo', 'fixture-zoo', 'Fixture Zoo', 'https://fixture.example/', 1)")
    connection.execute(
        "INSERT INTO sources VALUES ('fixture-source', 'fixture-zoo', 'https://fixture.example/feed.xml', 'rss', 'completed')"
    )
    connection.execute(
        "INSERT INTO crawl_runs VALUES ('run-1', ?, '2025-01-01T00:00:00Z', '2025-01-01T00:00:01Z')",
        (run_status,),
    )
    connection.execute(
        "INSERT INTO crawl_zoo_results VALUES (?, 'run-1', 'fixture-zoo', ?, ?, ?, 0, 0, 0, 0, ?, 0, 1, ?, ?)",
        (
            "fixture-result",
            status,
            source_status,
            error_category,
            1 if status in {"failed", "error", "failure"} else 0,
            error_summary,
            http_status,
        ),
    )
    connection.commit()
    connection.close()
    return config, db_path


@pytest.mark.parametrize(
    ("kwargs", "expected_status"),
    [
        (
            {
                "status": "failed",
                "source_status": "robots_disallowed",
                "error_category": "robots_disallowed",
                "error_summary": "robots.txt disallowed the official source",
                "run_status": "completed_with_errors",
            },
            "PASS",
        ),
        (
            {
                "status": "failed",
                "source_status": "failed",
                "error_summary": "HTTP 403 from the official source",
                "http_status": 403,
                "run_status": "completed_with_errors",
            },
            "PASS",
        ),
        (
            {
                "status": "failed",
                "source_status": "failed",
                "error_category": "parse_error",
                "error_summary": "article parse failed",
            },
            "FAIL",
        ),
        (
            {
                "status": "failed",
                "source_status": "failed",
                "error_category": "configuration_error",
                "error_summary": "storage/configuration failure",
            },
            "FAIL",
        ),
        (
            {
                "status": "failed",
                "source_status": "failed",
                "error_category": "mystery_internal",
                "error_summary": "unexpected internal failure",
            },
            "FAIL",
        ),
    ],
    ids=["robots-external", "http-403-external", "parser-blocking", "config-storage-blocking", "unknown-blocking"],
)
def test_external_limits_are_nonblocking_but_internal_failures_block(tmp_path, kwargs, expected_status):
    config, db_path = _single_result_fixture(tmp_path, **kwargs)
    report = build_reports(
        config=config,
        db_path=db_path,
        output_dir=tmp_path / "reports",
        now="2025-01-02T00:00:00Z",
        git_commit="fixture",
    )
    projection = report.projection
    assert projection["acceptance"]["status"] == expected_status
    zoo = projection["zoos"][0]
    if expected_status == "PASS":
        assert projection["external_limited"] is True
        assert zoo["external_limited"] is True
        assert projection["failure_reasons"]
        assert any(item["code"] == "source_error" for item in zoo["warnings"])
        assert projection["blocking_failure_class_counts"] == {}
    else:
        assert projection["blocking_failure_class_counts"]
        assert projection["acceptance"]["criteria"]


def test_source_coverage_distinguishes_checks_article_executables_and_health_only(tmp_path):
    report = build_reports(
        config_path=Path("config/zoos.yaml"),
        db_path=tmp_path / "missing.db",
        output_dir=tmp_path / "reports",
        now="2025-01-02T00:00:00Z",
        git_commit="coverage",
    )
    coverage = report.projection["coverage"]
    assert coverage["enabled_source_checks"] == 63
    assert coverage["article_capable_executable_enabled_sources"] == 44
    assert coverage["health_only_registry_only_enabled_sources"] == 19
    markdown = report["run-summary.md"].read_text(encoding="utf-8")
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "article-capable executable: 44" in markdown
    assert "health-only registry_only: 19" in markdown
    assert "Article-capable executable sources" in document
    assert "Health-only registry_only sources" in document


def test_scope_dirty_identity_long_title_warning_and_blocking_quality(tmp_path, monkeypatch):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path, articles=1)
    import zoofan.reporting as reporting

    monkeypatch.setattr(
        reporting,
        "_git_identity",
        lambda cwd=None: {"commit": "head", "dirty": True, "status": [" M zoofan/reporting.py"], "tree_fingerprint": "fingerprint"},
    )
    projection = reporting.build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="override")
    assert projection["scope"]["kind"] == "configured_registry"
    assert projection["scope"]["expanded_roster_available"] is False
    assert "no authoritative expanded zoo roster" in projection["scope"]["limitation"]
    assert projection["git_commit"] == "override"
    assert projection["git_dirty"] is True
    assert projection["code_state_fingerprint"] == "fingerprint"
    quality = next(item for item in projection["acceptance"]["criteria"] if item["id"] == "article_quality_rules")
    assert quality["passed"] is True
    assert projection["articles"][0]["warning_codes"] == ["long_title"]

    quality_db = tmp_path / "blocking.db"
    connection = sqlite3.connect(quality_db)
    connection.execute("CREATE TABLE articles (id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT)")
    connection.execute("INSERT INTO articles VALUES ('bad', 'not-a-url', '', '', NULL, NULL)")
    connection.commit()
    connection.close()
    blocking = reporting.build_projection(config_path=config_path, db_path=quality_db, now="2025-01-02T00:00:00Z", git_commit="override")
    blocking_quality = next(item for item in blocking["acceptance"]["criteria"] if item["id"] == "article_quality_rules")
    assert blocking_quality["passed"] is False
    assert {"empty_title", "bad_url", "empty_content"}.issubset(blocking["quality"]["blocking_codes"])


def _scope_config(*, count: int, provenance: object, groups: bool = True) -> dict:
    """Build a synthetic roster without changing the production registry."""

    return {
        "zoos": [
            {
                "id": f"scope-zoo-{index:03d}",
                "slug": f"scope-zoo-{index:03d}",
                "name": f"Scope Zoo {index:03d}",
                "website_url": f"https://scope-zoo-{index:03d}.example/",
                "country_code": "DE",
                "language": "de",
                "groups": ["phase0-central"] if groups else [],
                "list_provenance": provenance,
                "sources": [],
            }
            for index in range(count)
        ]
    }


def test_scope_derives_authoritative_phase0_roster_and_cross_format_evidence(tmp_path):
    roster_sha = "a" * 64
    provenance = [{
        "source": "data/zoofan_phase0_zoo_list_v0_1.md",
        "version": "Phase0 v0.1",
        "sha256": roster_sha,
        "expanded_count": 73,
    }]
    config = _scope_config(count=73, provenance=provenance)
    report = build_reports(
        config=config,
        db_path=tmp_path / "missing.db",
        output_dir=tmp_path / "reports",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )
    scope = report.projection["scope"]
    assert scope["kind"] == "authoritative_expanded_roster"
    assert scope["expanded_roster_available"] is True
    assert scope["expanded_roster_status"] == "available"
    assert scope["configured_zoos"] == 73
    assert scope["expanded_zoos"] == 73
    assert scope["roster_version"] == "Phase0 v0.1"
    assert scope["roster_sha256"] == roster_sha
    assert scope["roster_source"] == "data/zoofan_phase0_zoo_list_v0_1.md"
    assert scope["authoritative_roster"]["sha256"] == roster_sha
    assert scope["configured_membership_count"] == 73
    assert scope["expanded_membership_count"] == 73
    assert not scope["limitation"]

    summary = json.loads(report["run-summary.json"].read_text(encoding="utf-8"))
    assert summary["scope"] == scope
    markdown = report["run-summary.md"].read_text(encoding="utf-8")
    assert "Authoritative Phase0 v0.1 roster" in markdown
    assert "expanded zoos=73" in markdown
    assert roster_sha in markdown
    rows = list(csv.DictReader(report["zoo-status.csv"].read_text(encoding="utf-8").splitlines()))
    assert rows and rows[0]["expanded_roster_available"] == "true"
    assert rows[0]["expanded_zoo_count"] == "73"
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "authoritative_expanded_roster" in document
    assert "expanded roster available (" in document
    assert roster_sha in document


def test_static_acceptance_expanded_report_executes_and_supports_interactions(tmp_path):
    """Exercise the generated expanded-roster page in a real DOM runtime."""

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    roster_sha = "b" * 64
    provenance = [{
        "source": "data/zoofan_phase0_zoo_list_v0_1.md",
        "version": "Phase0 v0.1",
        "sha256": roster_sha,
        "expanded_count": 73,
    }]
    config = _scope_config(count=73, provenance=provenance)
    config["zoos"][0].update(
        country_code="NL",
        groups=["phase0-special"],
        website_url=None,
        source_status="not_found",
    )
    db_path = tmp_path / "acceptance.db"
    timestamp = "2025-01-01T00:00:00Z"
    with SQLiteStorage(db_path) as storage:
        for raw_zoo in config["zoos"]:
            storage.upsert_zoo({key: value for key, value in raw_zoo.items() if key != "sources"})
        run = storage.start_crawl_run(CrawlRun(batch_id="batch", started_at=timestamp))
        for raw_zoo in config["zoos"]:
            storage.upsert_zoo_run_result(
                CrawlZooResult(
                    crawl_run_id=run.id,
                    zoo_id=raw_zoo["id"],
                    zoo_slug=raw_zoo["slug"],
                    zoo_name=raw_zoo["name"],
                    status="completed",
                    source_status=raw_zoo.get("source_status"),
                    started_at=timestamp,
                    finished_at=timestamp,
                )
            )
        storage.finish_crawl_run(run.id, finished_at="2025-01-01T00:01:00Z")
    report = build_reports(
        config=config,
        db_path=db_path,
        output_dir=tmp_path / "reports",
        now=timestamp,
        git_commit="browser-test",
    )
    report_path = report["acceptance/index.html"]
    assert report.projection["acceptance"]["status"] == "PASS"
    console_errors = []
    page_errors = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise
            try:
                page = browser.new_page()
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.goto(report_path.as_uri(), wait_until="load")
                rows = page.locator("#zoo-table tbody tr")
                assert rows.count() == 73
                assert "Acceptance: PASS" in page.locator("#alerts").inner_text()
                page.locator("#name-filter").fill("Scope Zoo 000")
                assert page.locator("#zoo-table tbody tr").count() == 1
                page.locator("#name-filter").fill("")
                page.locator("#status-filter").select_option("SOURCE_NOT_FOUND")
                assert page.locator("#zoo-table tbody tr").count() == 1
                page.locator("#status-filter").select_option("")
                page.locator("#country-filter").select_option("NL")
                assert page.locator("#zoo-table tbody tr").count() == 1
                page.locator("#country-filter").select_option("")
                page.locator("#zoo-table tbody tr").first.click()
                assert "Scope Zoo 000" in page.locator("#details").inner_text()
            finally:
                browser.close()
    except ImportError:
        pytest.skip("Playwright is unavailable")
    assert console_errors == []
    assert page_errors == []
    assert "raw_html" not in report_path.read_text(encoding="utf-8").lower()


def test_scope_rejects_partial_membership_and_duplicate_identity(tmp_path):
    phase0 = [{
        "source": "data/zoofan_phase0_zoo_list_v0_1.md",
        "version": "Phase0 v0.1",
        "sha256": "c" * 64,
        "expanded_count": 73,
    }]
    partial = build_projection(
        config=_scope_config(count=72, provenance=phase0),
        db_path=tmp_path / "partial.db",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )["scope"]
    assert partial["kind"] == "configured_registry"
    assert partial["expanded_roster_available"] is False
    assert partial["expanded_roster_status"] == "invalid"
    assert partial["configured_membership_count"] == 72
    assert partial["expanded_count"] is None

    duplicate_config = _scope_config(count=73, provenance=phase0)
    duplicate_config["zoos"][-1]["id"] = duplicate_config["zoos"][0]["id"]
    duplicate_config["zoos"][-1]["slug"] = duplicate_config["zoos"][0]["slug"]
    duplicate = build_projection(
        config=duplicate_config,
        db_path=tmp_path / "duplicate.db",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )["scope"]
    assert duplicate["kind"] == "configured_registry"
    assert duplicate["expanded_roster_available"] is False
    assert duplicate["expanded_roster_status"] == "invalid"


def _phase0_gap_config(*, enabled: bool) -> dict:
    provenance = [{
        "source": "data/zoofan_phase0_zoo_list_v0_1.md",
        "version": "Phase0 v0.1",
        "sha256": "d" * 64,
        "expanded_count": 2,
    }]
    return {
        "zoos": [
            {
                "id": "phase0-gap-disabled",
                "slug": "phase0-gap-disabled",
                "name": "Phase0 Gap Disabled",
                "website_url": None,
                "country_code": "NL",
                "language": None,
                "city": None,
                "enabled": False,
                "groups": ["regional_lab"],
                "source_status": "not_found",
                "list_provenance": provenance,
                "sources": [],
            },
            {
                "id": "phase0-gap-enabled",
                "slug": "phase0-gap-enabled",
                "name": "Phase0 Gap Enabled",
                "website_url": None,
                "country_code": "NL",
                "language": None,
                "city": None,
                "enabled": enabled,
                "groups": ["regional_lab"],
                "source_status": "not_found",
                "list_provenance": provenance,
                "sources": [],
            },
        ]
    }


def test_phase0_no_site_gap_is_nonblocking_without_run_but_missing_enabled_result_blocks(tmp_path):
    config = _phase0_gap_config(enabled=True)
    report = build_reports(
        config=config,
        db_path=tmp_path / "no-run.db",
        output_dir=tmp_path / "no-run-reports",
        now="2025-01-01T00:00:00Z",
        git_commit="gap-test",
    )
    projection = report.projection
    assert projection["scope"]["expanded_roster_available"] is True
    assert projection["coverage"]["configured_zoos"] == 2
    assert projection["coverage"]["enabled_zoos"] == 1
    assert projection["scope"]["configured_zoo_count"] == 2
    assert projection["scope"]["enabled_zoo_count"] == 1
    assert len(projection["zoos"]) == 2
    for zoo in projection["zoos"]:
        assert zoo["completion_classification"] == "SOURCE_NOT_FOUND"
        assert zoo["completion_category"] == "source_not_found"
        assert zoo["website_url"] is None
        assert zoo["city"] is None
        assert zoo["language"] is None

    rows = list(csv.DictReader(report["zoo-status.csv"].read_text(encoding="utf-8").splitlines()))
    assert {row["zoo_id"] for row in rows} == {"phase0-gap-disabled", "phase0-gap-enabled"}
    assert {row["enabled"] for row in rows} == {"false", "true"}
    assert {row["configured_zoo_count"] for row in rows} == {"2"}
    assert {row["enabled_zoo_count"] for row in rows} == {"1"}
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "Phase0 Gap Disabled" in document
    assert "Phase0 Gap Enabled" in document
    assert "Configured zoos" in document and "Enabled zoos" in document

    db_path = tmp_path / "run.db"
    with SQLiteStorage(db_path) as storage:
        run = storage.start_crawl_run(CrawlRun(batch_id="gap-run", started_at="2025-01-01T00:00:00Z"))
        assert run.id is not None
        storage.finish_crawl_run(run.id, finished_at="2025-01-01T00:01:00Z")
    run_projection = build_projection(
        config=config,
        db_path=db_path,
        now="2025-01-01T00:00:00Z",
        git_commit="gap-test",
    )
    disabled = next(zoo for zoo in run_projection["zoos"] if zoo["id"] == "phase0-gap-disabled")
    enabled_gap = next(zoo for zoo in run_projection["zoos"] if zoo["id"] == "phase0-gap-enabled")
    assert disabled["completion_classification"] == "SOURCE_NOT_FOUND"
    assert enabled_gap["completion_classification"] == "FAILED"
    assert enabled_gap["completion_category"] == "failed"
    assert enabled_gap["result_issue"] == "missing_result"
    assert run_projection["acceptance"]["status"] == "FAIL"


def test_scope_keeps_three_zoo_legacy_registry_and_rejects_malformed_or_mixed_provenance(tmp_path):
    legacy = [{
        "source": "existing_registry",
        "scope": "existing_registry_only",
        "note": "The current registry is not an expanded roster.",
    }]
    legacy_projection = build_projection(
        config=_scope_config(count=3, provenance=legacy, groups=False),
        db_path=tmp_path / "legacy.db",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )
    legacy_scope = legacy_projection["scope"]
    assert legacy_scope["kind"] == "configured_registry"
    assert legacy_scope["configured_zoos"] == 3
    assert legacy_scope["expanded_roster_available"] is False
    assert legacy_scope["expanded_roster_status"] == "not_provided"
    assert "no authoritative expanded zoo roster" in legacy_scope["limitation"]

    phase0 = {
        "source": "data/zoofan_phase0_zoo_list_v0_1.md",
        "version": "Phase0 v0.1",
        "sha256": "b" * 64,
        "expanded_count": 73,
    }
    mixed = [phase0]
    mixed_config = _scope_config(count=3, provenance=mixed)
    mixed_config["zoos"][1]["list_provenance"] = legacy
    mixed_projection = build_projection(
        config=mixed_config,
        db_path=tmp_path / "mixed.db",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )
    mixed_scope = mixed_projection["scope"]
    assert mixed_scope["kind"] == "configured_registry"
    assert mixed_scope["expanded_roster_available"] is False
    assert mixed_scope["expanded_roster_status"] == "invalid"

    malformed = dict(phase0)
    malformed["sha256"] = "not-a-sha256"
    malformed_projection = build_projection(
        config=_scope_config(count=3, provenance=[malformed]),
        db_path=tmp_path / "malformed.db",
        now="2025-01-01T00:00:00Z",
        git_commit="scope-test",
    )
    malformed_scope = malformed_projection["scope"]
    assert malformed_scope["kind"] == "configured_registry"
    assert malformed_scope["expanded_roster_available"] is False
    assert malformed_scope["expanded_roster_status"] == "invalid"


def test_duplicate_result_rows_do_not_double_count_and_associations_are_explicit(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path, articles=1)
    connection = sqlite3.connect(db_path)
    latest_run = connection.execute("SELECT id FROM crawl_runs ORDER BY started_at DESC LIMIT 1").fetchone()[0]
    existing = connection.execute("SELECT * FROM crawl_zoo_results WHERE crawl_run_id=? LIMIT 1", (latest_run,)).fetchone()
    columns = [item[1] for item in connection.execute("PRAGMA table_info(crawl_zoo_results)").fetchall()]
    duplicate = dict(zip(columns, existing))
    duplicate["id"] = "duplicate-result"
    connection.execute("DROP INDEX ux_crawl_zoo_results_run_zoo")
    connection.execute(
        "INSERT INTO crawl_zoo_results ({}) VALUES ({})".format(
            ",".join(columns), ",".join("?" for _ in columns)
        ),
        [duplicate[column] for column in columns],
    )
    connection.execute("INSERT INTO article_discoveries (article_id, source_id) VALUES ('same-date-0', 'alpha-archive')")
    connection.commit()
    connection.close()
    projection = build_projection(config_path=config_path, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    coverage = projection["coverage"]
    assert coverage["raw_zoo_results"] == 2
    assert coverage["zoo_results"] == 1
    assert coverage["duplicate_zoo_results"] == 1
    assert coverage["latest_run_duplicate_zoo_results"] == ["alpha"]
    assert coverage["latest_run_missing_zoo_results"] == ["empty"]
    assert projection["completion_classifications"]["FAILED"] == 2
    alpha = next(zoo for zoo in projection["zoos"] if zoo["id"] == "alpha")
    assert alpha["counts"]["unique_articles"] == 1
    assert alpha["counts"]["source_article_associations"] == 2
    assert projection["adapter_stats"]["rss"]["article_association_count"] == 1
    assert projection["adapter_stats"]["archive"]["article_association_count"] == 1
    assert projection["adapter_stats"]["rss"]["unique_article_count"] == 1


def test_article_evidence_fields_project_across_formats_without_credentials_or_raw_html(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _two_run_result_db(tmp_path, config_path, articles=1)
    connection = sqlite3.connect(db_path)
    connection.execute(
        "UPDATE articles SET source_url_raw=?, raw_html=? WHERE id='same-date-0'",
        ("https://report-user:report-secret@alpha.example/story/?utm_source=feed", "<script>private</script>"),
    )
    connection.execute(
        "UPDATE article_discoveries SET discovered_url_raw=? WHERE article_id='same-date-0'",
        ("https://report-user:report-secret@alpha.example/story/?utm_medium=archive",),
    )
    connection.commit()
    connection.close()

    projection = build_projection(
        config_path=config_path,
        db_path=db_path,
        now="2025-01-02T00:00:00Z",
        git_commit="x",
    )
    article = projection["articles"][0]
    for field in ("first_discovered_at", "last_discovered_at", "created_at", "storage_updated_at"):
        assert article[field] and article[field].endswith("+00:00")
    assert article["source_url_raw"] == "https://alpha.example/story/?utm_source=feed"
    assert article["discoveries"][0]["discovered_url_raw"] == "https://alpha.example/story/?utm_medium=archive"
    assert "report-secret" not in json.dumps(projection)
    assert "raw_html" not in json.dumps(projection)
    report = build_reports(
        config_path=config_path,
        db_path=db_path,
        output_dir=tmp_path / "reports",
        now="2025-01-02T00:00:00Z",
        git_commit="x",
    )
    summary = json.loads(report["run-summary.json"].read_text(encoding="utf-8"))
    articles = json.loads(report["articles.json"].read_text(encoding="utf-8"))
    assert summary["articles"][0] == articles["articles"][0] == article
    assert "report-secret" not in report["run-summary.md"].read_text(encoding="utf-8")
    assert "report-secret" not in report["acceptance/index.html"].read_text(encoding="utf-8")
    assert "<script>private</script>" not in report["acceptance/index.html"].read_text(encoding="utf-8")


def test_report_adapter_taxonomy_separates_known_support_and_explicit_unsupported(tmp_path):
    config = {
        "zoos": [
            {
                "id": "json",
                "slug": "json",
                "name": "JSON Zoo",
                "website_url": "https://json.example/",
                "sources": [{"id": "json-source", "kind": "json_api", "url": "https://json.example/api"}],
            },
            {
                "id": "custom",
                "slug": "custom",
                "name": "Custom Zoo",
                "website_url": "https://custom.example/",
                "sources": [{
                    "id": "custom-source",
                    "kind": "custom_adapter",
                    "adapter": "fixture_adapter",
                    "url": "https://custom.example/news",
                }],
            },
            {
                "id": "unsupported",
                "slug": "unsupported",
                "name": "Unsupported Zoo",
                "website_url": "https://unsupported.example/",
                "sources": [{"id": "unsupported-source", "kind": "unsupported", "url": ""}],
            },
        ]
    }
    db_path = tmp_path / "adapter-contract.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE crawl_runs (id TEXT, status TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE crawl_zoo_results (
            id TEXT, crawl_run_id TEXT, zoo_id TEXT, status TEXT, source_status TEXT,
            error_category TEXT, discovered INTEGER, parsed INTEGER, inserted INTEGER,
            failed INTEGER, error_summary TEXT, http_status INTEGER, duration_ms INTEGER
        );
        """
    )
    connection.execute("INSERT INTO crawl_runs VALUES ('run-1', 'completed', '2025-01-01T00:00:00Z', '2025-01-01T00:00:03Z')")
    connection.executemany(
        "INSERT INTO crawl_zoo_results VALUES (?, 'run-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("json-result", "json", "success", "success", None, 2, 2, 1, 0, None, 200, 10),
            ("custom-result", "custom", "success", "success", None, 3, 2, 1, 0, None, 200, 11),
            ("unsupported-result", "unsupported", "failed", "unsupported", "unsupported", 0, 0, 0, 1, "no executable adapter", None, 1),
        ],
    )
    connection.commit()
    connection.close()

    projection = build_projection(config=config, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    by_id = {zoo["id"]: zoo for zoo in projection["zoos"]}
    assert set(KNOWN_ADAPTERS) >= {"json_api", "custom_adapter", "unsupported"}
    assert "unsupported" not in SUPPORTED_ADAPTERS
    assert by_id["json"]["adapter_kind"] == "json_api"
    assert by_id["json"]["completion_classification"] == "SUPPORTED"
    assert by_id["custom"]["adapter"] == "fixture_adapter"
    assert by_id["custom"]["adapter_kind"] == "custom_adapter"
    assert by_id["custom"]["completion_classification"] == "SUPPORTED"
    assert by_id["unsupported"]["adapter_kind"] == "unsupported"
    assert by_id["unsupported"]["completion_classification"] == "UNSUPPORTED"
    assert projection["categories"]["supported"] == list(SUPPORTED_ADAPTERS)
    assert projection["categories"]["known"] == list(KNOWN_ADAPTERS)
    criterion = next(item for item in projection["acceptance"]["criteria"] if item["id"] == "supported_adapters")
    assert criterion["passed"] is True


def test_report_per_zoo_evidence_is_consistent_in_json_csv_markdown_and_html(tmp_path):
    config = {
        "zoos": [
            {
                "id": "json",
                "slug": "json",
                "name": "JSON Zoo",
                "country_code": "DE",
                "region": "North",
                "groups": ["pilot"],
                "list_provenance": [{"source": "registry.csv", "version": "2025-01"}],
                "website_url": "https://json.example/",
                "sources": [{"id": "json-source", "kind": "json_api", "url": "https://json.example/api"}],
            },
            {
                "id": "failed",
                "slug": "failed",
                "name": "Failed Zoo",
                "country_code": "NL",
                "region": "West",
                "groups": ["watch"],
                "website_url": "https://failed.example/",
                "sources": [{"id": "failed-source", "kind": "rss", "url": "https://failed.example/feed.xml"}],
            },
        ]
    }
    db_path = tmp_path / "formats.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE crawl_runs (id TEXT, status TEXT, started_at TEXT, finished_at TEXT);
        CREATE TABLE crawl_zoo_results (
            id TEXT, crawl_run_id TEXT, zoo_id TEXT, status TEXT, source_status TEXT,
            error_category TEXT, discovered INTEGER, parsed INTEGER, inserted INTEGER,
            failed INTEGER, error_summary TEXT, http_status INTEGER, duration_ms INTEGER
        );
        """
    )
    connection.execute("INSERT INTO crawl_runs VALUES ('run-1', 'completed', '2025-01-01T00:00:00Z', '2025-01-01T00:00:03Z')")
    connection.executemany(
        "INSERT INTO crawl_zoo_results VALUES (?, 'run-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("json-result", "json", "success", "success", None, 5, 4, 3, 0, None, 200, 120),
            ("failed-result", "failed", "failed", "failed", "http_error", 2, 1, 0, 2, "HTTP 503", 503, 80),
        ],
    )
    connection.commit()
    connection.close()

    report = build_reports(config=config, db_path=db_path, output_dir=tmp_path / "reports", now="2025-01-02T00:00:00Z", git_commit="x")
    projection_by_id = {zoo["id"]: zoo for zoo in report.projection["zoos"]}
    rows = {row["zoo_id"]: row for row in csv.DictReader(report["zoo-status.csv"].read_text(encoding="utf-8").splitlines())}
    for zoo_id, zoo in projection_by_id.items():
        row = rows[zoo_id]
        counts = zoo["counts"]
        assert row["completion_classification"] == zoo["completion_classification"]
        assert row["status"] == zoo["status"]
        assert int(row["discovered"]) == counts["discovered"]
        assert int(row["parsed"]) == counts["parsed"]
        assert int(row["inserted"]) == counts["inserted"]
        assert int(row["failed"]) == counts["failed"]
        assert row["adapter_kind"] == zoo["adapter_kind"]
        assert row["news_source_url"] == zoo["news_source_url"]
        assert row["error_summary"] == (zoo["error_summary"] or "")
        assert row["latest_news_date"] == (zoo["latest_news_date"] or "")
    markdown = report["run-summary.md"].read_text(encoding="utf-8")
    for zoo in projection_by_id.values():
        assert zoo["name"] in markdown
        assert zoo["completion_classification"] in markdown
        assert zoo["adapter_kind"] in markdown
        assert (zoo["error_summary"] or "HTTP 503" if zoo["id"] == "failed" else zoo["name"]) in markdown
    document = report["acceptance/index.html"].read_text(encoding="utf-8")
    for zoo in projection_by_id.values():
        assert zoo["id"] in document
        assert zoo["completion_classification"] in document
        assert zoo["adapter_kind"] in document
    assert projection_by_id["json"]["list_provenance"] == [{"source": "registry.csv", "version": "2025-01"}]
    assert "List provenance" in document
    for label in ("Region", "Official site", "News source URL", "Adapter", "Run status", "HTTP", "Discovered", "Parsed", "Inserted", "Failed", "Latest errors", "Latest news date", "Duration", "Error summary"):
        assert label in document


def test_static_acceptance_responsive_contract_keeps_overflow_inside_table_and_wraps_alerts(tmp_path):
    config_path = _config_file(tmp_path)
    db_path = _populated_db(tmp_path, config_path)
    report = build_reports(
        config_path=config_path,
        db_path=db_path,
        output_dir=tmp_path / "reports",
        now="2025-01-01T00:00:00Z",
        git_commit="a" * 64,
    )
    document = report["acceptance/index.html"].read_text(encoding="utf-8")

    # The wide operational table remains fully available through its own
    # scroll region; the document itself must not need horizontal scrolling.
    assert ".table-wrap { overflow-x:auto;" in document
    assert ".banner p { margin:3px 0; overflow-wrap:anywhere; word-break:break-word; }" in document
    required_headers = (
        "Name", "Country", "Region", "Groups", "Official site", "News source URL",
        "Adapter", "Completion", "Run status", "HTTP", "Discovered", "Parsed",
        "Inserted", "Failed", "Latest errors", "Latest news date", "Duration",
        "Error summary",
    )
    assert document.count("<th scope=\"col\">") == len(required_headers)
    for header in required_headers:
        assert f">{header}</th>" in document
    assert 'id="zoo-table"' in document
    assert 'id="filters"' in document


def test_reporting_keeps_exact_official_host_and_redacts_sensitive_raw_url_queries(tmp_path):
    config = {
        "zoos": [{
            "id": "exact",
            "slug": "exact",
            "name": "Exact Host Zoo",
            "website_url": "https://exact.example/",
            "sources": [{"id": "exact-source", "kind": "rss", "url": "https://exact.example/feed.xml"}],
        }]
    }
    db_path = tmp_path / "privacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE articles (id TEXT, zoo_id TEXT, canonical_url TEXT, title TEXT, content TEXT, published_at TEXT, last_fetched_at TEXT, source_url_raw TEXT);
        CREATE TABLE article_discoveries (article_id TEXT, source_id TEXT, discovered_url TEXT, discovered_url_raw TEXT);
        """
    )
    connection.execute(
        "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "article-1",
            "exact",
            "https://exact.example/story",
            "Story",
            "Body",
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:00:00Z",
            "https://report-user:report-pass@exact.example/story?utm_source=feed&token=TOPSECRET&password=PASSSECRET&api_key=APISECRET&access_token=ACCESSSECRET&auth=AUTHSECRET&signature=SIGSECRET",
        ),
    )
    connection.execute(
        "INSERT INTO article_discoveries VALUES (?, ?, ?, ?)",
        (
            "article-1",
            "exact-source",
            "https://exact.example/story?utm_medium=archive",
            "https://report-user:report-pass@exact.example/story?utm_medium=archive&sig=SIGSECRET&secret=SECRET",
        ),
    )
    connection.commit()
    connection.close()

    projection = build_projection(config=config, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    article = projection["articles"][0]
    assert article["source_url_raw"] == "https://exact.example/story?utm_source=feed&token=REDACTED&password=REDACTED&api_key=REDACTED&access_token=REDACTED&auth=REDACTED&signature=REDACTED"
    assert article["discoveries"][0]["discovered_url_raw"] == "https://exact.example/story?utm_medium=archive&sig=REDACTED&secret=REDACTED"
    assert "TOPSECRET" not in json.dumps(projection)
    assert "SIGSECRET" not in json.dumps(projection)
    assert "utm_source=feed" in json.dumps(projection)

    # ``www`` is not an implicit alias of the configured exact host.
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE articles SET canonical_url=?", ("https://www.exact.example/story",))
    connection.commit()
    connection.close()
    changed = build_projection(config=config, db_path=db_path, now="2025-01-02T00:00:00Z", git_commit="x")
    assert "non_official_host" in changed["articles"][0]["warning_codes"]
