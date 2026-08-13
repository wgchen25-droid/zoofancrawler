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
