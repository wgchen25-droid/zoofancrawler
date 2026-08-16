import sqlite3

from zoofan.console_repository import ConsoleRepository, parse_metadata
from zoofan.console_service import ConsoleService
from zoofan.models import Article, CrawlRun, CrawlRunStat, CrawlZooResult, Source, Zoo
from zoofan.storage import SQLiteStorage


NOW = "2026-08-16T12:00:00+00:00"


def _seed_base(storage):
    zoo = storage.upsert_zoo(
        Zoo(id="z1", slug="alpha", name="Alpha Zoo", country_code="DE")
    )
    source = storage.upsert_source(
        Source(id="s1", zoo_id=zoo.id, url="https://alpha.example/news", name="News")
    )
    return zoo, source


def test_status_projection_table_covers_terminal_and_liveness_contract():
    cases = [
        ("completed", "success", "Success"), ("warning", "warning", "Warning"),
        ("failed", "failed", "Failed"), ("running", "running", "Running"),
        ("stale", "stale", "Stale"), ("mystery", "unknown", "Unknown"),
        ("completed", "budget_hit", "Budget Hit"),
    ]
    for raw, key, label in cases:
        value = {"status": raw, "stop_reason": "article_limit" if key == "budget_hit" else None,
                 "active": key == "running", "active_run": {"id": "r"},
                 "poll_hint": {"enabled": True}}
        projected = ConsoleService.json_ready(value)
        assert projected["classified_status"] == key
        assert projected["status_key"] == key
        assert projected["display_status"] == key
        assert projected["status_label"] == label
        assert projected["raw_status"] == raw
        assert projected["run_status"] == raw
        assert projected["terminal_status"] == (raw if key in {"success", "warning", "failed", "budget_hit"} else None)
        if key != "running":
            assert projected["active"] is False
            assert projected["active_run"] is None
            assert projected["poll_hint"]["enabled"] is False


def test_no_runs_and_missing_optional_tables_are_explicitly_unknown():
    with SQLiteStorage(":memory:") as storage:
        service = ConsoleService(ConsoleRepository(storage))

        status = service.get_current_status(now=NOW)
        assert status["status"] == "unknown"
        assert status["active"] is False
        assert status["observability"] == {"observable": False, "reason": "no_runs"}

        run = storage.start_crawl_run(
            CrawlRun(id="run-old", started_at="2026-08-15T12:00:00+00:00", status="completed")
        )
        storage.upsert_article(
            Article(id="a-old", url="https://alpha.example/old", title="Old"),
        )
        # A discovery timestamp is deliberately present.  It must not be
        # mistaken for proof that the article belongs to this run.
        storage.connection.execute(
            "INSERT INTO article_discoveries(id,article_id,source_id,discovered_url,discovered_at) "
            "VALUES('d-old','a-old',NULL,'https://alpha.example/old',?)",
            ("2026-08-15T12:01:00+00:00",),
        )
        articles = service.get_run_articles(run.id)
        assert articles["items"] == []
        assert articles["pagination"]["total"] == 0
        # The current storage schema has the relation table, but no link for
        # this run.  It is therefore observable and correctly empty rather
        # than inferred from the old discovery timestamp.
        assert articles["observability"] == {
            "observable": True,
            "reason": None,
            "relation": "crawl_run_articles",
        }


def test_legacy_schema_without_run_article_relation_is_empty_and_unobservable():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE crawl_runs (id TEXT PRIMARY KEY, status TEXT, started_at TEXT);
        CREATE TABLE articles (id TEXT PRIMARY KEY, url TEXT, title TEXT, created_at TEXT);
        INSERT INTO crawl_runs VALUES ('legacy-run', 'completed', '2026-08-15T12:00:00+00:00');
        INSERT INTO articles VALUES ('legacy-article', 'https://example.test/article', 'Article', '2026-08-15T12:01:00+00:00');
        """
    )
    try:
        service = ConsoleService(ConsoleRepository(connection))
        status = service.get_current_status(now=NOW)
        # A legacy terminal row has no heartbeat or activity evidence.  The
        # console must not manufacture a placeholder current activity DTO.
        assert status["current_activity"] is None
        assert status["current_run"]["current_activity"] is None
        articles = service.get_run_articles("legacy-run")
        assert articles["items"] == []
        assert articles["pagination"]["total"] == 0
        assert articles["observability"] == {
            "observable": False,
            "reason": "run_article_relation_unavailable",
            "relation": "run_article_relation_unavailable",
        }
        assert service.get_run_events("legacy-run")["items"] == []
    finally:
        connection.close()


def test_running_without_heartbeat_or_lease_is_unknown_and_not_active():
    with SQLiteStorage(":memory:") as storage:
        run = storage.start_crawl_run(
            CrawlRun(
                id="legacy-running",
                started_at="2026-08-16T11:00:00+00:00",
                status="running",
            )
        )
        storage.connection.execute(
            "UPDATE crawl_runs SET heartbeat_at=NULL WHERE id=?",
            (run.id,),
        )
        storage.connection.execute("DELETE FROM crawler_leases")
        repository = ConsoleRepository(storage)
        service = ConsoleService(repository)

        status = service.get_current_status(now=NOW)
        assert status["status"] == "unknown"
        assert status["active"] is False
        assert status["status_observable"] is False
        assert status["status_reason"] == "no_liveness_evidence"
        assert status["current_run"]["run_status"] == "running"
        assert service.get_active_run(now=NOW) is None
        assert repository.list_runs(status="running", now=NOW) == []
        assert [row["id"] for row in repository.list_runs(status="unknown", now=NOW)] == [run.id]


def test_current_status_prefers_older_live_run_over_newer_finished_run():
    with SQLiteStorage(":memory:") as storage:
        active = storage.start_crawl_run(
            CrawlRun(
                id="active-run",
                started_at="2026-08-16T10:00:00+00:00",
                status="running",
            )
        )
        storage.connection.execute(
            "UPDATE crawl_runs SET heartbeat_at=? WHERE id=?",
            (NOW, active.id),
        )
        finished = storage.start_crawl_run(
            CrawlRun(
                id="newer-finished",
                started_at="2026-08-16T11:59:00+00:00",
                status="running",
            )
        )
        storage.finish_crawl_run(
            finished.id,
            status="completed",
            finished_at=NOW,
        )
        service = ConsoleService(ConsoleRepository(storage))

        current = service.get_current_status(now=NOW)
        assert current["run_id"] == active.id
        assert current["status"] == "running"
        assert current["active"] is True

        finished_rows = service.list_runs(status="finished")
        assert [row["id"] for row in finished_rows["runs"]] == [finished.id]
        raw_rows = service.list_runs(status="completed")
        assert [row["id"] for row in raw_rows["runs"]] == [finished.id]


def test_current_status_distinguishes_running_stale_finished_and_unknown():
    with SQLiteStorage(":memory:") as storage:
        run = storage.start_crawl_run(
            CrawlRun(id="run-state", started_at="2026-08-16T11:59:00+00:00", status="running")
        )
        storage.connection.execute(
            "UPDATE crawl_runs SET heartbeat_at=?,current_zoo_id=?,current_source_id=?,progress_json=? WHERE id=?",
            (NOW, "z1", "s1", '{"completed": 2, "total": 4}', run.id),
        )
        storage.connection.execute(
            "INSERT OR REPLACE INTO crawler_leases(name,owner,acquired_at,lease_until,heartbeat_at) "
            "VALUES('crawler','test','2026-08-16T11:59:00+00:00',?,?)",
            ("2026-08-16T12:05:00+00:00", NOW),
        )
        service = ConsoleService(ConsoleRepository(storage))

        running = service.get_current_status(now=NOW)
        assert running["status"] == "running"
        assert running["active"] is True
        assert running["current_run"]["run_status"] == "running"
        assert running["current_run"]["heartbeat_at"] == NOW
        assert running["current_activity"] == {
            "current_zoo_id": "z1",
            "current_source_id": "s1",
            "progress": {"completed": 2, "total": 4},
            "status": "running",
            "stop_reason": None,
        }
        assert running["current_run"]["current_activity"] == running["current_activity"]

        storage.connection.execute(
            "UPDATE crawl_runs SET heartbeat_at=? WHERE id=?",
            ("2026-08-16T11:00:00+00:00", run.id),
        )
        storage.connection.execute(
            "UPDATE crawler_leases SET heartbeat_at=?, lease_until=? WHERE name='crawler'",
            ("2026-08-16T11:00:00+00:00", "2026-08-16T11:30:00+00:00"),
        )
        stale = service.get_current_status(now=NOW, stale_after_seconds=300)
        assert stale["status"] == "stale"
        assert stale["active"] is False
        # The legacy lease has no run_id and must not be attributed to this
        # run; stale classification therefore comes from the run heartbeat.
        assert stale["status_reason"] == "heartbeat_expired"
        assert stale["current_run"]["stale"] is True

        storage.finish_crawl_run(run.id, status="failed", finished_at=NOW, error="403")
        finished = service.get_current_status(now=NOW)
        assert finished["status"] == "failed"
        assert finished["active"] is False
        assert finished["current_run"]["run_status"] == "failed"
        assert finished["current_run"]["terminal_status"] == "failed"

        unknown = storage.start_crawl_run(
            CrawlRun(id="run-unknown", started_at=NOW, status="mystery")
        )
        status = service.get_current_status(now=NOW)
        assert status["run_id"] == unknown.id
        assert status["status"] == "unknown"
        assert status["status_observable"] is False


def test_summary_results_articles_events_filters_and_pagination():
    with SQLiteStorage(":memory:") as storage:
        zoo, source = _seed_base(storage)
        run = storage.start_crawl_run(
            CrawlRun(
                id="run-1",
                batch_id="batch-1",
                started_at="2026-08-16T11:00:00+00:00",
                finished_at=NOW,
                status="completed",
                metadata={"limit": 50},
            )
        )
        storage.record_run_stat(
            CrawlRunStat(
                id="stat-1",
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                source_id=source.id,
                status="completed",
                discovered_count=4,
                fetched_count=3,
                stored_count=2,
                already_known_count=1,
                duplicate_candidate_count=1,
                error_count=0,
                started_at="2026-08-16T11:00:00+00:00",
                finished_at=NOW,
                metadata={"stop_reason": "exhausted"},
            )
        )
        storage.upsert_zoo_run_result(
            CrawlZooResult(
                id="zoo-result-1",
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                status="completed",
                discovered=4,
                parsed=3,
                inserted=2,
                duplicate_filtered=1,
                started_at="2026-08-16T11:00:00+00:00",
                finished_at=NOW,
            )
        )
        storage.upsert_article(
            Article(
                id="a1",
                url="https://alpha.example/a1",
                title="First",
                summary="Summary",
                language="de",
                image_url="https://alpha.example/a1.jpg",
                metadata={"image_count": 2},
            ),
            source_id=source.id,
        )
        storage.upsert_article(
            Article(id="a2", url="https://alpha.example/a2", title="Second"),
            source_id=source.id,
        )
        storage.connection.execute("DELETE FROM crawl_run_articles")
        storage.connection.executemany(
            "INSERT INTO crawl_run_articles(run_id,article_id,source_id,outcome,created_at,updated_at,metadata_json) "
            "VALUES (?, ?, ?, 'stored', ?, ?, '{}')",
            [
                (run.id, "a1", source.id, "2026-08-16T11:01:00+00:00", "2026-08-16T11:01:00+00:00"),
                (run.id, "a2", source.id, "2026-08-16T11:02:00+00:00", "2026-08-16T11:02:00+00:00"),
            ],
        )
        storage.connection.execute("DELETE FROM crawl_run_events")
        storage.connection.executemany(
            "INSERT INTO crawl_run_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1, run.id, zoo.id, source.id, NOW, "INFO", "crawler", "crawl_started", "started", '{"ok":true}'),
                (2, run.id, zoo.id, source.id, NOW, "WARNING", "fetcher", "retry_exhausted", "retry 3", "not-json"),
                (3, run.id, zoo.id, source.id, NOW, "ERROR", "fetcher", "http_error", "HTTP 403", '{"status_code":403}'),
            ],
        )

        repository = ConsoleRepository(storage)
        service = ConsoleService(repository)
        summary = service.get_run_summary(run.id)
        assert summary["stored"] == 2
        assert summary["duplicate"] == 1
        assert summary["stop_reason"] == "exhausted"
        assert service.get_run_zoo_results(run.id)[0]["zoo_name"] == "Alpha Zoo"
        assert service.get_run_source_results(run.id)[0]["source_name"] == "News"

        articles = service.get_run_articles(run.id, limit=1)
        assert articles["pagination"] == {
            "limit": 1,
            "offset": 0,
            "total": 2,
            "has_more": True,
            "next_offset": 1,
        }
        assert articles["items"][0]["id"] == "a2"
        assert articles["items"][0]["image_count"] in {0, 2}
        assert "raw_html" not in articles["items"][0]
        assert articles["observability"]["observable"] is True
        assert service.get_run_articles(run.id, limit=1, offset=1)["items"][0]["id"] == "a1"

        events = service.get_run_events(run.id, limit=2)
        assert [event["id"] for event in events["items"]] == [1, 2]
        assert events["pagination"]["total"] == 3
        assert events["items"][1]["metadata"] == {}
        assert service.get_run_events(run.id, level="ERROR")["items"][0]["id"] == 3
        assert service.get_run_events(run.id, text="403")["items"][0]["event_type"] == "http_error"
        assert [event["id"] for event in service.get_run_events(run.id, after_id=1)["items"]] == [2, 3]
        assert service.get_run_events(run.id, limit=1, offset=1)["items"][0]["id"] == 2
        assert service.get_run_events(run.id, component="missing")["items"] == []


def test_explicit_stop_reason_columns_override_upsert_metadata_fallback():
    with SQLiteStorage(":memory:") as storage:
        zoo, source = _seed_base(storage)
        run = storage.start_crawl_run(
            CrawlRun(
                id="run-stop-reason",
                started_at="2026-08-16T11:00:00+00:00",
                finished_at=NOW,
                status="completed",
            )
        )
        storage.record_run_stat(
            CrawlRunStat(
                id="stat-stop-reason",
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                source_id=source.id,
                status="completed",
                metadata={"stop_reason": "stats-metadata"},
            )
        )
        storage.upsert_zoo_run_result(
            CrawlZooResult(
                id="zoo-stop-reason",
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                status="completed",
                metadata={"stop_reason": "zoo-metadata"},
            )
        )

        fallback_service = ConsoleService(ConsoleRepository(storage))
        assert fallback_service.get_run_source_results(run.id)[0]["stop_reason"] == "stats-metadata"
        assert fallback_service.get_run_zoo_results(run.id)[0]["stop_reason"] == "zoo-metadata"

        stats_columns = {
            row[1]
            for row in storage.connection.execute(
                "PRAGMA table_info(crawl_run_stats)"
            ).fetchall()
        }
        zoo_result_columns = {
            row[1]
            for row in storage.connection.execute(
                "PRAGMA table_info(crawl_zoo_results)"
            ).fetchall()
        }
        if "stop_reason" not in stats_columns:
            storage.connection.execute(
                "ALTER TABLE crawl_run_stats ADD COLUMN stop_reason TEXT"
            )
        if "stop_reason" not in zoo_result_columns:
            storage.connection.execute(
                "ALTER TABLE crawl_zoo_results ADD COLUMN stop_reason TEXT"
            )
        storage.connection.execute(
            "UPDATE crawl_run_stats SET stop_reason=? WHERE id=?",
            ("stats-column", "stat-stop-reason"),
        )
        storage.connection.execute(
            "UPDATE crawl_zoo_results SET stop_reason=? WHERE id=?",
            ("zoo-column", "zoo-stop-reason"),
        )

        service = ConsoleService(ConsoleRepository(storage))
        assert service.get_run_source_results(run.id)[0]["stop_reason"] == "stats-column"
        assert service.get_run_zoo_results(run.id)[0]["stop_reason"] == "zoo-column"
        assert service.get_run_summary(run.id)["stop_reason"] == "stats-column"


def test_zoo_list_detail_history_and_missing_zoo_are_safe():
    with SQLiteStorage(":memory:") as storage:
        zoo, source = _seed_base(storage)
        run = storage.start_crawl_run(
            CrawlRun(id="run-zoo", started_at="2026-08-15T00:00:00+00:00", status="completed")
        )
        storage.record_run_stat(
            CrawlRunStat(
                crawl_run_id=run.id,
                zoo_id=zoo.id,
                source_id=source.id,
                status="failed",
                discovered_count=1,
                error_count=1,
                error="HTTP 403",
            )
        )
        service = ConsoleService(ConsoleRepository(storage))
        zoos = service.get_zoos()
        assert zoos[0]["id"] == zoo.id
        detail = service.get_zoo(zoo.slug)
        assert detail["id"] == zoo.id
        assert detail["sources"][0]["id"] == source.id
        history = service.get_zoo_history(zoo.id)
        assert history["pagination"]["total"] == 1
        assert history["items"][0]["errors"] == 1
        assert service.get_zoo("does-not-exist") is None
        assert service.get_zoo_history("does-not-exist")["items"] == []


def test_malformed_metadata_parser_is_json_ready_and_bounded():
    assert parse_metadata("not-json") == {}
    assert parse_metadata("[]") == {}
    assert parse_metadata('{"count": 2, "nested": [true, null]}') == {
        "count": 2,
        "nested": [True, None],
    }
    deep = "{" * 100 + "\"x\":1" + "}" * 100
    assert parse_metadata(deep) == {}
