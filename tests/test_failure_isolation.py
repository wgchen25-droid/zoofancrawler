import pytest

from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.storage import SQLiteStorage


class FailingOneFetcher:
    supports_request_policy = True

    def fetch(self, url, *, request_policy):
        assert request_policy.validate(url) == url
        if url.endswith("/bad"):
            raise RuntimeError("boom")
        if url.endswith("/good"):
            return FetchResponse(url, 200, b"<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>")
        return FetchResponse(url, 200, b"<html><head><meta property='og:title' content='One'/></head><article>Body</article></html>")


def _two_zoo_setup():
    first_zoo = Zoo(id="first-zoo", slug="first-zoo", name="First", website_url="https://first.example/")
    second_zoo = Zoo(id="second-zoo", slug="second-zoo", name="Second", website_url="https://second.example/")
    first_source = Source(
        id="first-source", zoo_id="first-zoo", kind="rss", url="https://first.example/feed",
        config={"official_host": "first.example", "allow_regex": r"/news/"},
    )
    second_source = Source(
        id="second-source", zoo_id="second-zoo", kind="rss", url="https://second.example/feed",
        config={"official_host": "second.example", "allow_regex": r"/news/"},
    )
    class TwoZooFetcher:
        supports_request_policy = True

        def __init__(self):
            self.calls = []

        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            self.calls.append(url)
            if url == first_source.url:
                return FetchResponse(url, 200, b"<rss/>")
            if url == second_source.url:
                return FetchResponse(
                    url,
                    200,
                    b"<rss><channel><item><link>https://second.example/news/ok</link></item></channel></rss>",
                )
            if url == "https://second.example/news/ok":
                return FetchResponse(
                    url,
                    200,
                    b"<html><head><title>OK</title></head><article>OK</article></html>",
                )
            raise AssertionError(f"unexpected URL: {url}")

    fetcher = TwoZooFetcher()
    config = CrawlerConfig(
        zoos=[first_zoo, second_zoo],
        sources=[first_source, second_source],
        request_delay=0,
    )
    return config, fetcher


def _assert_two_zoo_isolation(result, storage):
    assert result.status == "completed_with_errors"
    assert len(result.zoo_results) == 2
    by_zoo = {item.zoo_id: item for item in result.zoo_results}
    assert by_zoo["first-zoo"].status == "failed"
    assert by_zoo["first-zoo"].error_category == "configuration_error"
    assert by_zoo["second-zoo"].status == "success"
    assert by_zoo["second-zoo"].inserted == 1
    assert result.run is not None
    durable = storage.get_zoo_run_results(result.run.id)
    assert {item.zoo_id for item in durable} == {"first-zoo", "second-zoo"}
    assert len(durable) == 2


def test_zoo_sources_for_zoo_exception_isolated_and_durable(monkeypatch):
    config, fetcher = _two_zoo_setup()
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(config, storage=storage, fetcher=fetcher)
    original = crawler.registry.sources_for_zoo

    def failing_sources(zoo, **kwargs):
        if zoo.id == "first-zoo":
            raise ValueError("configuration source selection failed")
        return original(zoo, **kwargs)

    monkeypatch.setattr(crawler.registry, "sources_for_zoo", failing_sources)

    result = crawler.crawl("all")

    _assert_two_zoo_isolation(result, storage)
    assert "second.example/news/ok" in fetcher.calls[-1]


def test_zoo_source_setup_exception_isolated_and_durable(monkeypatch):
    config, fetcher = _two_zoo_setup()
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(config, storage=storage, fetcher=fetcher)
    original = crawler._source_id

    def failing_source_id(source):
        if source.id == "first-source":
            raise ValueError("configuration source setup failed")
        return original(source)

    monkeypatch.setattr(crawler, "_source_id", failing_source_id)

    result = crawler.crawl("all")

    _assert_two_zoo_isolation(result, storage)
    assert "second.example/news/ok" in fetcher.calls[-1]


def test_source_failure_does_not_abort_other_sources_and_is_logged_in_stats():
    zoo = Zoo(id="z1", slug="test-zoo", name="Test", website_url="https://official.example/")
    config = CrawlerConfig(
        zoos=[zoo],
        sources=[
            Source(id="bad", zoo_id="z1", kind="rss", url="https://official.example/bad", config={"official_host": "official.example", "allow_regex": r"/news/"}),
            Source(id="good", zoo_id="z1", kind="rss", url="https://official.example/good", config={"official_host": "official.example", "allow_regex": r"/news/"}),
        ],
        request_delay=0,
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(config, storage=storage, fetcher=FailingOneFetcher()).crawl("all")
    assert result.status == "completed_with_errors"
    assert any(stat.source_id == "bad" and stat.status == "error" for stat in result.stats)
    assert any(stat.source_id == "good" and stat.stored_count == 1 for stat in result.stats)
    assert storage.get_source("bad").last_error == "boom"


def test_next_batch_article_failure_counts_in_batch_and_continues():
    zoo = Zoo(
        id="batch-zoo",
        slug="batch-zoo",
        name="Batch Zoo",
        website_url="https://batch.example/",
    )
    source = Source(
        id="batch-source",
        zoo_id=zoo.id,
        kind="rss",
        url="https://batch.example/feed",
        config={
            "official_host": "batch.example",
            "allow_regex": r"/news/",
        },
    )

    class BatchFailureFetcher:
        supports_request_policy = True

        def __init__(self):
            self.calls = []

        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            self.calls.append(url)
            if url == source.url:
                return FetchResponse(
                    url,
                    200,
                    b"<rss><channel>"
                    b"<item><link>https://batch.example/news/bad</link></item>"
                    b"<item><link>https://batch.example/news/good</link></item>"
                    b"</channel></rss>",
                )
            if url.endswith("/bad"):
                raise RuntimeError("one article failed")
            return FetchResponse(
                url,
                200,
                b"<html><head><title>Good</title></head><article>Good</article></html>",
            )

    fetcher = BatchFailureFetcher()
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    )

    result = crawler.next_batch(source.id, limit=2)

    assert result.status == "completed_with_errors"
    assert result.metadata["candidates_processed"] == 2
    assert result.stats[0].error_count == 1
    assert result.stats[0].stored_count == 1
    assert storage.list_articles()[0].url == "https://batch.example/news/good"


def test_next_batch_stat_persistence_failure_keeps_error_metadata_in_sync():
    zoo = Zoo(
        id="stat-zoo",
        slug="stat-zoo",
        name="Stat Zoo",
        website_url="https://stat.example/",
    )
    source = Source(
        id="stat-source",
        zoo_id=zoo.id,
        kind="rss",
        url="https://stat.example/feed",
        config={
            "official_host": "stat.example",
            "allow_regex": r"/news/",
        },
    )

    class StatFailureStorage(SQLiteStorage):
        def record_run_stat(self, stat):
            raise RuntimeError("run stat write failed")

    class StatFetcher:
        supports_request_policy = True

        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            if url == source.url:
                return FetchResponse(
                    url,
                    200,
                    b"<rss><channel><item><link>"
                    b"https://stat.example/news/one</link></item></channel></rss>",
                )
            return FetchResponse(
                url,
                200,
                b"<html><head><title>One</title></head><article>One</article></html>",
            )

    storage = StatFailureStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=StatFetcher(),
    ).next_batch(source.id)

    stat = result.stats[0]
    zoo_result = result.zoo_results[0]
    assert stat.metadata["errors"] == stat.error_count == 1
    assert zoo_result.metadata["errors"] == 1
    assert result.metadata["errors"] == result.error_count == 1


def test_source_http_failure_retains_status_and_other_source_duration():
    class HTTPFailureFetcher(FailingOneFetcher):
        def fetch(self, url, *, request_policy):
            if url.endswith("/bad"):
                return FetchResponse(url, 503, b"unavailable")
            return super().fetch(url, request_policy=request_policy)

    zoo = Zoo(id="z1", slug="test-zoo", name="Test", website_url="https://official.example/")
    config = CrawlerConfig(
        zoos=[zoo],
        sources=[
            Source(id="bad", zoo_id="z1", kind="rss", url="https://official.example/bad", config={"official_host": "official.example", "allow_regex": r"/news/"}),
            Source(id="good", zoo_id="z1", kind="rss", url="https://official.example/good", config={"official_host": "official.example", "allow_regex": r"/news/"}),
        ],
    )
    storage = SQLiteStorage(":memory:")

    result = Crawler(config, storage=storage, fetcher=HTTPFailureFetcher()).crawl()

    assert result.status == "completed_with_errors"
    assert storage.get_source("bad").last_http_status == 503
    assert storage.get_source("good").last_http_status == 200
    assert all(stat.duration_ms is not None for stat in result.stats)


def test_request_boundary_failure_is_recorded_while_another_source_succeeds():
    calls = []

    class TraceFetcher:
        supports_request_policy = True

        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            calls.append(url)
            if url.endswith("/bad"):
                return FetchResponse(
                    url, 200,
                    b"<rss><channel><item><link>https://evil.example/news/escape</link></item></channel></rss>",
                )
            if url.endswith("/good"):
                return FetchResponse(
                    url, 200,
                    b"<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>",
                )
            return FetchResponse(url, 200, b"<html><article>One</article></html>")

    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    config = CrawlerConfig(
        zoos=[zoo],
        sources=[
            Source(
                id="bad", zoo_id="z1", kind="sitemap", url="https://official.example/bad",
                config={"official_host": "official.example", "allow_regex": r"/news/"},
            ),
            Source(
                id="good", zoo_id="z1", kind="rss", url="https://official.example/good",
                config={"official_host": "official.example", "allow_regex": r"/news/"},
            ),
        ],
    )
    # The bad source is a sitemap index whose child must never reach transport.
    class SitemapTraceFetcher(TraceFetcher):
        def fetch(self, url, *, request_policy):
            if url.endswith("/bad"):
                calls.append(url)
                return FetchResponse(
                    url, 200,
                    b"<sitemapindex><sitemap><loc>https://evil.example/private.xml?token=secret</loc></sitemap></sitemapindex>",
                )
            return super().fetch(url, request_policy=request_policy)

    storage = SQLiteStorage(":memory:")
    result = Crawler(config, storage=storage, fetcher=SitemapTraceFetcher()).crawl()

    assert result.status == "completed_with_errors"
    assert any(stat.source_id == "bad" and stat.status == "error" for stat in result.stats)
    assert any(stat.source_id == "good" and stat.stored_count == 1 for stat in result.stats)
    assert not any("evil.example" in url for url in calls)
    error = storage.get_source("bad").last_error
    assert "source bad" in error and "https://evil.example/private.xml" in error
    assert "secret" not in error


def test_source_boundary_error_and_logs_remove_query_secrets_and_userinfo(caplog):
    calls = []

    class PolicyFetcher:
        supports_request_policy = True

        def fetch(self, url, *, request_policy):
            calls.append(url)
            return FetchResponse(url, 200, b"<rss/>")

    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="secret-source", zoo_id="z1", kind="rss",
        url="https://alice:password@official.example/feed?token=topsecret",
        config={"official_host": "official.example"},
    )
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage,
        fetcher=PolicyFetcher(),
    ).crawl()

    assert result.status == "completed_with_errors"
    assert calls == []
    error = storage.get_source("secret-source").last_error
    combined = error + "\n" + caplog.text
    assert "source secret-source" in combined
    assert "https://official.example/feed" in combined
    assert "alice" not in combined
    assert "password" not in combined
    assert "token" not in combined
    assert "topsecret" not in combined


@pytest.mark.parametrize(
    "source_status",
    [
        "robots_disallowed",
        "javascript_required",
        "blocked",
        "unsupported",
        "partial",
        "source_not_found",
        "success_no_items",
    ],
)
def test_authoritative_no_source_zoo_does_not_block_following_zoo_or_duplicate_results(
    source_status,
):
    gap = Zoo(
        id="mini-zoo-venray",
        slug="mini-zoo-venray",
        name="Mini ZOO Venray",
        website_url=None,
        language=None,
        source_status=source_status,
        groups=("regional_lab", "dutch_border"),
        metadata={
            "no_official_website_verified": True,
            "no_executable_source": True,
            "import_gate": {"blockers": ["missing_official_website_url", "missing_language"]},
        },
        list_provenance=[
            {
                "source": "data/zoofan_phase0_zoo_list_v0_1.md",
                "version": "Phase0 v0.1",
                "sha256": "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd",
                "authority": "user_designated_authoritative_membership_list",
                "expanded_count": 73,
                "source_line": 82,
            },
        ],
    )
    following = Zoo(
        id="following-zoo",
        slug="following-zoo",
        name="Following Zoo",
        website_url="https://following.example/",
    )
    source = Source(
        id="following-source",
        zoo_id="following-zoo",
        kind="rss",
        url="https://following.example/feed",
        config={"official_host": "following.example", "allow_regex": r"/news/"},
    )
    gap_source = Source(
        id=f"mini-zoo-venray-{source_status}-evidence",
        zoo_id=gap.id,
        kind="unsupported",
        url=f"https://phase0.example/{source_status}",
        enabled=False,
        source_status=source_status,
    )
    article_url = "https://following.example/news/one"

    class Fetcher:
        supports_request_policy = True

        def __init__(self):
            self.calls = []

        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            self.calls.append(url)
            if url == source.url:
                return FetchResponse(
                    url,
                    200,
                    f"<rss><channel><item><link>{article_url}</link></item></channel></rss>".encode(),
                )
            if url == article_url:
                return FetchResponse(url, 200, b"<html><head><title>One</title></head><article>Body</article></html>")
            raise AssertionError(url)

    fetcher = Fetcher()
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[gap, following], sources=[gap_source, source], request_delay=0),
        storage=storage,
        fetcher=fetcher,
    ).crawl()

    assert result.status == "completed"
    assert fetcher.calls == [source.url, article_url]
    assert [item.zoo_id for item in result.zoo_results] == ["mini-zoo-venray", "following-zoo"]
    assert {item.zoo_id for item in storage.get_zoo_run_results(result.run.id)} == {
        "mini-zoo-venray",
        "following-zoo",
    }
    assert len(storage.get_zoo_run_results(result.run.id)) == 2
    assert {item.id for item in storage.list_sources()} == {
        gap_source.id,
        "following-source",
    }
    assert storage.get_source(gap_source.id).enabled is False
    assert result.zoo_results[0].status == "success_no_items"
    assert result.zoo_results[0].source_status == source_status
    assert result.zoo_results[1].status == "success"
    assert result.zoo_results[1].inserted == 1
