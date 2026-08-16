import json

from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.fetcher import RobotsDisallowed
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.storage import SQLiteStorage


class RecordingSink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(dict(event))


class FixtureFetcher:
    supports_request_policy = True

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, url, *, request_policy):
        assert request_policy.validate(url) == url
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, FetchResponse):
            return value
        return FetchResponse(url, 200, value.encode("utf-8"))


def _setup(*, article_count=1, second_source=False):
    zoo = Zoo(
        id="events-zoo",
        slug="events-zoo",
        name="Events Zoo",
        website_url="https://events.example/",
    )
    sources = [
        Source(
            id="events-source",
            zoo_id=zoo.id,
            kind="rss",
            url="https://events.example/feed",
            config={"official_host": "events.example", "allow_regex": r"/news/"},
        )
    ]
    if second_source:
        sources.insert(
            0,
            Source(
                id="blocked-source",
                zoo_id=zoo.id,
                kind="rss",
                url="https://events.example/blocked",
                config={"official_host": "events.example", "allow_regex": r"/news/"},
            ),
        )
    article_urls = [f"https://events.example/news/{index}" for index in range(article_count)]
    feed_url = sources[-1].url
    payloads = {
        feed_url: "<rss><channel>"
        + "".join(f"<item><link>{url}</link></item>" for url in article_urls)
        + "</channel></rss>",
    }
    for url in article_urls:
        payloads[url] = f"<html><head><title>{url}</title></head><article>Body</article></html>"
    if second_source:
        payloads[sources[0].url] = RobotsDisallowed("robots.txt disallows source")
    return CrawlerConfig(zoos=[zoo], sources=sources, request_delay=0), FixtureFetcher(payloads), zoo, sources


def _event_types(sink):
    return [event["event_type"] for event in sink.events]


def test_success_emits_lifecycle_and_exact_run_article_evidence():
    config, fetcher, zoo, sources = _setup()
    storage = SQLiteStorage(":memory:")
    sink = RecordingSink()

    result = Crawler(config, storage=storage, fetcher=fetcher, event_sink=sink).crawl()

    assert result.status == "completed"
    assert result.stop_reason == "exhausted"
    assert result.stored_count == 1
    assert "crawl_started" in _event_types(sink)
    assert "source_started" in _event_types(sink)
    assert "discovery_completed" in _event_types(sink)
    assert "article_stored" in _event_types(sink)
    assert "source_completed" in _event_types(sink)
    assert "zoo_completed" in _event_types(sink)
    assert "crawl_completed" in _event_types(sink)
    stored = [event for event in sink.events if event["event_type"] == "article_stored"]
    assert stored[0]["metadata"]["created"] is True
    assert stored[0]["zoo_id"] == zoo.id
    assert stored[0]["source_id"] == sources[0].id

    article_id = storage.list_articles()[0].id
    relations = storage.list_crawl_run_articles(result.run.id)
    assert [(item.article_id, item.outcome) for item in relations] == [
        (article_id, "stored")
    ]
    assert result.run.metadata["stored_article_ids"] == [article_id]
    assert result.run.metadata["article_ids"] == [article_id]
    durable_run = storage.get_crawl_run(result.run.id)
    assert durable_run.metadata["stored_article_ids"] == [article_id]
    assert durable_run.metadata["stop_reason"] == "exhausted"
    assert result.zoo_results[0].metadata["stop_reason"] == "exhausted"
    assert result.stats[0].metadata["stop_reason"] == "exhausted"
    for event in sink.events:
        json.dumps(event, allow_nan=False)


def test_dedupe_emits_event_and_does_not_reassociate_old_article_as_new():
    config, fetcher, _zoo, _sources = _setup()
    storage = SQLiteStorage(":memory:")
    sink = RecordingSink()
    crawler = Crawler(config, storage=storage, fetcher=fetcher, event_sink=sink)

    first = crawler.crawl()
    sink.events.clear()
    second = crawler.crawl()

    assert first.stored_count == 1
    assert second.stored_count == 0
    assert second.already_known_count == 1
    assert second.stop_reason == "no_new_urls"
    assert not [event for event in sink.events if event["event_type"] == "article_stored"]
    duplicate = [event for event in sink.events if event["event_type"] == "article_duplicate"]
    assert duplicate
    assert duplicate[0]["metadata"]["reason"] == "already_known"
    assert second.run.metadata.get("stored_article_ids", []) == []
    relations = storage.list_crawl_run_articles(second.run.id)
    assert [(item.article_id, item.outcome) for item in relations] == [
        (storage.list_articles()[0].id, "already_known")
    ]


def test_recent_recheck_records_updated_article_outcome_without_new_article_row():
    config, fetcher, _zoo, _sources = _setup()
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(
        config,
        storage=storage,
        fetcher=fetcher,
        event_sink=RecordingSink(),
        recent_recheck=1,
    )

    first = crawler.crawl()
    fetcher.payloads["https://events.example/news/0"] = (
        "<html><head><title>Changed</title></head>"
        "<article>Changed body</article></html>"
    )
    second = crawler.crawl()

    assert first.stored_count == 1
    assert second.updated_count == 1
    assert len(storage.list_articles()) == 1
    relations = storage.list_crawl_run_articles(second.run.id)
    assert len(relations) == 1
    assert relations[0].outcome == "updated"


def test_candidate_budget_is_structured_in_source_zoo_run_and_event():
    config, fetcher, _zoo, _sources = _setup(article_count=2)
    storage = SQLiteStorage(":memory:")
    sink = RecordingSink()

    result = Crawler(config, storage=storage, fetcher=fetcher, event_sink=sink).crawl(
        max_candidates_per_source=1
    )

    assert result.stored_count == 1
    assert result.stop_reason == "url_discovery_limit"
    assert result.stats[0].metadata["stop_reason"] == "url_discovery_limit"
    assert result.zoo_results[0].metadata["stop_reason"] == "url_discovery_limit"
    budget_events = [event for event in sink.events if event["event_type"] == "crawl_budget_hit"]
    assert len(budget_events) == 1
    assert budget_events[0]["level"] == "WARNING"
    assert budget_events[0]["metadata"]["stop_reason"] == "url_discovery_limit"


def test_source_failure_emits_major_failure_and_other_source_continues():
    config, fetcher, _zoo, sources = _setup(second_source=True)
    storage = SQLiteStorage(":memory:")
    sink = RecordingSink()

    result = Crawler(config, storage=storage, fetcher=fetcher, event_sink=sink).crawl()

    assert result.status == "completed_with_errors"
    assert result.stop_reason == "robots_blocked"
    assert any(stat.source_id == "blocked-source" and stat.status == "error" for stat in result.stats)
    assert any(stat.source_id == "events-source" and stat.stored_count == 1 for stat in result.stats)
    failed = [event for event in sink.events if event["event_type"] == "source_failed"]
    assert failed and failed[0]["source_id"] == sources[0].id
    robots = [event for event in sink.events if event["event_type"] == "robots_blocked"]
    assert robots and robots[0]["metadata"]["stop_reason"] == "robots_blocked"
    assert any(event["event_type"] == "article_stored" for event in sink.events)


def test_event_sink_failure_is_fail_soft_and_preserves_source_isolation():
    config, fetcher, _zoo, sources = _setup(second_source=True)
    storage = SQLiteStorage(":memory:")

    class FailingSink:
        def __init__(self):
            self.calls = 0

        def emit(self, _event):
            self.calls += 1
            raise RuntimeError("event persistence failed")

        def record(self, _event):
            self.calls += 1
            raise RuntimeError("event persistence failed")

    sink = FailingSink()

    result = Crawler(config, storage=storage, fetcher=fetcher, event_sink=sink).crawl()

    assert sink.calls
    assert result.status == "completed_with_errors"
    assert result.stop_reason == "robots_blocked"
    assert result.stored_count == 1
    assert any(
        stat.source_id == sources[0].id and stat.status == "error"
        for stat in result.stats
    )
    assert any(
        stat.source_id == sources[1].id
        and stat.status == "completed"
        and stat.stored_count == 1
        for stat in result.stats
    )

    articles = storage.list_articles()
    assert len(articles) == 1
    relations = storage.list_crawl_run_articles(result.run.id)
    assert [(item.article_id, item.outcome) for item in relations] == [
        (articles[0].id, "stored")
    ]
    assert result.run.metadata["stored_article_ids"] == [articles[0].id]


def test_explicit_run_article_relation_hook_receives_each_article_outcome():
    config, fetcher, zoo, sources = _setup()

    class RelationStorage(SQLiteStorage):
        def __init__(self):
            super().__init__(":memory:")
            self.relations = []

        def record_crawl_run_article(
            self,
            run_id,
            article_id,
            zoo_id=None,
            source_id=None,
            outcome=None,
            created_at=None,
            metadata=None,
        ):
            self.relations.append(
                {
                    "run_id": run_id,
                    "article_id": article_id,
                    "zoo_id": zoo_id,
                    "source_id": source_id,
                    "outcome": outcome,
                    "created_at": created_at,
                    "metadata": metadata,
                }
            )

    storage = RelationStorage()
    crawler = Crawler(config, storage=storage, fetcher=fetcher, event_sink=RecordingSink())

    first = crawler.crawl()
    second = crawler.crawl()

    assert len(storage.relations) == 2
    relation = storage.relations[0]
    assert relation["run_id"] == first.run.id
    assert relation["article_id"] == storage.list_articles()[0].id
    assert relation["zoo_id"] == zoo.id
    assert relation["source_id"] == sources[0].id
    assert relation["outcome"] == "stored"
    assert storage.relations[1]["outcome"] == "already_known"
    assert second.run.metadata.get("stored_article_ids", []) == []
