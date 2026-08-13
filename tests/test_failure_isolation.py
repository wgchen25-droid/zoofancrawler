from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.storage import SQLiteStorage


class FailingOneFetcher:
    def fetch(self, url):
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
