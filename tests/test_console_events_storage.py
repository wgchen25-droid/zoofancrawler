import json
import sqlite3
from datetime import datetime, timezone

import pytest

from zoofan.events import CrawlEvent, CrawlEventRecorder
from zoofan.models import Article, CrawlRun, CrawlRunStat, CrawlZooResult, Source, Zoo
from zoofan.storage import SQLiteStorage


def _seed_run_graph(storage, run_id="run-graph", zoo_id="zoo-1", source_id="source-1", article_id="article-1"):
    zoo = storage.upsert_zoo(Zoo(id=zoo_id, slug=zoo_id, name=zoo_id))
    source = storage.upsert_source(
        Source(id=source_id, zoo_id=zoo.id, url=f"https://example.org/{source_id}")
    )
    run = storage.start_crawl_run(CrawlRun(id=run_id))
    article = storage.upsert_article(
        Article(id=article_id, canonical_url=f"https://example.org/{article_id}")
    )
    return run, zoo, source, article


def test_event_schema_is_created_with_auto_id_and_query_indexes():
    with SQLiteStorage() as storage:
        columns = {
            row["name"]: row
            for row in storage.connection.execute("PRAGMA table_info(crawl_run_events)")
        }
        assert columns["id"]["pk"] == 1
        assert columns["id"]["type"].upper() == "INTEGER"
        assert set(columns) == {
            "id",
            "run_id",
            "zoo_id",
            "source_id",
            "created_at",
            "level",
            "component",
            "event_type",
            "message",
            "metadata_json",
        }
        indexes = {
            row["name"]
            for row in storage.connection.execute("PRAGMA index_list(crawl_run_events)")
        }
        assert {
            "idx_crawl_run_events_run_id_id",
            "idx_crawl_run_events_run_scope_id",
            "idx_crawl_run_events_zoo",
            "idx_crawl_run_events_source",
            "idx_crawl_run_events_created_at",
        } <= indexes

        run_columns = {
            row["name"]
            for row in storage.connection.execute("PRAGMA table_info(crawl_runs)")
        }
        assert {
            "heartbeat_at",
            "progress_at",
            "current_phase",
            "current_zoo_id",
            "current_source_id",
            "progress_json",
            "stop_reason",
        } <= run_columns
        relation_columns = {
            row["name"]
            for row in storage.connection.execute("PRAGMA table_info(crawl_run_articles)")
        }
        assert {
            "id",
            "run_id",
            "article_id",
            "zoo_id",
            "source_id",
            "outcome",
            "created_at",
            "updated_at",
            "metadata_json",
        } <= relation_columns
        assert "stop_reason" in {
            row["name"]
            for row in storage.connection.execute("PRAGMA table_info(crawl_run_stats)")
        }
        assert "stop_reason" in {
            row["name"]
            for row in storage.connection.execute("PRAGMA table_info(crawl_zoo_results)")
        }


def test_event_insert_recorder_and_after_id_pagination():
    with SQLiteStorage() as storage:
        _seed_run_graph(storage, run_id="run-1")
        recorder = CrawlEventRecorder(storage)
        first = recorder.emit(
            run_id="run-1",
            zoo_id="zoo-1",
            source_id="source-1",
            created_at="2026-08-16T10:00:00+00:00",
            level="info",
            component="discovery",
            event_type="source_started",
            message="Source started",
            metadata={"discovered": 0},
        )
        second = storage.record_crawl_event(
            CrawlEvent(
                run_id="run-1",
                created_at="2026-08-16T10:00:01+00:00",
                level="WARNING",
                component="crawler",
                event_type="crawl_budget_hit",
                message="Article limit reached",
                metadata={"limit": 50},
            )
        )
        third = recorder.record_event(
            run_id="run-1",
            event_type="crawl_completed",
            message="Crawl completed",
        )

        assert [first.id, second.id, third.id] == sorted([first.id, second.id, third.id])
        assert first.id < second.id < third.id
        assert first.level == "INFO"
        assert first.metadata == {"discovered": 0}
        assert first.created_at == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)

        first_page = storage.list_crawl_run_events("run-1", limit=2)
        assert [event.id for event in first_page] == [first.id, second.id]
        next_page = storage.list_crawl_run_events(
            "run-1", after_id=first_page[-1].id, limit=2
        )
        assert [event.id for event in next_page] == [third.id]
        offset_page = storage.list_crawl_run_events("run-1", limit=1, offset=1)
        assert [event.id for event in offset_page] == [second.id]
        assert [event.event_type for event in storage.list_crawl_events("run-1", event_type="crawl_completed")] == [
            "crawl_completed"
        ]


def test_event_metadata_is_safe_and_malformed_values_do_not_abort_insert():
    class FakeResponse:
        status_code = 503
        url = "https://example.org/article"
        content = b"<html>private response body</html>"

        def __repr__(self):
            return "FakeResponse(<html>private response body</html>)"

    cyclic = {}
    cyclic["self"] = cyclic
    with SQLiteStorage() as storage:
        _seed_run_graph(storage, run_id="run-safe")
        event = CrawlEventRecorder(storage).emit(
            run_id="run-safe",
            event_type="source_failed",
            message="Source failed",
            metadata={
                "exception": ValueError("bad parser"),
                "html": "<html>secret body</html>",
                "response": FakeResponse(),
                "not_json": object(),
                "nan": float("nan"),
                "cycle": cyclic,
                "token": "do-not-persist",
                "password": ValueError("password=do-not-persist"),
                "url": "https://example.org/article?token=secret&email=user@example.org",
            },
        )
        row = storage.connection.execute(
            "SELECT metadata_json FROM crawl_run_events WHERE id=?", (event.id,)
        ).fetchone()
        persisted = json.loads(row["metadata_json"])

        assert event.id is not None
        assert persisted["exception"]["__type__"].endswith("ValueError")
        assert persisted["html"]["__omitted__"] == "html"
        assert persisted["response"]["status_code"] == 503
        assert persisted["not_json"]["__type__"].endswith("object")
        assert persisted["nan"] is None
        assert persisted["cycle"]["self"]["__omitted__"] == "cyclic"
        assert persisted["token"]["__omitted__"] == "secret"
        assert persisted["password"]["__omitted__"] == "secret"
        assert "token=secret" not in row["metadata_json"]
        assert "?token" not in persisted["url"]
        assert "private response body" not in row["metadata_json"]

        # A malformed root is graceful too: the event remains durable with a
        # valid object-shaped metadata payload.
        fallback = storage.record_crawl_event(
            run_id="run-safe",
            event_type="metadata_invalid",
            metadata=["not", "an", "object"],
        )
        assert fallback.metadata == {}
        assert json.loads(
            storage.connection.execute(
                "SELECT metadata_json FROM crawl_run_events WHERE id=?", (fallback.id,)
            ).fetchone()["metadata_json"]
        ) == {}


def test_run_article_relation_is_idempotent_and_queryable_without_time_inference():
    with SQLiteStorage() as storage:
        _seed_run_graph(storage, run_id="run-articles", article_id="article-1")
        storage.upsert_source(
            Source(id="source-2", zoo_id="zoo-1", url="https://example.org/source-2")
        )
        first = storage.record_crawl_run_article(
            run_id="run-articles",
            article_id="article-1",
            zoo_id="zoo-1",
            source_id=None,
            outcome="stored",
            created_at="2026-08-16T10:00:00+00:00",
            metadata={"title": "First"},
        )
        repeated = storage.upsert_crawl_run_article(
            run_id="run-articles",
            article_id="article-1",
            zoo_id="zoo-1",
            source_id=None,
            outcome="duplicate",
            metadata={"title": "First", "reason": "known"},
        )
        other_source = storage.record_crawl_run_article(
            run_id="run-articles",
            article_id="article-1",
            source_id="source-2",
            outcome="stored",
            created_at="2026-08-16T10:00:02+00:00",
        )

        assert repeated.id == first.id
        assert repeated.zoo_id == "zoo-1"
        assert repeated.outcome == "duplicate"
        assert repeated.metadata["reason"] == "known"
        assert other_source.id != first.id
        relations = storage.list_crawl_run_articles("run-articles")
        assert [(item.article_id, item.source_id, item.outcome) for item in relations] == [
            ("article-1", None, "duplicate"),
            ("article-1", "source-2", "stored"),
        ]
        assert [item.zoo_id for item in storage.list_crawl_run_articles(
            "run-articles", zoo_id="zoo-1"
        )] == ["zoo-1", "zoo-1"]
        assert [item.id for item in storage.list_crawl_run_articles(
            "run-articles", after_id=first.id
        )] == [other_source.id]


def test_run_article_zoo_id_uses_source_fallback_and_is_part_of_identity():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="zoo-fallback", slug="fallback", name="Fallback"))
        source = storage.upsert_source(
            Source(id="source-fallback", zoo_id=zoo.id, url="https://example.org/feed")
        )
        storage.start_crawl_run(CrawlRun(id="run-fallback"))
        storage.upsert_article(
            Article(id="article-1", canonical_url="https://example.org/article-1")
        )
        storage.upsert_zoo(Zoo(id="other-zoo", slug="other", name="Other Zoo"))

        fallback = storage.record_crawl_run_article(
            run_id="run-fallback",
            article_id="article-1",
            source_id=source.id,
        )
        explicit = storage.record_crawl_run_article(
            run_id="run-fallback",
            article_id="article-1",
            zoo_id="other-zoo",
            source_id=source.id,
        )

        assert fallback.zoo_id == zoo.id
        assert explicit.zoo_id == "other-zoo"
        assert explicit.id != fallback.id
        assert storage.connection.execute(
            "SELECT zoo_id FROM crawl_run_articles WHERE id=?", (fallback.id,)
        ).fetchone()["zoo_id"] == zoo.id


def test_stats_and_zoo_results_persist_first_class_stop_reason_with_legacy_fallback():
    with SQLiteStorage() as storage:
        zoo = storage.upsert_zoo(Zoo(id="zoo-stop", slug="stop", name="Stop"))
        run = storage.start_crawl_run(CrawlRun(id="run-stop"))

        stat = storage.record_run_stat(
            CrawlRunStat(
                id="stat-stop",
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                status="stopped",
                metadata={"stop_reason": "legacy_budget"},
            )
        )
        assert stat.stop_reason == "legacy_budget"
        assert storage.connection.execute(
            "SELECT stop_reason FROM crawl_run_stats WHERE id='stat-stop'"
        ).fetchone()["stop_reason"] == "legacy_budget"

        updated_stat = storage.record_run_stat(
            {
                "id": "stat-stop",
                "crawl_run_id": run.id,
                "zoo_id": zoo.id,
                "status": "stopped",
                "stop_reason": "explicit_limit",
                "metadata": {"stop_reason": "legacy_budget"},
            }
        )
        assert updated_stat.stop_reason == "explicit_limit"
        assert len(storage.list_run_stats(run.id)) == 1

        result = storage.upsert_zoo_run_result(
            CrawlZooResult(
                id="result-stop",
                run_id=run.id,
                zoo_id=zoo.id,
                status="stopped",
                metadata={"stop_reason": "legacy_timeout"},
            )
        )
        assert result.stop_reason == "legacy_timeout"
        updated_result = storage.upsert_zoo_run_result(
            {
                "id": "result-stop",
                "run_id": run.id,
                "zoo_id": zoo.id,
                "status": "stopped",
                "stop_reason": "explicit_shutdown",
                "metadata": {"stop_reason": "legacy_timeout"},
            }
        )
        assert updated_result.stop_reason == "explicit_shutdown"
        assert len(storage.get_zoo_run_results(run.id)) == 1


def test_active_run_state_has_heartbeat_progress_and_structured_stop_reason():
    with SQLiteStorage() as storage:
        run = storage.start_crawl_run(CrawlRun(id="state-run"))
        assert run.heartbeat_at is None
        assert run.progress_at is None
        assert run.current_phase is None
        assert run.progress is None

        updated = storage.update_crawl_run_state(
            run.id,
            heartbeat_at="2026-08-16T10:01:00+00:00",
            current_zoo="zoo-1",
            current_source="source-1",
            current_phase="article",
            progress={"discovered": 12, "fetched": 4, "stored": 2},
        )
        assert updated and updated.current_zoo_id == "zoo-1"
        assert updated.current_source == "source-1"
        assert updated.current_phase == "article"
        assert updated.progress_at is not None
        assert updated.progress == {"discovered": 12, "fetched": 4, "stored": 2}
        assert storage.get_active_crawl_run().id == run.id
        assert storage.get_crawl_run_state(run.id)["progress"]["stored"] == 2

        finished = storage.finish_crawl_run(
            run.id,
            status="warning",
            stop_reason="article_limit",
            finished_at="2026-08-16T10:02:00+00:00",
        )
        assert finished and finished.stop_reason == "article_limit"
        assert storage.get_active_crawl_run() is None


def test_legacy_database_gets_event_table_and_migration_is_idempotent():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE crawl_runs(id TEXT PRIMARY KEY, batch_id TEXT);
        INSERT INTO crawl_runs VALUES('legacy-run', 'legacy-batch');
        """
    )

    storage = SQLiteStorage(connection=connection)
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key='version'"
    ).fetchone()[0] == str(SQLiteStorage.SCHEMA_VERSION)
    assert connection.execute(
        "SELECT COUNT(*) FROM crawl_run_events"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM crawl_run_articles"
    ).fetchone()[0] == 0
    run_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(crawl_runs)")
    }
    assert {"heartbeat_at", "progress_at", "current_phase", "progress_json", "stop_reason"} <= run_columns

    storage.record_crawl_event(
        run_id="legacy-run",
        event_type="migration_check",
        metadata={"ok": True},
    )
    before = connection.execute(
        "SELECT id,run_id,event_type,metadata_json FROM crawl_run_events"
    ).fetchall()
    storage.create_schema()
    after = connection.execute(
        "SELECT id,run_id,event_type,metadata_json FROM crawl_run_events"
    ).fetchall()

    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert storage.get_crawl_run("legacy-run") is not None
    storage.close()


def test_legacy_stats_results_and_relations_gain_new_columns_idempotently():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE crawl_runs(id TEXT PRIMARY KEY, batch_id TEXT);
        CREATE TABLE zoos(id TEXT PRIMARY KEY, slug TEXT, name TEXT);
        CREATE TABLE sources(id TEXT PRIMARY KEY, zoo_id TEXT, url TEXT);
        CREATE TABLE crawl_run_stats(
            id TEXT PRIMARY KEY, crawl_run_id TEXT, zoo_id TEXT, source_id TEXT,
            status TEXT, discovered_count INTEGER, fetched_count INTEGER,
            stored_count INTEGER, already_known_count INTEGER,
            duplicate_candidate_count INTEGER, error_count INTEGER,
            started_at TEXT, finished_at TEXT, duration_ms INTEGER, error TEXT,
            errors_json TEXT, metadata_json TEXT
        );
        CREATE TABLE crawl_zoo_results(
            id TEXT PRIMARY KEY, crawl_run_id TEXT NOT NULL, zoo_id TEXT NOT NULL,
            zoo_slug TEXT, zoo_name TEXT, status TEXT, source_status TEXT,
            discovered INTEGER, parsed INTEGER, inserted INTEGER, updated INTEGER,
            failed INTEGER, duplicate_filtered INTEGER, duration_ms INTEGER,
            source_url TEXT, http_status INTEGER, error_category TEXT,
            error_summary TEXT, started_at TEXT, finished_at TEXT,
            metadata_json TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE crawl_run_articles(
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, article_id TEXT,
            source_id TEXT, outcome TEXT, created_at TEXT, updated_at TEXT,
            metadata_json TEXT
        );
        CREATE TABLE articles(id TEXT PRIMARY KEY);
        INSERT INTO crawl_runs VALUES('legacy-run', 'legacy-batch');
        INSERT INTO zoos VALUES('legacy-zoo', 'legacy', 'Legacy Zoo');
        INSERT INTO sources VALUES('legacy-source', 'legacy-zoo', 'https://example.org/feed');
        INSERT INTO articles VALUES('legacy-article');
        INSERT INTO crawl_run_stats(
            id,crawl_run_id,zoo_id,source_id,status,metadata_json
        ) VALUES('legacy-stat','legacy-run','legacy-zoo','legacy-source','stopped',
                  '{"stop_reason":"legacy_stat_reason"}');
        INSERT INTO crawl_zoo_results(
            id,crawl_run_id,zoo_id,status,metadata_json
        ) VALUES('legacy-result','legacy-run','legacy-zoo','stopped',
                 '{"stop_reason":"legacy_result_reason"}');
        INSERT INTO crawl_run_articles(
            run_id,article_id,source_id,outcome,created_at,metadata_json
        ) VALUES('legacy-run','legacy-article','legacy-source','stored',
                 '2026-08-16T10:00:00+00:00','{}');
        INSERT INTO crawl_run_articles(
            run_id,article_id,source_id,outcome,created_at,metadata_json
        ) VALUES('legacy-run','legacy-article','legacy-source','already_known',
                 '2026-08-16T10:01:00+00:00','{"legacy_duplicate":true}');
        """
    )

    storage = SQLiteStorage(connection=connection)
    assert storage.get_run_stat("legacy-stat").stop_reason == "legacy_stat_reason"
    assert storage.get_zoo_run_result("legacy-run", "legacy-zoo").stop_reason == "legacy_result_reason"
    relation = storage.list_crawl_run_articles("legacy-run")[0]
    assert relation.zoo_id == "legacy-zoo"
    assert relation.outcome == "already_known"
    assert relation.metadata["legacy_duplicate"] is True
    assert len(storage.list_crawl_run_articles("legacy-run")) == 1

    storage.create_schema()
    assert storage.get_run_stat("legacy-stat").stop_reason == "legacy_stat_reason"
    assert storage.get_zoo_run_result("legacy-run", "legacy-zoo").stop_reason == "legacy_result_reason"
    assert storage.list_crawl_run_articles("legacy-run")[0].zoo_id == "legacy-zoo"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    storage.close()


def test_console_run_graph_foreign_keys_reject_orphans_and_cascade_children():
    with SQLiteStorage() as storage:
        run, zoo, source, article = _seed_run_graph(storage)
        with pytest.raises(sqlite3.IntegrityError):
            storage.record_crawl_event(run_id="missing-run", event_type="orphan")
        with pytest.raises(sqlite3.IntegrityError):
            storage.record_crawl_run_article(
                run_id=run.id, article_id="missing-article", zoo_id=zoo.id
            )

        storage.record_crawl_event(
            run_id=run.id, zoo_id=zoo.id, source_id=source.id, event_type="child"
        )
        storage.record_crawl_run_article(
            run_id=run.id,
            article_id=article.id,
            zoo_id=zoo.id,
            source_id=source.id,
            outcome="already_known",
        )
        storage.record_run_stat(
            CrawlRunStat(crawl_run_id=run.id, zoo_id=zoo.id, source_id=source.id)
        )
        storage.upsert_zoo_run_result(
            CrawlZooResult(run_id=run.id, zoo_id=zoo.id)
        )
        storage.connection.execute("DELETE FROM crawl_runs WHERE id=?", (run.id,))
        for table in ("crawl_run_events", "crawl_run_articles", "crawl_run_stats", "crawl_zoo_results"):
            assert storage.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE "
                + ("run_id=?" if table == "crawl_run_events" else "crawl_run_id=?" if table in {"crawl_run_stats", "crawl_zoo_results"} else "run_id=?"),
                (run.id,),
            ).fetchone()[0] == 0

        run2, zoo2, source2, article2 = _seed_run_graph(
            storage, run_id="run-article-delete", zoo_id="zoo-2", source_id="source-3", article_id="article-2"
        )
        storage.record_crawl_run_article(
            run_id=run2.id, article_id=article2.id, zoo_id=zoo2.id, source_id=source2.id
        )
        storage.connection.execute("DELETE FROM articles WHERE id=?", (article2.id,))
        assert storage.list_crawl_run_articles(run2.id) == []
