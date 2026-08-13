import sqlite3
from datetime import datetime, timezone

import pytest

from zoofan.models import Article, ArticleDiscovery, CrawlRun, CrawlRunStat, Source, Zoo
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
    assert connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "4"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_new_fields_round_trip_and_partial_updates_preserve_evidence():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="z", slug="z", name="Zoo", country_code="DE", language="de"))
        source = storage.upsert_source(Source(id="s", zoo_id=zoo.id, url="https://example.org/feed", language="de", last_http_status=200))
        fetched = datetime(2026, 1, 2, tzinfo=timezone.utc)
        first = storage.upsert_article(Article(url="https://example.org/a", content_hash="content", html_hash="html", language="de", http_status=203, crawl_status="fetched", last_fetched_at=fetched))
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
    with SQLiteStorage(connection=connection) as storage:
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


def test_transitive_duplicate_merge_flattens_and_preserves_provenance():
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
        INSERT INTO articles VALUES('a1','https://example.org/shared/','https://example.org/shared/','A1',NULL,NULL,'one','hash-a1','html-a1',NULL,NULL,'{"origin":"one"}');
        INSERT INTO articles VALUES('a2','https://example.org/shared?utm_source=x','https://example.org/shared?utm_source=x','A2','Author 2',NULL,'two','bridge-hash','html-a2','<p>two</p>',NULL,'{"origin":"two"}');
        INSERT INTO articles VALUES('a3','https://example.org/final','https://example.org/final','A3','Author 3','Summary 3','three richer body','bridge-hash','html-a3','<p>three richer body</p>','de','{"origin":"three"}');
        INSERT INTO article_discoveries VALUES('d1','a1','s','https://example.org/shared/','2026-01-01T00:00:00+00:00');
        INSERT INTO article_discoveries VALUES('d2','a2','s','https://example.org/shared?utm_source=x','2026-01-02T00:00:00+00:00');
        INSERT INTO article_discoveries VALUES('d3','a3','s','https://example.org/final','2026-01-03T00:00:00+00:00');
    """)
    storage = SQLiteStorage(connection=connection)
    final = storage.list_articles()[0]
    assert final.id == "a3"
    assert final.metadata["merged_legacy_article_ids"] == ["a1", "a2"]
    snapshots = final.metadata["_migration_provenance"]
    assert [item["donor_article_id"] for item in snapshots] == ["a1", "a2"]
    assert snapshots[0]["donor_metadata"] == {"origin": "one"}
    assert snapshots[1]["donor_metadata"] == {"origin": "two"}
    assert all("_migration_provenance" not in item["donor_metadata"] for item in snapshots)
    metadata_before = final.metadata
    storage.create_schema()
    assert storage.list_articles()[0].metadata == metadata_before
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
