import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from zoofan.models import Article, ArticleDiscovery, CrawlRun, CrawlRunStat, CrawlZooResult, Source, Zoo
from zoofan.storage import SQLiteStorage


def test_article_upsert_deduplicates_url_and_records_multiple_sources():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(slug="zoo", name="Zoo"))
        feed = storage.upsert_source(Source(zoo_id=zoo.id, url="https://example.org/feed", kind="rss"))
        archive = storage.upsert_source(Source(zoo_id=zoo.id, url="https://example.org/archive", kind="archive"))
        first = storage.upsert_article(Article(url="https://example.org/story/", title="Story"), source_id=feed.id)
        second = storage.upsert_article(Article(url="https://EXAMPLE.org/story?utm_source=x", title="Full story"), source_id=archive.id)
        assert first.id == second.id
        assert len(storage.list_articles()) == 1
        assert len(storage.list_discoveries(article_id=first.id)) == 2


def test_crawl_run_and_per_source_stat_persist_error_fields():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(slug="zoo", name="Zoo"))
        source = storage.upsert_source(Source(zoo_id=zoo.id, url="https://example.org/feed"))
        run = storage.start_crawl_run(CrawlRun(batch_id="batch-1"))
        stat = storage.record_run_stat(
            CrawlRunStat(crawl_run_id=run.id, zoo_id=zoo.id, source_id=source.id, status="error", error_count=1, errors=["timeout"], error="timeout")
        )
        loaded = storage.get_run_stat(stat.id)
        assert loaded and loaded.errors == ["timeout"] and loaded.error == "timeout"
        storage.update_source_status(source.id, status="error", success=False, error="timeout", http_status=504)
        current = storage.get_source(source.id)
        assert current and current.last_error == "timeout" and current.last_http_status == 504


def test_crawl_run_metadata_update_replaces_by_default_and_merges_explicitly():
    with SQLiteStorage() as storage:
        run = storage.start_crawl_run(
            CrawlRun(id="metadata-run", metadata={"selection": "all", "old": True})
        )
        replaced = storage.update_crawl_run_metadata(run.id, {"final": True})
        assert replaced and replaced.metadata == {"final": True}
        assert not storage.connection.in_transaction

        merged = storage.update_crawl_run_metadata(
            run.id, {"status": "completed"}, merge=True
        )
        assert merged and merged.metadata == {"final": True, "status": "completed"}


def test_crawl_run_metadata_update_unknown_run_is_a_noop():
    with SQLiteStorage() as storage:
        assert storage.update_crawl_run_metadata("missing-run", {"final": True}) is None
        assert storage.list_leases() == []


def test_crawl_run_metadata_update_rejects_non_json_objects_before_writing():
    with SQLiteStorage() as storage:
        run = storage.start_crawl_run(CrawlRun(id="metadata-invalid", metadata={"old": True}))
        invalid_values = (
            (None, TypeError),
            ([], TypeError),
            ("metadata", TypeError),
            ({1: "non-string key"}, TypeError),
            ({"object": object()}, ValueError),
            ({"nan": float("nan")}, ValueError),
        )
        for value, error_type in invalid_values:
            with pytest.raises(error_type):
                storage.update_crawl_run_metadata(run.id, value)
        with pytest.raises(TypeError, match="merge"):
            storage.update_crawl_run_metadata(run.id, {"new": True}, merge=1)
        assert storage.get_crawl_run(run.id).metadata == {"old": True}


def test_discovery_null_key_is_idempotent_and_source_url_is_normalized():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(slug="zoo", name="Zoo"))
        source = storage.upsert_source(Source(zoo_id=zoo.id, url="https://EXAMPLE.org/feed/?utm_campaign=x"))
        same_source = storage.upsert_source(Source(zoo_id=zoo.id, url="https://example.org/feed/"))
        assert source.id == same_source.id
        article = storage.upsert_article(Article(url="https://example.org/story"))
        first = storage.record_discovery(ArticleDiscovery(article_id=article.id, source_id=source.id))
        second = storage.record_discovery(ArticleDiscovery(article_id=article.id, source_id=source.id))
        assert first.id == second.id
        assert len(storage.list_discoveries(article_id=article.id)) == 1


def test_status_update_with_success_none_preserves_previous_success():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(slug="zoo", name="Zoo"))
        source = storage.upsert_source(Source(zoo_id=zoo.id, url="https://example.org/feed"))
        storage.update_source_status(source.id, status="ok", success=True)
        storage.update_source_status(source.id, status="checking", success=None)
        assert storage.get_source(source.id).success is True


def test_legacy_schema_is_migrated_idempotently():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, html_hash TEXT, raw_html TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT);
        INSERT INTO zoos VALUES('z', 'zoo', 'Zoo');
        INSERT INTO sources VALUES('s', 'z', 'https://example.org/feed/');
        INSERT INTO articles VALUES('a', 'https://example.org/story/', 'https://example.org/story/', 'raw-hash', '<p>x</p>');
        INSERT INTO article_discoveries VALUES('d', 'a', 's', NULL);
        """
    )
    storage = SQLiteStorage(connection=connection)
    assert storage.get_source("s").normalized_url == "https://example.org/feed"
    migrated = storage.get_article("a")
    assert migrated.html_hash == "raw-hash"
    assert migrated.content_hash is None
    assert migrated.language is None and migrated.http_status is None
    assert len(storage.list_discoveries(article_id="a")) == 1
    storage.create_schema()
    assert connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == str(SQLiteStorage.SCHEMA_VERSION)
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_zoo_registry_fields_and_article_compatibility_aliases_round_trip():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(
            Zoo(
                id="z",
                slug="z",
                name="Zoo",
                groups=["regional", "news"],
                region="west",
                city="Berlin",
                source_status="configured",
                list_provenance=[{"source": "registry.csv"}],
            )
        )
        loaded_zoo = storage.get_zoo(zoo.id)
        assert loaded_zoo.groups == ("regional", "news")
        assert loaded_zoo.region == "west" and loaded_zoo.city == "Berlin"
        assert loaded_zoo.source_status == "configured"
        assert loaded_zoo.list_provenance == [{"source": "registry.csv"}]

        article = storage.upsert_article(
            Article(
                url="https://example.org/article",
                title="Evidence",
                published_at_raw="not-a-date",
                content="plain text",
                content_hash="parsed-content",
                content_html="<p>plain text</p>",
                raw_html="<html><p>plain text</p></html>",
            )
        )
        assert article.article_id == article.id
        assert article.fetched_at == article.last_fetched_at
        assert article.content_text == "plain text"
        assert article.published_at_raw == "not-a-date"
        assert article.content_html != article.raw_html
        assert article.content_identity_key
        assert storage.connection.execute(
            "SELECT content_identity_key FROM articles WHERE id=?", (article.id,)
        ).fetchone()[0] == article.content_identity_key


def test_zoo_authoritative_empty_registry_lists_clear_stale_values_idempotently():
    with SQLiteStorage() as storage:
        stored = storage.upsert_zoo(
            Zoo(
                id="z",
                slug="z",
                name="Zoo",
                groups=["old-group"],
                list_provenance=[{"source": "legacy"}],
            )
        )
        assert storage.get_zoo(stored.id).groups == ("old-group",)
        assert storage.get_zoo(stored.id).list_provenance == [{"source": "legacy"}]

        cleared = storage.upsert_zoo(
            Zoo(id="z", slug="z", name="Zoo", groups=[], list_provenance=[])
        )
        assert cleared.groups == ()
        assert cleared.list_provenance == []
        loaded = storage.get_zoo("z")
        assert loaded.groups == ()
        assert loaded.list_provenance == []

        repeated_input = Zoo(
            id="z", slug="z", name="Zoo", groups=["transient"], list_provenance=["transient"]
        )
        repeated_input.groups = []
        repeated_input.list_provenance = []
        repeated = storage.upsert_zoo(repeated_input)
        assert repeated.groups == () and repeated.list_provenance == []
        loaded_again = storage.get_zoo("z")
        assert loaded_again.groups == ()
        assert loaded_again.list_provenance == []


def test_legacy_zoo_registry_values_migrate_then_explicit_empty_clears_idempotently():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(
            id TEXT PRIMARY KEY, slug TEXT, name TEXT,
            groups TEXT, list_provenance TEXT
        );
        INSERT INTO zoos VALUES(
            'z', 'z', 'Zoo', '["legacy"]', '[{"source":"legacy"}]'
        );
        """
    )
    storage = SQLiteStorage(connection=connection)
    migrated = storage.get_zoo("z")
    assert migrated.groups == ("legacy",)
    assert migrated.list_provenance == [{"source": "legacy"}]

    storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo", groups=[], list_provenance=[]))
    cleared = storage.get_zoo("z")
    assert cleared.groups == () and cleared.list_provenance == []
    storage.create_schema()
    repeated = storage.get_zoo("z")
    assert repeated.groups == () and repeated.list_provenance == []


def test_content_identity_partial_unique_constraint_allows_hash_collisions_but_blocks_same_title():
    with SQLiteStorage() as storage:
        first = storage.upsert_article(
            Article(url="https://example.org/one", title="First", content_hash="card")
        )
        second = storage.upsert_article(
            Article(url="https://example.org/two", title="Second", content_hash="card")
        )
        assert first.content_identity_key != second.content_identity_key
        with pytest.raises(sqlite3.IntegrityError, match="content_identity_key"):
            storage.connection.execute(
                "INSERT INTO articles(id,title,content_hash,content_identity_key) VALUES(?,?,?,?)",
                ("bypass", "First", "card", first.content_identity_key),
            )


def test_zoo_title_identity_merges_within_zoo_but_not_across_zoos():
    with SQLiteStorage() as storage:
        first_zoo = storage.upsert_zoo(Zoo(id="z1", slug="z1", name="One"))
        second_zoo = storage.upsert_zoo(Zoo(id="z2", slug="z2", name="Two"))
        same_zoo_first = storage.upsert_article(
            Article(url="https://example.org/one", title="  Shared   title "),
            zoo_id=first_zoo.id,
        )
        same_zoo_second = storage.upsert_article(
            Article(url="https://example.org/two", title="shared title"),
            zoo_id=first_zoo.id,
        )
        other_zoo = storage.upsert_article(
            Article(url="https://example.org/three", title="SHARED TITLE"),
            zoo_id=second_zoo.id,
        )
        assert same_zoo_first.id == same_zoo_second.id
        assert other_zoo.id != same_zoo_first.id
        assert len(storage.list_articles()) == 2
        identities = storage.list_article_zoo_identities()
        assert {(item["zoo_id"], item["title_key"]) for item in identities} == {
            ("z1", "shared title"),
            ("z2", "shared title"),
        }


def test_zoo_run_result_is_unique_per_run_and_zoo_and_updates():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        run = storage.start_crawl_run(CrawlRun(id="run"))
        initial = storage.upsert_zoo_run_result(
            CrawlZooResult(
                run_id=run.id,
                zoo_id=zoo.id,
                status="success_no_items",
                discovered=0,
                started_at="2026-01-01T00:00:00+00:00",
                finished_at="2026-01-01T00:00:00.010000+00:00",
            )
        )
        updated = storage.upsert_zoo_run_result(
            CrawlZooResult(
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                status="success",
                discovered=3,
                parsed=2,
                inserted=1,
                updated=1,
                failed=0,
                duplicate_filtered=1,
                source_url="https://example.org/feed?utm_source=test",
            )
        )
        assert updated.id == initial.id
        assert updated.status == "success" and updated.discovered == 3
        assert updated.source_url == "https://example.org/feed"
        assert len(storage.get_zoo_run_results(run.id)) == 1
        assert storage.connection.execute(
            "SELECT COUNT(*) FROM crawl_zoo_results WHERE crawl_run_id=? AND zoo_id=?",
            (run.id, zoo.id),
        ).fetchone()[0] == 1


def test_article_upsert_outcome_distinguishes_identical_from_evidence_update():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        first = storage.upsert_article_with_outcome(
            Article(url="https://example.org/a", title="A", content="old", raw_html="<p>old</p>"),
            zoo_id=zoo.id,
            source_id=source.id,
            discovered_at="2026-01-02T00:00:00+00:00",
        )
        identical = storage.upsert_article_with_outcome(
            Article(url="https://example.org/a", title="A", content="old", raw_html="<p>old</p>"),
            zoo_id=zoo.id,
            source_id=source.id,
            discovered_at="2026-01-01T00:00:00+00:00",
        )
        changed = storage.upsert_article_with_outcome(
            Article(url="https://example.org/a", title="A", content="new", raw_html="<p>new</p>"),
            zoo_id=zoo.id,
            source_id=source.id,
            discovered_at="2026-01-03T00:00:00+00:00",
        )
        assert first.created is True and first.updated is False and first.action == "created"
        assert identical.created is False and identical.updated is False and identical.action == "unchanged"
        assert changed.created is False and changed.updated is True and changed.action == "updated"
        discovery = storage.list_discoveries(article_id=first.article.id)[0]
        assert discovery.discovered_at.isoformat() == "2026-01-01T00:00:00+00:00"
        assert discovery.last_discovered_at.isoformat() == "2026-01-03T00:00:00+00:00"


def test_raw_response_recheck_refreshes_capture_without_business_update():
    with SQLiteStorage() as storage:
        first = storage.upsert_article_with_outcome(
            Article(
                url="https://example.org/a",
                title="A",
                published_at="2026-01-01T00:00:00+00:00",
                content="same body",
                content_html="<p>same body</p>",
                content_hash="parsed-body-v1",
                html_hash="raw-hash-v1",
                raw_html="<html><input value='csrf-one'><p>same body</p></html>",
                metadata={"section": "news"},
                last_fetched_at="2026-01-01T00:00:00+00:00",
            )
        )
        assert first.created is True and first.updated is False

        # Keep the assertion independent of wall-clock resolution.  A raw
        # response-only recheck must not move this business timestamp.
        business_timestamp = "2026-01-01T00:00:00+00:00"
        storage.connection.execute(
            "UPDATE articles SET updated_at=? WHERE id=?",
            (business_timestamp, first.article.id),
        )
        raw_recheck = storage.upsert_article_with_outcome(
            Article(
                url="https://example.org/a",
                title="A",
                published_at="2026-01-01T00:00:00+00:00",
                content="same body",
                content_html="<p>same body</p>",
                content_hash="parsed-body-v1",
                html_hash="raw-hash-v2",
                raw_html="<html><input value='csrf-two'><p>same body</p></html>",
                metadata={"section": "news"},
                last_fetched_at="2026-01-02T00:00:00+00:00",
            )
        )
        assert raw_recheck.created is False and raw_recheck.updated is False
        assert raw_recheck.action == "unchanged"
        assert raw_recheck.article.id == first.article.id
        assert raw_recheck.article.raw_html == "<html><input value='csrf-two'><p>same body</p></html>"
        assert raw_recheck.article.html_hash == "raw-hash-v2"
        assert raw_recheck.article.last_fetched_at == datetime(2026, 1, 2, tzinfo=timezone.utc)
        assert storage.connection.execute(
            "SELECT updated_at FROM articles WHERE id=?", (first.article.id,)
        ).fetchone()[0] == business_timestamp

        parsed_update = storage.upsert_article_with_outcome(
            Article(
                url="https://example.org/a",
                title="A",
                published_at="2026-01-01T00:00:00+00:00",
                content="changed body",
                content_html="<p>changed body</p>",
                content_hash="parsed-body-v2",
                html_hash="raw-hash-v3",
                raw_html="<html><input value='csrf-three'><p>changed body</p></html>",
                metadata={"section": "news", "revision": 2},
                last_fetched_at="2026-01-03T00:00:00+00:00",
            )
        )
        assert parsed_update.created is False and parsed_update.updated is True
        assert parsed_update.action == "updated"
        assert parsed_update.article.content == "changed body"
        assert parsed_update.article.metadata == {"section": "news", "revision": 2}
        assert storage.connection.execute(
            "SELECT updated_at FROM articles WHERE id=?", (first.article.id,)
        ).fetchone()[0] != business_timestamp


def test_different_urls_and_titles_with_same_content_hash_remain_distinct():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        first = storage.upsert_article_with_outcome(
            Article(url="https://example.org/one", title="First story", content_hash="generic-card"),
            zoo_id=zoo.id,
        )
        second = storage.upsert_article_with_outcome(
            Article(url="https://example.org/two", title="Second story", content_hash="generic-card"),
            zoo_id=zoo.id,
        )
        assert first.created is True and first.updated is False
        assert second.created is True and second.updated is False
        assert second.article.id != first.article.id
        assert len(storage.list_articles()) == 2
        assert [article.content_hash for article in storage.list_articles()] == [
            "generic-card", "generic-card"
        ]


def test_different_urls_same_title_and_hash_use_hash_fallback_and_outcome_is_known():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        first = storage.upsert_article_with_outcome(
            Article(url="https://example.org/one", title="Shared story", content_hash="same-card"),
            zoo_id=zoo.id,
        )
        second = storage.upsert_article_with_outcome(
            Article(url="https://example.org/two", title="Shared story", content_hash="same-card"),
            zoo_id=zoo.id,
        )
        assert first.created is True and first.updated is False
        assert second.created is False and second.updated is False
        assert second.article.id == first.article.id
        assert second.article.canonical_url == "https://example.org/one"
        assert len(storage.list_articles()) == 1


def test_url_less_same_hash_different_titles_do_not_merge_arbitrarily():
    with SQLiteStorage() as storage:
        first = storage.upsert_article(
            Article(title="First title", content_hash="boilerplate")
        )
        second = storage.upsert_article(
            Article(title="Second title", content_hash="boilerplate")
        )
        same_title = storage.upsert_article(
            Article(title=" first   title ", content_hash="boilerplate")
        )
        assert first.id != second.id
        assert same_title.id == first.id
        assert len(storage.list_articles()) == 2
        assert {article.title for article in storage.list_articles()} == {
            " first   title ", "Second title"
        }


def test_url_less_same_hash_without_titles_do_not_merge():
    with SQLiteStorage() as storage:
        first = storage.upsert_article(Article(content_hash="boilerplate"))
        second = storage.upsert_article(Article(content_hash="boilerplate"))
        assert first.id != second.id
        assert len(storage.list_articles()) == 2


def test_upsert_title_and_content_hash_changes_composite_identity_key():
    with SQLiteStorage() as storage:
        first = storage.upsert_article_with_outcome(
            Article(url="https://example.org/story", title="Before", content_hash="old-hash")
        )
        old_key = first.article.content_identity_key
        changed = storage.upsert_article_with_outcome(
            Article(
                url="https://example.org/story",
                title="After",
                content_hash="new-hash",
                content="new evidence",
            )
        )
        assert changed.created is False and changed.updated is True
        assert changed.article.content_identity_key
        assert changed.article.content_identity_key != old_key
        assert storage.connection.execute(
            "SELECT content_identity_key FROM articles WHERE id=?", (first.article.id,)
        ).fetchone()[0] == changed.article.content_identity_key


def test_composite_key_conflict_uses_runtime_merge_provenance():
    with SQLiteStorage() as storage:
        keeper = storage.upsert_article(
            Article(url="https://example.org/one", title="One", content_hash="shared")
        )
        donor = storage.upsert_article(
            Article(url="https://example.org/two", title="Two", content_hash="shared")
        )
        changed = storage.upsert_article_with_outcome(
            Article(url="https://example.org/one", title="Two", content_hash="shared")
        )
        assert changed.article.id == keeper.id
        assert changed.created is False and changed.updated is True
        assert storage.get_article(donor.id) is None
        provenance = changed.article.metadata["_runtime_merge_provenance"]
        assert provenance[0]["donor_article_id"] == donor.id


def test_discovery_tracking_urls_merge_to_one_identity_and_preserve_first_last_metadata():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        article = storage.upsert_article(Article(url="https://example.org/a", title="A"))
        storage.record_discovery(
            article_id=article.id, source_id=source.id,
            discovered_url="https://example.org/a?utm_source=first",
            discovered_at="2026-01-02T00:00:00+00:00", metadata={"first": 1},
        )
        storage.record_discovery(
            article_id=article.id, source_id=source.id,
            discovered_url="https://EXAMPLE.org/a?utm_campaign=second",
            discovered_at="2026-01-01T00:00:00+00:00", metadata={"second": 2},
        )
        discoveries = storage.list_discoveries(article_id=article.id)
        assert len(discoveries) == 1
        assert discoveries[0].discovered_at.isoformat() == "2026-01-01T00:00:00+00:00"
        assert discoveries[0].last_discovered_at.isoformat() == "2026-01-02T00:00:00+00:00"
        assert discoveries[0].metadata == {"first": 1, "second": 2}


def test_runtime_discovery_first_last_compare_offsets_in_utc_and_normalize_naive():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        article = storage.upsert_article(Article(url="https://example.org/a"))
        storage.record_discovery(
            article_id=article.id,
            source_id=source.id,
            discovered_url="https://example.org/a",
            discovered_at="2026-01-01T00:30:00+02:00",
        )
        storage.record_discovery(
            article_id=article.id,
            source_id=source.id,
            discovered_url="https://EXAMPLE.org/a?utm_source=later",
            discovered_at="2025-12-31T23:00:00+00:00",
        )
        discovery = storage.list_discoveries(article_id=article.id)[0]
        assert discovery.discovered_at.isoformat() == "2025-12-31T22:30:00+00:00"
        assert discovery.last_discovered_at.isoformat() == "2025-12-31T23:00:00+00:00"

        storage.record_discovery(
            article_id=article.id,
            source_id=source.id,
            discovered_url="https://example.org/a?utm_medium=naive",
            discovered_at="2025-12-31T23:30:00",
        )
        assert storage.list_discoveries(article_id=article.id)[0].last_discovered_at.isoformat() == "2025-12-31T23:30:00+00:00"


def test_unparseable_discovery_timestamp_is_preserved_with_fallback_order():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        article = storage.upsert_article(Article(url="https://example.org/a"))
        storage.record_discovery(
            article_id=article.id, source_id=source.id,
            discovered_url="https://example.org/a",
            discovered_at="legacy-unknown-first",
        )
        storage.record_discovery(
            article_id=article.id, source_id=source.id,
            discovered_url="https://EXAMPLE.org/a?utm_source=second",
            discovered_at="legacy-unknown-second",
        )
        discovery = storage.list_discoveries(article_id=article.id)[0]
        assert discovery.discovered_at == "legacy-unknown-first"
        assert discovery.last_discovered_at == "legacy-unknown-second"


def test_legacy_unparseable_discovery_timestamps_survive_coalescing_and_repeat_migration():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(
            id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT,
            discovered_url TEXT, discovered_key TEXT, discovered_at TEXT,
            last_discovered_at TEXT, metadata_json TEXT
        );
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed');
        INSERT INTO articles VALUES('a','https://example.org/a','https://example.org/a','A');
        INSERT INTO article_discoveries VALUES('d1','a','s','https://example.org/a?utm_source=one','https://example.org/a?utm_source=one','legacy-z',NULL,'{"one":1}');
        INSERT INTO article_discoveries VALUES('d2','a','s','https://EXAMPLE.org/a?utm_campaign=two','https://EXAMPLE.org/a?utm_campaign=two','legacy-a','legacy-z','{"two":2}');
        """
    )
    storage = SQLiteStorage(connection=connection)
    discovery = storage.list_discoveries(article_id="a")[0]
    assert discovery.discovered_at == "legacy-a"
    assert discovery.last_discovered_at == "legacy-z"
    assert discovery.metadata == {"one": 1, "two": 2}
    storage.create_schema()
    repeated = storage.list_discoveries(article_id="a")[0]
    assert repeated.discovered_at == "legacy-a"
    assert repeated.last_discovered_at == "legacy-z"


def test_article_updated_at_only_advances_for_evidence_changes():
    with SQLiteStorage() as storage:
        article = storage.upsert_article(
            Article(url="https://example.org/a", title="A", content="same")
        )
        initial = storage.connection.execute(
            "SELECT updated_at FROM articles WHERE id=?", (article.id,)
        ).fetchone()[0]
        time.sleep(0.002)
        storage.upsert_article(
            Article(url="https://example.org/a", title="A", content="same")
        )
        unchanged = storage.connection.execute(
            "SELECT updated_at FROM articles WHERE id=?", (article.id,)
        ).fetchone()[0]
        assert unchanged == initial
        time.sleep(0.002)
        storage.upsert_article(
            Article(url="https://example.org/a", title="A", content="changed")
        )
        changed = storage.connection.execute(
            "SELECT updated_at FROM articles WHERE id=?", (article.id,)
        ).fetchone()[0]
        assert changed != initial


def test_global_url_identity_wins_over_conflicting_title_identity():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        title_article = storage.upsert_article(
            Article(url="https://example.org/title-only", title="Same title"), zoo_id=zoo.id
        )
        global_article = storage.upsert_article(
            Article(url="https://example.org/global", title="Different title"), zoo_id=zoo.id
        )
        resolved = storage.upsert_article(
            Article(url="https://example.org/global", title="Same title", content="fresh"),
            zoo_id=zoo.id,
        )
        assert resolved.id == global_article.id
        assert resolved.canonical_url == "https://example.org/global"
        assert storage.get_article(title_article.id) is None
        assert storage.get_article_by_title("Same title", zoo.id).id == global_article.id


def test_article_outcome_source_object_resolves_zoo_slug_before_title_lookup():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = Source(id="s", zoo_id="z", url="https://example.org/feed")
        first = storage.upsert_article_with_outcome(
            Article(url="https://example.org/a", title="Scoped title"), source=source
        )
        second = storage.upsert_article_with_outcome(
            Article(url="https://example.org/b", title="Scoped title"), source=source
        )
        assert first.created is True
        assert second.created is False and second.article.id == first.article.id
        assert storage.get_article_by_title("Scoped title", zoo.slug).id == first.article.id


def test_legacy_tracking_variant_discoveries_are_coalesced_before_unique_index():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(
            id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT,
            discovered_url TEXT, discovered_key TEXT, discovered_at TEXT,
            last_discovered_at TEXT, metadata_json TEXT
        );
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed');
        INSERT INTO articles VALUES('a','https://example.org/a','https://example.org/a','A');
        INSERT INTO article_discoveries VALUES('d1','a','s','https://example.org/a?utm_source=one','https://example.org/a?utm_source=one','2026-01-01T00:30:00+02:00',NULL,'{"one":1}');
        INSERT INTO article_discoveries VALUES('d2','a','s','https://EXAMPLE.org/a?utm_campaign=two','https://EXAMPLE.org/a?utm_campaign=two','2025-12-31T23:00:00+00:00','2025-12-31T23:30:00+00:00','{"two":2}');
        """
    )
    storage = SQLiteStorage(connection=connection)
    discoveries = storage.list_discoveries(article_id="a")
    assert len(discoveries) == 1
    assert discoveries[0].discovered_at.isoformat() == "2025-12-31T22:30:00+00:00"
    assert discoveries[0].last_discovered_at.isoformat() == "2025-12-31T23:30:00+00:00"
    assert discoveries[0].metadata == {"one": 1, "two": 2}


def test_schema_migration_rejects_active_caller_transaction_without_rollback():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    storage = SQLiteStorage(connection=connection)
    connection.execute("BEGIN")
    connection.execute("CREATE TABLE caller_data(value TEXT)")
    connection.execute("INSERT INTO caller_data VALUES('keep')")
    with pytest.raises(RuntimeError, match="active caller transaction"):
        storage.create_schema()
    assert connection.execute("SELECT value FROM caller_data").fetchone()[0] == "keep"
    connection.rollback()


def test_concurrent_file_initialization_retries_wal_setup(tmp_path):
    database = tmp_path / "concurrent-init.db"
    failures = []

    for _ in range(8):
        barrier = threading.Barrier(2)

        def initialize() -> None:
            try:
                barrier.wait(timeout=5)
                storage = SQLiteStorage(database)
                storage.connection.execute("SELECT 1").fetchone()
                storage.close()
            except BaseException as error:  # pragma: no cover - assertion below reports it
                failures.append(error)

        workers = [threading.Thread(target=initialize) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
        assert all(not worker.is_alive() for worker in workers)

    assert failures == []


@pytest.mark.parametrize("constraint", ("UNIQUE", "REFERENCES zoos(id)"))
def test_fk_rebuild_rejects_unpreservable_extension_column_constraint(constraint):
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        f"""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(
            id TEXT PRIMARY KEY, zoo_id TEXT REFERENCES zoos(id), url TEXT,
            extra TEXT {constraint}
        );
        INSERT INTO zoos VALUES('z', 'z', 'Zoo');
        INSERT INTO sources VALUES('s', 'z', 'https://example.org/feed', 'keep');
        """
    )
    original_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
    ).fetchone()[0]
    with pytest.raises(RuntimeError, match="extension column constraint"):
        SQLiteStorage(connection=connection)
    assert connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sources'"
    ).fetchone()[0] == original_sql
    assert connection.execute("SELECT extra FROM sources WHERE id='s'").fetchone()[0] == "keep"
    assert "country_code" not in {
        row[1] for row in connection.execute("PRAGMA table_info(zoos)")
    }


def test_legacy_empty_article_identity_values_become_null_idempotently():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE articles(
            id TEXT PRIMARY KEY, canonical_url TEXT, normalized_url TEXT,
            content_hash TEXT, url TEXT, title TEXT
        );
        INSERT INTO articles VALUES('a1', '', '', '', '', 'No identity 1');
        INSERT INTO articles VALUES('a2', '', '', '', '', 'No identity 2');
        """
    )
    storage = SQLiteStorage(connection=connection)
    values = [
        tuple(row)
        for row in connection.execute(
            "SELECT canonical_url,normalized_url,content_hash,content_identity_key FROM articles ORDER BY id"
        )
    ]
    assert values == [(None, None, None, None), (None, None, None, None)]
    storage.create_schema()
    assert [
        tuple(row)
        for row in connection.execute(
            "SELECT canonical_url,normalized_url,content_hash,content_identity_key FROM articles ORDER BY id"
        )
    ] == values


def test_legacy_same_hash_and_normalized_title_merges_before_content_identity_index():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE articles(
            id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT,
            content_hash TEXT
        );
        INSERT INTO articles VALUES('a1','https://example.org/one','https://example.org/one','Same title','card');
        INSERT INTO articles VALUES('a2','https://example.org/two','https://example.org/two',' same   title ','card');
        """
    )
    storage = SQLiteStorage(connection=connection)
    assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    key = connection.execute("SELECT content_identity_key FROM articles").fetchone()[0]
    assert key
    storage.create_schema()
    assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    assert connection.execute("SELECT content_identity_key FROM articles").fetchone()[0] == key


def test_concurrent_same_content_identity_has_one_created_outcome(tmp_path):
    database = tmp_path / "concurrent-content-identity.db"
    SQLiteStorage(database).close()
    barrier = threading.Barrier(2)
    outcomes = []
    failures = []

    def upsert(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            with SQLiteStorage(database) as storage:
                outcomes.append(
                    storage.upsert_article_with_outcome(
                        Article(
                            url=f"https://example.org/{index}",
                            title="Concurrent story",
                            content_hash="same-card",
                        )
                    )
                )
        except BaseException as error:  # pragma: no cover - assertion reports it
            failures.append(error)

    workers = [threading.Thread(target=upsert, args=(index,)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
    assert failures == []
    assert len(outcomes) == 2
    assert sum(result.created for result in outcomes) == 1
    assert sum(not result.created for result in outcomes) == 1
    with SQLiteStorage(database) as storage:
        assert len(storage.list_articles()) == 1
def test_new_fields_round_trip_and_partial_updates_preserve_evidence():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo", country_code="DE", language="de"))
        storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed", language="de", last_http_status=200))
        fetched = datetime(2026, 1, 2, tzinfo=timezone.utc)
        storage.upsert_article(Article(url="https://example.org/a", content_hash="content", html_hash="html", language="de", http_status=203, crawl_status="fetched", last_fetched_at=fetched))
        outcome = storage.upsert_article_with_outcome(Article(url="https://example.org/a", title="Title"))
        assert outcome.created is False and outcome.already_known is True
        assert outcome.article.content_hash == "content"
        assert outcome.article.html_hash == "html"
        assert outcome.article.language == "de"
        assert outcome.article.http_status == 203
        assert outcome.article.crawl_status == "fetched"
        assert outcome.article.last_fetched_at == fetched


def test_current_source_check_replaces_http_evidence_while_config_upsert_preserves_it():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed", last_http_status=200))
        storage.update_source_status(source.id, status="error", success=False, error="connection failed", http_status=None)
        failed = storage.get_source(source.id)
        assert failed.status == "error" and failed.success is False
        assert failed.last_error == "connection failed"
        assert failed.last_http_status is None
        storage.upsert_source(Source(id=source.id, zoo_id=zoo.id, url=source.url, language="de"))
        registered = storage.get_source(source.id)
        assert registered.status == "error" and registered.last_error == "connection failed"
        assert registered.last_http_status is None
        storage.update_source_status(source.id, status="completed", success=True, error=None, http_status=204)
        succeeded = storage.get_source(source.id)
        assert succeeded.status == "completed" and succeeded.success is True
        assert succeeded.last_http_status == 204
        assert succeeded.last_error is None


def test_source_registry_reregistration_preserves_all_operational_evidence():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed", language="de"))
        checked = datetime(2026, 1, 2, tzinfo=timezone.utc)
        storage.update_source_status(source.id, status="completed", checked_at=checked, success=True, error="prior warning", http_status=204)
        storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed", language="de", config={"role": "news"}))
        loaded = storage.get_source(source.id)
        assert loaded.status == "completed"
        assert loaded.success is True
        assert loaded.last_checked == checked
        assert loaded.last_success == checked
        assert loaded.last_error == "prior warning"
        assert loaded.last_http_status == 204
        assert loaded.config == {"role": "news"}


def test_first_and_last_discovery_are_distinct():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        article = storage.upsert_article(Article(url="https://example.org/a"))
        storage.record_discovery(article_id=article.id, source_id=source.id, discovered_at="2026-01-01T00:00:00+00:00")
        storage.record_discovery(article_id=article.id, source_id=source.id, discovered_at="2026-01-02T00:00:00+00:00")
        discovery = storage.list_discoveries(article_id=article.id)[0]
        assert discovery.discovered_at.isoformat() == "2026-01-01T00:00:00+00:00"
        assert discovery.last_discovered_at.isoformat() == "2026-01-02T00:00:00+00:00"


def test_run_duration_and_new_counts_round_trip():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed"))
        run = storage.start_crawl_run(CrawlRun(id="r", batch_id="b", started_at="2026-01-01T00:00:00+00:00"))
        finished = storage.finish_crawl_run(run.id, finished_at="2026-01-01T00:00:01.250000+00:00")
        assert finished.duration_ms == 1250
        stat = storage.record_run_stat(CrawlRunStat(crawl_run_id=run.id, zoo_id=zoo.id, source_id=source.id, already_known_count=7, duplicate_candidate_count=2, started_at="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:00.010000+00:00"))
        assert stat.duration_ms == 10
        assert stat.already_known_count == 7 and stat.duplicate_candidate_count == 2


def test_idempotent_migration_backfills_safe_run_values():
    with SQLiteStorage() as storage:
        storage.connection.execute(
            "INSERT INTO crawl_runs(id,batch_id,started_at,finished_at,duration_ms,status) VALUES(?,?,?,?,?,?)",
            ("r", "b", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:02+00:00", None, "completed"),
        )
        storage.connection.execute(
            "INSERT INTO crawl_run_stats(id,crawl_run_id,discovered_count,fetched_count,stored_count,already_known_count,duplicate_candidate_count,error_count,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s", "r", None, None, None, None, None, None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00.005000+00:00"),
        )
        storage.create_schema()
        assert storage.get_crawl_run("r").duration_ms == 2000
        stat = storage.get_run_stat("s")
        assert stat.duration_ms == 5
        assert (stat.discovered_count, stat.fetched_count, stat.stored_count) == (0, 0, 0)
        assert (stat.already_known_count, stat.duplicate_candidate_count, stat.error_count) == (0, 0, 0)


def test_foreign_keys_are_explicit_and_enforced():
    with SQLiteStorage() as storage:
        expected = {
            "sources": {("zoos", "RESTRICT", "CASCADE")},
            "article_discoveries": {("articles", "CASCADE", "CASCADE"), ("sources", "CASCADE", "CASCADE")},
            "crawl_run_stats": {("crawl_runs", "CASCADE", "CASCADE"), ("zoos", "SET NULL", "CASCADE"), ("sources", "SET NULL", "CASCADE")},
        }
        for table, constraints in expected.items():
            actual = {(row[2], row[6], row[5]) for row in storage.connection.execute(f"PRAGMA foreign_key_list({table})")}
            assert actual == constraints
        with pytest.raises(sqlite3.IntegrityError):
            storage.connection.execute("INSERT INTO sources(id,zoo_id,url) VALUES('bad','missing','https://x')")


def test_migration_rolls_back_on_orphaned_legacy_data():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        INSERT INTO sources VALUES('s', 'missing', 'https://example.org/feed');
    """)
    with pytest.raises(RuntimeError, match="foreign_key_check"):
        SQLiteStorage(connection=connection)
    assert connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
    assert "country_code" not in {row[1] for row in connection.execute("PRAGMA table_info(zoos)")}


def test_partial_foreign_key_schema_is_rebuilt_with_exact_actions():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT REFERENCES zoos(id), url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT REFERENCES articles(id), source_id TEXT, discovered_url TEXT);
        CREATE TABLE crawl_runs(id TEXT PRIMARY KEY, batch_id TEXT);
        CREATE TABLE crawl_run_stats(id TEXT PRIMARY KEY, crawl_run_id TEXT REFERENCES crawl_runs(id), zoo_id TEXT, source_id TEXT);
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed');
        INSERT INTO articles VALUES('a','https://example.org/a','https://example.org/a');
        INSERT INTO article_discoveries VALUES('d','a','s','https://example.org/a');
        INSERT INTO crawl_runs VALUES('r','b');
        INSERT INTO crawl_run_stats VALUES('rs','r','z','s');
    """)
    with SQLiteStorage(connection=connection):
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        expected = {
            "sources": {("zoo_id", "zoos", "id", "CASCADE", "RESTRICT")},
            "article_discoveries": {("article_id", "articles", "id", "CASCADE", "CASCADE"), ("source_id", "sources", "id", "CASCADE", "CASCADE")},
            "crawl_run_stats": {("crawl_run_id", "crawl_runs", "id", "CASCADE", "CASCADE"), ("zoo_id", "zoos", "id", "CASCADE", "SET NULL"), ("source_id", "sources", "id", "CASCADE", "SET NULL")},
        }
        for table, wanted in expected.items():
            actual = {(row[3], row[2], row[4], row[5], row[6]) for row in connection.execute(f"PRAGMA foreign_key_list({table})")}
            assert actual == wanted
        assert connection.execute("SELECT COUNT(*) FROM crawl_run_stats").fetchone()[0] == 1


def test_fk_rebuild_preserves_extension_columns_indexes_and_triggers():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(
            id TEXT PRIMARY KEY, zoo_id TEXT REFERENCES zoos(id), url TEXT,
            extra_note TEXT NOT NULL DEFAULT 'unset', extension_score INTEGER DEFAULT 7,
            "expression score" NUMERIC NOT NULL DEFAULT (40 + 2)
        );
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT);
        CREATE TABLE article_discoveries(
            id TEXT PRIMARY KEY, article_id TEXT REFERENCES articles(id), source_id TEXT,
            discovered_url TEXT, external_tag TEXT DEFAULT 'legacy'
        );
        CREATE TABLE crawl_runs(id TEXT PRIMARY KEY, batch_id TEXT);
        CREATE TABLE crawl_run_stats(
            id TEXT PRIMARY KEY, crawl_run_id TEXT REFERENCES crawl_runs(id), zoo_id TEXT,
            source_id TEXT, vendor_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE extension_audit(source_id TEXT, note TEXT);
        CREATE INDEX ix_sources_extension_score ON sources(extension_score, "expression score");
        CREATE TRIGGER trg_sources_extra_note AFTER UPDATE OF extra_note ON sources
        BEGIN
          INSERT INTO extension_audit(source_id,note) VALUES(NEW.id,NEW.extra_note);
        END;
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed','preserve me',42,99);
        INSERT INTO articles VALUES('a','https://example.org/a','https://example.org/a');
        INSERT INTO article_discoveries VALUES('d','a','s','https://example.org/a','tagged');
        INSERT INTO crawl_runs VALUES('r','b');
        INSERT INTO crawl_run_stats VALUES('rs','r','z','s',11);
    """)
    storage = SQLiteStorage(connection=connection)
    assert dict(connection.execute('SELECT extra_note,extension_score,"expression score" FROM sources WHERE id=\'s\'').fetchone()) == {"extra_note": "preserve me", "extension_score": 42, "expression score": 99}
    assert connection.execute("SELECT external_tag FROM article_discoveries WHERE id='d'").fetchone()[0] == "tagged"
    assert connection.execute("SELECT vendor_count FROM crawl_run_stats WHERE id='rs'").fetchone()[0] == 11
    columns = {row[1]: (row[2], row[3], row[4]) for row in connection.execute("PRAGMA table_info(sources)")}
    assert columns["extra_note"] == ("TEXT", 1, "'unset'")
    assert columns["extension_score"] == ("INTEGER", 0, "7")
    assert columns["expression score"] == ("NUMERIC", 1, "40 + 2")
    assert connection.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='ix_sources_extension_score'").fetchone() is not None
    assert connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='trg_sources_extra_note'").fetchone() is not None
    connection.execute("UPDATE sources SET extra_note='after migration' WHERE id='s'")
    assert tuple(connection.execute("SELECT source_id,note FROM extension_audit").fetchone()) == ("s", "after migration")
    schema_before = [tuple(row) for row in connection.execute("PRAGMA table_info(sources)")]
    storage.create_schema()
    assert [tuple(row) for row in connection.execute("PRAGMA table_info(sources)")] == schema_before
    assert connection.execute("SELECT extra_note FROM sources WHERE id='s'").fetchone()[0] == "after migration"
    connection.execute("INSERT INTO sources(id,zoo_id,url,normalized_url,extra_note) VALUES('defaulted','z','https://example.org/other','https://example.org/other','x')")
    assert connection.execute('SELECT "expression score" FROM sources WHERE id=\'defaulted\'').fetchone()[0] == 42
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_fk_rebuild_rolls_back_when_custom_schema_object_cannot_be_recreated():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.create_collation("REMOVED_COLLATION", lambda left, right: (left > right) - (left < right))
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT REFERENCES zoos(id), url TEXT, extra_note TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT REFERENCES articles(id), source_id TEXT, discovered_url TEXT);
        CREATE TABLE crawl_runs(id TEXT PRIMARY KEY, batch_id TEXT);
        CREATE TABLE crawl_run_stats(id TEXT PRIMARY KEY, crawl_run_id TEXT REFERENCES crawl_runs(id), zoo_id TEXT, source_id TEXT);
        CREATE INDEX ix_unrecreatable ON sources(extra_note COLLATE REMOVED_COLLATION);
    """)
    connection.create_collation("REMOVED_COLLATION", None)
    with pytest.raises(sqlite3.OperationalError, match="collation"):
        SQLiteStorage(connection=connection)
    assert "extra_note" in {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
    assert connection.execute("SELECT sql FROM sqlite_master WHERE name='ix_unrecreatable'").fetchone() is not None
    assert "country_code" not in {row[1] for row in connection.execute("PRAGMA table_info(zoos)")}


def test_legacy_duplicate_identities_merge_evidence_and_discoveries():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT, content TEXT, content_hash TEXT, html_hash TEXT, raw_html TEXT, metadata_json TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT, discovered_at TEXT);
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed');
        INSERT INTO articles VALUES('a1','https://EXAMPLE.org/story/','https://example.org/story/','Old title','Old body','old-content-hash','old-html-hash','<p>Old body</p>','{"shared":"old","old_only":1}');
        INSERT INTO articles VALUES('a2','https://example.org/story?utm_source=x','https://example.org/story?utm_source=x','New title','New and longer body','new-content-hash','new-html-hash','<p>New and longer body</p>','{"shared":"new","new_only":2}');
        INSERT INTO article_discoveries VALUES('d1','a1','s','https://example.org/story/','2026-01-01T00:00:00+00:00');
        INSERT INTO article_discoveries VALUES('d2','a2','s','https://example.org/story?utm_source=x','2026-01-02T00:00:00+00:00');
    """)
    storage = SQLiteStorage(connection=connection)
    assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    article = storage.list_articles()[0]
    assert article.title == "New title" and article.content == "New and longer body"
    assert article.content_hash == "new-content-hash" and article.html_hash == "new-html-hash"
    assert set(article.metadata["merged_legacy_article_ids"]) in ({"a1"}, {"a2"})
    provenance = article.metadata["_migration_provenance"]
    assert provenance == [{
        "donor_article_id": "a1",
        "conflicting_evidence": {
            "source_url": "https://example.org/story/",
            "title": "Old title",
            "content": "Old body",
            "content_hash": "old-content-hash",
            "html_hash": "old-html-hash",
            "raw_html": "<p>Old body</p>",
        },
        "donor_metadata": {"shared": "old", "old_only": 1},
    }]
    assert article.metadata["shared"] == "new"
    assert article.metadata["old_only"] == 1 and article.metadata["new_only"] == 2
    discoveries = storage.list_discoveries(article_id=article.id)
    assert len(discoveries) == 1
    assert discoveries[0].discovered_at.isoformat() == "2026-01-01T00:00:00+00:00"
    assert discoveries[0].last_discovered_at.isoformat() == "2026-01-02T00:00:00+00:00"
    storage.create_schema()
    assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 1
    assert storage.list_articles()[0].metadata["_migration_provenance"] == provenance
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_content_hash_collisions_with_different_titles_remain_distinct():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(
            id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT, author TEXT,
            summary TEXT, content TEXT, content_hash TEXT, html_hash TEXT, raw_html TEXT,
            language TEXT, metadata_json TEXT
        );
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT, discovered_at TEXT);
        INSERT INTO zoos VALUES('z','z','Zoo');
        INSERT INTO sources VALUES('s','z','https://example.org/feed');
        INSERT INTO articles VALUES('a1','https://example.org/first','https://example.org/first','A1',NULL,NULL,'one','hash-a1','html-a1',NULL,NULL,'{"origin":"one"}');
        INSERT INTO articles VALUES('a2','https://example.org/shared?utm_source=x','https://example.org/shared?utm_source=x','A2','Author 2',NULL,'two','bridge-hash','html-a2','<p>two</p>',NULL,'{"origin":"two"}');
        INSERT INTO articles VALUES('a3','https://example.org/final','https://example.org/final','A3','Author 3','Summary 3','three richer body','bridge-hash','html-a3','<p>three richer body</p>','de','{"origin":"three"}');
        INSERT INTO article_discoveries VALUES('d1','a1','s','https://example.org/first','2026-01-01T00:00:00+00:00');
        INSERT INTO article_discoveries VALUES('d2','a2','s','https://example.org/shared?utm_source=x','2026-01-02T00:00:00+00:00');
        INSERT INTO article_discoveries VALUES('d3','a3','s','https://example.org/final','2026-01-03T00:00:00+00:00');
    """)
    storage = SQLiteStorage(connection=connection)
    articles = storage.list_articles()
    assert {article.id for article in articles} == {"a1", "a2", "a3"}
    assert sum(article.content_hash == "bridge-hash" for article in articles) == 2
    storage.create_schema()
    assert connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_article_raw_url_lexemes_and_joined_read_model_round_trip():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo"))
        feed = storage.upsert_source(Source(id="feed", zoo_id=zoo.id, url="https://example.org/feed"))
        archive = storage.upsert_source(Source(id="archive", zoo_id=zoo.id, url="https://example.org/archive"))
        source_lexeme = " https://EXAMPLE.org/story/?b=2&utm_source=feed&a=1 "
        discovered_lexeme = "https://EXAMPLE.org/story/?utm_medium=archive&b=2&a=1"
        article = storage.upsert_article(
            Article(url=source_lexeme, title="Evidence", content="Body"),
            source_id=feed.id,
            discovered_url=source_lexeme,
            discovered_at="2026-01-02T02:00:00+02:00",
        )
        storage.record_discovery(
            article_id=article.id,
            source_id=archive.id,
            discovered_url=discovered_lexeme,
            discovered_at="2026-01-03T01:30:00+01:00",
        )

        loaded = storage.get_article(article.id)
        assert loaded and loaded.source_url_raw == source_lexeme
        discoveries = storage.list_discoveries(article_id=article.id)
        assert {item.discovered_url_raw for item in discoveries} == {source_lexeme, discovered_lexeme}
        assert all(item.discovered_url != item.discovered_url_raw for item in discoveries)

        read = storage.get_article_read_model(article.id)
        assert read and read.first_discovered_at == "2026-01-02T00:00:00+00:00"
        assert read.last_discovered_at == "2026-01-03T00:30:00+00:00"
        assert read.created_at and read.storage_updated_at
        assert len(read.article_discoveries) == 2
        assert {item.source_id for item in read.article_discoveries} == {"feed", "archive"}


def test_v7_raw_url_columns_migrate_legacy_rows_idempotently():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT);
        INSERT INTO zoos VALUES('z', 'z', 'Zoo');
        INSERT INTO sources VALUES('s', 'z', 'https://example.org/feed');
        INSERT INTO articles VALUES('a', 'https://example.org/story', 'https://EXAMPLE.org/story/?utm_source=legacy', 'Legacy');
        INSERT INTO article_discoveries VALUES('d', 'a', 's', 'https://EXAMPLE.org/story/?utm_medium=legacy');
        """
    )
    storage = SQLiteStorage(connection=connection)
    assert {"source_url_raw"}.issubset(SQLiteStorage._columns(connection, "articles"))
    assert {"discovered_url_raw"}.issubset(SQLiteStorage._columns(connection, "article_discoveries"))
    assert storage.get_article("a").source_url_raw == "https://EXAMPLE.org/story/?utm_source=legacy"
    assert storage.list_discoveries(article_id="a")[0].discovered_url_raw == "https://EXAMPLE.org/story/?utm_medium=legacy"
    raw_before = tuple(
        connection.execute(
            "SELECT source_url_raw FROM articles WHERE id='a'"
        ).fetchone()
    ) + tuple(
        connection.execute(
            "SELECT discovered_url_raw FROM article_discoveries WHERE id='d'"
        ).fetchone()
    )
    storage.create_schema()
    raw_after = tuple(
        connection.execute(
            "SELECT source_url_raw FROM articles WHERE id='a'"
        ).fetchone()
    ) + tuple(
        connection.execute(
            "SELECT discovered_url_raw FROM article_discoveries WHERE id='d'"
        ).fetchone()
    )
    assert raw_after == raw_before
    assert connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == str(SQLiteStorage.SCHEMA_VERSION)


def test_malformed_legacy_urls_keep_raw_evidence_and_empty_identities_idempotently():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    malformed_source = "https://[invalid-source"
    malformed_article = "https://[invalid-article"
    malformed_discovery = "https://[invalid-discovery"
    connection.executescript(
        f"""
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT);
        INSERT INTO zoos VALUES('z', 'z', 'Zoo');
        INSERT INTO sources VALUES('s', 'z', '{malformed_source}');
        INSERT INTO articles VALUES('a', '{malformed_article}', '{malformed_article}', 'Legacy');
        INSERT INTO article_discoveries VALUES('d', 'a', 's', '{malformed_discovery}');
        """
    )

    storage = SQLiteStorage(connection=connection)
    first = {
        "source": tuple(
            connection.execute("SELECT url,normalized_url FROM sources WHERE id='s'").fetchone()
        ),
        "article": tuple(
            connection.execute(
                "SELECT canonical_url,normalized_url,source_url,source_url_raw FROM articles WHERE id='a'"
            ).fetchone()
        ),
        "discovery": tuple(
            connection.execute(
                "SELECT discovered_url,discovered_url_raw,discovered_key FROM article_discoveries WHERE id='d'"
            ).fetchone()
        ),
    }
    assert first == {
        "source": (malformed_source, None),
        "article": (None, None, malformed_article, malformed_article),
        "discovery": (None, malformed_discovery, ""),
    }
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    storage.create_schema()
    second = {
        "source": tuple(
            connection.execute("SELECT url,normalized_url FROM sources WHERE id='s'").fetchone()
        ),
        "article": tuple(
            connection.execute(
                "SELECT canonical_url,normalized_url,source_url,source_url_raw FROM articles WHERE id='a'"
            ).fetchone()
        ),
        "discovery": tuple(
            connection.execute(
                "SELECT discovered_url,discovered_url_raw,discovered_key FROM article_discoveries WHERE id='d'"
            ).fetchone()
        ),
    }
    assert second == first
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_fresh_schema_creates_crawler_lease_table_and_indexes():
    with SQLiteStorage() as storage:
        columns = SQLiteStorage._columns(storage.connection, "crawler_leases")
        assert columns == {"name", "owner", "acquired_at", "lease_until", "heartbeat_at"}
        assert storage.list_leases() == []
        indexes = {
            row["name"]
            for row in storage.connection.execute("PRAGMA index_list(crawler_leases)").fetchall()
        }
        assert "ux_crawler_leases_name" in indexes
        assert "idx_crawler_leases_until" in indexes


def test_legacy_database_gets_idempotent_crawler_lease_migration():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT);
        CREATE TABLE crawler_leases(
            name TEXT, owner TEXT, acquired_at TEXT, lease_until TEXT, heartbeat_at TEXT
        );
        INSERT INTO crawler_leases VALUES
            ('crawler', 'old-owner', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:10+00:00', '2026-01-01T00:00:00+00:00'),
            ('crawler', 'newer-owner', '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:20+00:00', '2026-01-01T00:00:01+00:00'),
            ('invalid-null', NULL, '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('invalid-blank', '   ', '2026-01-01T00:00:00+00:00', '2099-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('tie', 'tie-first', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'),
            ('tie', 'tie-second', '2026-01-01T01:00:00+01:00', '2026-01-01T01:00:00+01:00', '2026-01-01T01:00:00+01:00');
        """
    )
    storage = SQLiteStorage(connection=connection)
    assert connection.execute("SELECT COUNT(*) FROM crawler_leases").fetchone()[0] == 2
    assert storage.get_lease()["owner"] == "newer-owner"
    assert storage.get_lease("tie")["owner"] == "tie-first"
    assert storage.acquire_lease(
        "invalid-null", "valid-owner", now="2026-01-01T00:00:01+00:00", ttl_seconds=30
    )
    storage.create_schema()
    assert connection.execute("SELECT COUNT(*) FROM crawler_leases").fetchone()[0] == 3
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == str(SQLiteStorage.SCHEMA_VERSION)


def test_crawler_lease_acquire_conflict_renew_and_owner_checked_release():
    first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SQLiteStorage() as storage:
        assert storage.acquire_crawler_lease("owner-a", ttl_seconds=60, now=first_now)
        assert not storage.connection.in_transaction
        lease = storage.get_lease()
        assert lease and lease["owner"] == "owner-a"
        assert lease["acquired_at"] == "2026-01-01T00:00:00+00:00"
        assert lease["lease_until"] == "2026-01-01T00:01:00+00:00"
        assert not storage.acquire_crawler_lease(
            "owner-b", ttl_seconds=60, now=first_now.replace(second=30)
        )
        assert not storage.renew_crawler_lease(
            "owner-b", ttl_seconds=60, now=first_now.replace(second=30)
        )
        assert not storage.release_crawler_lease("owner-b")
        assert storage.renew_crawler_lease(
            "owner-a", ttl_seconds=60, now=first_now.replace(second=30)
        )
        renewed = storage.get_lease()
        assert renewed and renewed["heartbeat_at"] == "2026-01-01T00:00:30+00:00"
        assert renewed["lease_until"] == "2026-01-01T00:01:30+00:00"
        assert storage.release_crawler_lease("owner-a")
        assert storage.get_lease() is None
        assert not storage.release_crawler_lease("owner-a")


def test_crawler_lease_same_owner_reacquire_is_idempotent_and_expired_takeover_is_atomic():
    first_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with SQLiteStorage() as storage:
        assert storage.acquire_lease("crawler", "owner-a", ttl_seconds=10, now=first_now)
        assert storage.acquire_lease(
            "crawler", "owner-a", ttl_seconds=10, now=first_now.replace(second=5)
        )
        renewed = storage.get_lease()
        assert renewed and renewed["acquired_at"] == "2026-01-01T00:00:00+00:00"
        assert renewed["lease_until"] == "2026-01-01T00:00:15+00:00"
        assert storage.acquire_lease(
            "crawler", "owner-b", ttl_seconds=20, now=first_now.replace(second=16)
        )
        taken_over = storage.get_lease()
        assert taken_over and taken_over["owner"] == "owner-b"
        assert taken_over["acquired_at"] == "2026-01-01T00:00:16+00:00"
        assert not storage.renew_lease(
            "crawler", "owner-a", ttl_seconds=60, now=first_now.replace(second=17)
        )
        assert not storage.release_lease("crawler", "owner-a")
        assert storage.release_lease("crawler", "owner-b")


def test_crawler_lease_rejects_missing_or_invalid_owner_for_every_operation():
    with SQLiteStorage() as storage:
        for operation in (storage.acquire_lease, storage.renew_lease, storage.release_lease):
            for owner in (None, "", "   ", "\t\n", 123):
                with pytest.raises(ValueError, match="owner"):
                    operation(owner=owner)
        assert storage.list_leases() == []


def test_crawler_lease_rejects_invalid_ttl_without_creating_a_row():
    with SQLiteStorage() as storage:
        for ttl in (
            0,
            -1,
            1e-7,
            1e300,
            float("inf"),
            float("nan"),
            "not-a-number",
        ):
            with pytest.raises(ValueError, match="TTL"):
                storage.acquire_crawler_lease("owner", ttl_seconds=ttl)
        assert storage.get_lease() is None


def test_crawler_lease_rejects_datetime_overflow_and_corrupt_extreme_offsets():
    with SQLiteStorage() as storage:
        with pytest.raises(ValueError, match="datetime range"):
            storage.acquire_crawler_lease(
                "owner", ttl_seconds=1, now=datetime.max.replace(tzinfo=timezone.utc)
            )
        for value in (
            "0001-01-01T00:00:00+23:59:59",
            "9999-12-31T23:59:59-23:59:59",
        ):
            with pytest.raises(ValueError, match="timestamp"):
                SQLiteStorage._lease_datetime(value)
        assert storage.get_lease() is None


def test_legacy_extreme_offset_timestamp_is_treated_as_invalid_during_migration():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE articles(id TEXT PRIMARY KEY, canonical_url TEXT, url TEXT, title TEXT);
        CREATE TABLE article_discoveries(id TEXT PRIMARY KEY, article_id TEXT, source_id TEXT, discovered_url TEXT);
        CREATE TABLE crawler_leases(
            name TEXT, owner TEXT, acquired_at TEXT, lease_until TEXT, heartbeat_at TEXT
        );
        INSERT INTO crawler_leases VALUES
            ('extreme', 'bad', '2026-01-01T00:00:00+00:00', '0001-01-01T00:00:00+23:59:59', '2026-01-01T00:00:00+00:00'),
            ('extreme', 'valid', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00', '2026-01-01T00:00:00+00:00');
        """
    )
    storage = SQLiteStorage(connection=connection)
    assert storage.get_lease("extreme")["owner"] == "valid"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_crawler_lease_competes_across_two_sqlite_connections(tmp_path):
    path = tmp_path / "lease.sqlite"
    first = SQLiteStorage(path)
    second = SQLiteStorage(path)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        assert first.acquire_crawler_lease("owner-a", ttl_seconds=10, now=now)
        assert not second.acquire_crawler_lease("owner-b", ttl_seconds=10, now=now)
        assert second.acquire_crawler_lease(
            "owner-b", ttl_seconds=10, now=now.replace(second=11)
        )
        assert not first.renew_crawler_lease(
            "owner-a", ttl_seconds=10, now=now.replace(second=12)
        )
        assert not first.release_crawler_lease("owner-a")
        assert second.release_crawler_lease("owner-b")
    finally:
        first.close()
        second.close()
