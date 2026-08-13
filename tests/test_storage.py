import sqlite3

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
    assert storage.get_article("a").content_hash == "raw-hash"
    assert len(storage.list_discoveries(article_id="a")) == 1
    storage.create_schema()
    assert connection.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == "3"
