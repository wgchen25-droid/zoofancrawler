from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.discovery import DiscoveryEngine, SourceRegistry, URLPolicy
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.normalization import normalize_url
from zoofan.storage import SQLiteStorage


class FakeFetcher:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        return FetchResponse(url, 200, value.encode("utf-8"))


def _config():
    zoo = Zoo(id="z1", slug="test-zoo", name="Test Zoo", website_url="https://official.example/")
    rss = Source(id="rss", zoo_id="z1", kind="rss", url="https://official.example/feed", config={"official_host": "official.example", "allow_regex": r"/news/"})
    sitemap = Source(id="sitemap", zoo_id="z1", kind="sitemap", url="https://official.example/news.xml", config={"official_host": "official.example", "allow_regex": r"/news/"})
    return CrawlerConfig(zoos=[zoo], sources=[rss, sitemap], request_delay=0)


def test_rss_sitemap_dedup_and_second_run_skips_old_article():
    feed = "<rss><channel><item><title>One</title><link>https://official.example/news/one</link></item></channel></rss>"
    sitemap = "<urlset><url><loc>https://official.example/news/one</loc></url></urlset>"
    article = "<html><head><link rel='canonical' href='/news/one'/><meta property='og:title' content='One'/></head><article>Body</article></html>"
    fetcher = FakeFetcher({"https://official.example/feed": feed, "https://official.example/news.xml": sitemap, "https://official.example/news/one": article})
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(_config(), storage=storage, fetcher=fetcher)
    first = crawler.crawl("all")
    second = crawler.crawl("all")
    assert first.stored_count == 1
    assert second.fetched_count == 0
    assert len(storage.list_articles()) == 1
    assert len(storage.list_discoveries()) == 2
    assert fetcher.calls.count("https://official.example/news/one") == 1


def test_change_detection_rechecks_when_configured():
    config = _config()
    config.sources[0].config["recent_recheck"] = True
    feed = "<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>"
    first_article = "<html><head><meta property='og:title' content='Old'/></head><article>Old body</article></html>"
    new_article = "<html><head><meta property='og:title' content='New'/></head><article>New body</article></html>"
    class Changing(FakeFetcher):
        count = 0
        def fetch(self, url):
            if url.endswith("/news/one"):
                value = first_article if self.count == 0 else new_article
                self.count += 1
                self.calls.append(url)
                return FetchResponse(url, 200, value.encode())
            return super().fetch(url)
    fetcher = Changing({"https://official.example/feed": feed, "https://official.example/news.xml": "<urlset/>"})
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(config, storage=storage, fetcher=fetcher)
    crawler.crawl("all")
    crawler.crawl("all")
    assert storage.list_articles()[0].title == "New"
    assert len(storage.list_articles()) == 1


def test_source_registry_normalizes_tracking_and_trailing_variants_before_storage():
    zoo = Zoo(id="z1", slug="test-zoo", name="Test Zoo", website_url="https://official.example/")
    first = Source(id="first", zoo_id="z1", kind="rss", url="https://official.example/feed/?utm_source=registry")
    second = Source(id="second", zoo_id="z1", kind="rss", url="https://official.example/feed")
    registry = SourceRegistry([zoo], [first, second])

    assert len(registry.sources) == 1
    source = next(iter(registry.sources.values()))
    assert source is first
    assert source.url == normalize_url("https://official.example/feed")

    storage = SQLiteStorage(":memory:")
    registry.register_storage(storage)
    assert len(storage.list_sources(zoo_id="z1")) == 1


def test_url_policy_allow_overrides_default_navigation_but_not_exclude_domain_or_scheme():
    policy = URLPolicy(
        official_hosts=("official.example",),
        allow_patterns=(r"/events/",),
        exclude_patterns=(r"/events/private",),
    )
    assert policy.accepts("https://official.example/events/public")
    assert not policy.accepts("https://official.example/events/private")
    assert not policy.accepts("https://evil.example/events/public")
    assert not policy.accepts("ftp://official.example/events/public")


def test_sitemap_max_sitemaps_counts_root_document():
    zoo = Zoo(id="z1", slug="test-zoo", name="Test Zoo", website_url="https://official.example/")
    source = Source(
        id="sitemap", zoo_id="z1", kind="sitemap", url="https://official.example/sitemap.xml",
        config={"official_host": "official.example", "allow_regex": r"/news/", "max_sitemaps": 1},
    )
    payloads = {
        source.url: "<sitemapindex><sitemap><loc>/child.xml</loc></sitemap></sitemapindex>",
        "https://official.example/child.xml": "<urlset><url><loc>/news/one</loc></url></urlset>",
    }
    fetcher = FakeFetcher(payloads)
    assert DiscoveryEngine(fetcher).discover(source, zoo=zoo) == []
    assert fetcher.calls == [source.url]
