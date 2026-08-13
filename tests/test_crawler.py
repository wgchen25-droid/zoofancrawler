import hashlib

import pytest

from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.discovery import DiscoveryEngine, DiscoveryError, DiscoveryResult, SourceRegistry, URLPolicy
from zoofan.fetcher import Fetcher, RequestBoundaryError
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.normalization import normalize_url
from zoofan.storage import SQLiteStorage


class FakeFetcher:
    supports_request_policy = True

    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch(self, url, *, request_policy):
        assert request_policy.validate(url) == url
        self.calls.append(url)
        value = self.payloads[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, FetchResponse):
            return value
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
        def fetch(self, url, *, request_policy):
            assert request_policy.validate(url) == url
            if url.endswith("/news/one"):
                value = first_article if self.count == 0 else new_article
                self.count += 1
                self.calls.append(url)
                return FetchResponse(url, 200, value.encode())
            return super().fetch(url, request_policy=request_policy)
    fetcher = Changing({"https://official.example/feed": feed, "https://official.example/news.xml": "<urlset/>"})
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(config, storage=storage, fetcher=fetcher)
    crawler.crawl("all")
    crawler.crawl("all")
    assert storage.list_articles()[0].title == "New"
    assert len(storage.list_articles()) == 1


def test_crawler_persists_configured_article_dateline_and_keeps_unknown_null():
    zoo = Zoo(id="koeln-zoo", website_url="https://koelnerzoo.de/")
    source = Source(
        id="koeln-zoo-news-sitemap",
        zoo_id=zoo.id,
        kind="sitemap",
        url="https://koelnerzoo.de/news.xml",
        config={
            "official_host": "koelnerzoo.de",
            "allow_regex": r"/aktuellartical/",
            "article_date_selector": "main#brx-content p",
            "article_date_pattern": r"^(?:Köln|Koeln|Cologne),?\s+(?P<day>\d{1,2})\.?\s+(?P<month>[A-Za-zÄÖÜäöüß]+)\s+(?P<year>\d{4})(?:\b|$)",
        },
    )
    dated_url = "https://koelnerzoo.de/aktuellartical/dated"
    unknown_url = "https://koelnerzoo.de/aktuellartical/unknown"
    sitemap = f"""<urlset><url><loc>{dated_url}</loc></url>
    <url><loc>{unknown_url}</loc></url></urlset>"""
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=FakeFetcher({
            source.url: sitemap,
            dated_url: "<main id='brx-content'><p><strong>Köln, 7. März 2025</strong></p><p>Dated</p></main>",
            unknown_url: "<main id='brx-content'><p>Unanchored 8. April 2025</p><p>Unknown</p></main>",
        }),
    ).crawl()

    articles = {article.url: article for article in storage.list_articles()}
    assert result.stored_count == 2
    persisted_dates = dict(
        storage._connection.execute("SELECT source_url, published_at FROM articles").fetchall()
    )
    assert persisted_dates[dated_url] == "2025-03-07"
    assert persisted_dates[unknown_url] is None
    assert articles[dated_url].published_at.date().isoformat() == "2025-03-07"
    assert articles[unknown_url].published_at is None


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


def test_discovery_reports_root_status_and_suppressed_within_source_duplicates():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss", zoo_id="z1", kind="rss", url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    feed = """<rss><channel>
      <item><link>https://official.example/news/one</link></item>
      <item><link>https://official.example/news/one/</link></item>
    </channel></rss>"""
    fetcher = FakeFetcher({source.url: FetchResponse(source.url, 206, feed.encode())})

    result = DiscoveryEngine(fetcher).discover(source, zoo=zoo)

    assert isinstance(result, DiscoveryResult)
    assert len(result) == 1
    assert result.root_http_status == 206
    assert result.duplicate_candidate_count == 1

    storage = SQLiteStorage(":memory:")
    crawler_result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage,
        fetcher=FakeFetcher({
            source.url: FetchResponse(source.url, 206, feed.encode()),
            "https://official.example/news/one": "<html><article>One</article></html>",
        }),
    ).crawl()
    assert crawler_result.stats[0].duplicate_candidate_count == 1


def test_metric_classification_and_second_run_preserves_article_evidence():
    config = _config()
    config.sources = config.sources[:1]
    config.sources[0].language = "de"
    feed = "<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>"
    body = b"<html lang='de'><head><title>One</title></head><article>Body \xff</article></html>"
    fetcher = FakeFetcher({
        config.sources[0].url: FetchResponse(config.sources[0].url, 202, feed.encode()),
        "https://official.example/news/one": FetchResponse("https://official.example/news/one", 201, body),
    })
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(config, storage=storage, fetcher=fetcher)

    first = crawler.crawl()
    first_article = storage.list_articles()[0]
    second = crawler.crawl()
    article = storage.list_articles()[0]

    assert (first.stored_count, first.already_known_count, first.duplicate_candidate_count) == (1, 0, 0)
    assert (second.stored_count, second.already_known_count, second.duplicate_candidate_count) == (0, 1, 0)
    assert storage.get_source("rss").last_http_status == 202
    assert article.http_status == first_article.http_status == 201
    assert article.html_hash == first_article.html_hash == hashlib.sha256(body).hexdigest()
    assert article.content_hash == first_article.content_hash
    assert article.crawl_status == "success"
    assert article.last_fetched_at == first_article.last_fetched_at
    assert article.language == "de"
    assert first.run.duration_ms is not None and second.run.duration_ms is not None
    assert all(stat.duration_ms is not None for stat in first.stats + second.stats)
    discovery = storage.list_discoveries()[0]
    assert discovery.last_discovered_at >= discovery.discovered_at


def test_cross_source_duplicate_is_run_duplicate_and_records_both_relations():
    feed = "<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>"
    article = "<html><head><title>One</title></head><article>Body</article></html>"
    config = _config()
    config.sources[1].kind = "rss"
    fetcher = FakeFetcher({
        config.sources[0].url: feed,
        config.sources[1].url: feed,
        "https://official.example/news/one": article,
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(config, storage=storage, fetcher=fetcher).crawl()

    assert (result.stored_count, result.already_known_count, result.duplicate_candidate_count) == (1, 0, 1)
    assert len(storage.list_articles()) == 1
    assert {item.source_id for item in storage.list_discoveries()} == {"rss", "sitemap"}

    second = Crawler(config, storage=storage, fetcher=fetcher).crawl()
    assert (second.stored_count, second.already_known_count, second.duplicate_candidate_count) == (0, 1, 1)


@pytest.mark.parametrize("merge_by", ["canonical", "content"])
def test_post_fetch_identity_merge_counts_duplicate_not_stored(merge_by):
    zoo = Zoo(id="z1", website_url="https://official.example/")
    sources = [
        Source(id="one", zoo_id="z1", kind="rss", url="https://official.example/feed-one", config={"official_host": "official.example", "allow_regex": r"/news/"}),
        Source(id="two", zoo_id="z1", kind="rss", url="https://official.example/feed-two", config={"official_host": "official.example", "allow_regex": r"/news/"}),
    ]
    feeds = [
        "<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>",
        "<rss><channel><item><link>https://official.example/news/two</link></item></channel></rss>",
    ]
    if merge_by == "canonical":
        bodies = [
            "<html><link rel='canonical' href='/news/shared'><article>First</article></html>",
            "<html><link rel='canonical' href='/news/shared'><article>Second</article></html>",
        ]
    else:
        bodies = ["<html><article>Same</article></html>"] * 2
    fetcher = FakeFetcher({
        sources[0].url: feeds[0], sources[1].url: feeds[1],
        "https://official.example/news/one": bodies[0],
        "https://official.example/news/two": bodies[1],
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(CrawlerConfig(zoos=[zoo], sources=sources), storage=storage, fetcher=fetcher).crawl()

    assert (result.stored_count, result.already_known_count, result.duplicate_candidate_count) == (1, 0, 1)
    assert len(storage.list_articles()) == 1
    assert len(storage.list_discoveries()) == 2


@pytest.mark.parametrize(
    ("html", "source_language", "zoo_language", "expected"),
    [
        ("<html lang='de'><meta name='language' content='nl'><article>x</article></html>", "fr", "en", "de"),
        ("<html><script type='application/ld+json'>{\"@type\":\"NewsArticle\",\"inLanguage\":\"nl\"}</script><article>x</article></html>", "fr", "en", "nl"),
        ("<html><article>x</article></html>", "fr", "en", "fr"),
        ("<html><article>x</article></html>", None, "en", "en"),
        ("<html><article>x</article></html>", None, None, None),
    ],
)
def test_article_language_priority(html, source_language, zoo_language, expected):
    zoo = Zoo(id="z1", website_url="https://official.example/", language=zoo_language)
    source = Source(
        id="rss", zoo_id="z1", url="https://official.example/feed", language=source_language,
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    candidate_url = "https://official.example/news/one"
    crawler = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        fetcher=FakeFetcher({candidate_url: html}),
    )

    from zoofan.discovery import DiscoveryCandidate
    article = crawler._fetch_article(DiscoveryCandidate(candidate_url), source, zoo)
    assert article.language == expected


def test_registry_only_records_real_health_status_without_articles():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(id="health", zoo_id="z1", kind="registry_only", url="https://official.example/health")
    config = CrawlerConfig(zoos=[zoo], sources=[source])
    storage = SQLiteStorage(":memory:")
    fetcher = FakeFetcher({source.url: FetchResponse(source.url, 204, b"")})

    result = Crawler(config, storage=storage, fetcher=fetcher).crawl()

    assert result.discovered_count == 0
    assert result.stats[0].status == "completed"
    assert storage.get_source("health").last_http_status == 204


@pytest.mark.parametrize(
    ("kind", "payload", "config"),
    [
        ("atom", "<feed xmlns='http://www.w3.org/2005/Atom'/>", {}),
        ("sitemap", "<urlset/>", {}),
        ("archive", "<html/>", {"article_selector": ".card", "link_selector": "a"}),
    ],
)
def test_each_discovery_kind_persists_its_real_root_status(kind, payload, config):
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id=kind, zoo_id="z1", kind=kind, url=f"https://official.example/{kind}",
        config={"official_host": "official.example", **config},
    )
    storage = SQLiteStorage(":memory:")
    fetcher = FakeFetcher({source.url: FetchResponse(source.url, 203, payload.encode())})

    Crawler(CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage, fetcher=fetcher).crawl()

    assert storage.get_source(kind).last_http_status == 203


def test_source_parse_failure_retains_successful_root_http_status():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(id="rss", zoo_id="z1", kind="rss", url="https://official.example/feed")
    storage = SQLiteStorage(":memory:")
    fetcher = FakeFetcher({source.url: FetchResponse(source.url, 200, b"not xml")})

    result = Crawler(CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage, fetcher=fetcher).crawl()

    assert result.status == "completed_with_errors"
    assert storage.get_source("rss").last_http_status == 200


def test_downstream_sitemap_failure_keeps_root_status_and_child_error_detail():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(id="map", zoo_id="z1", kind="sitemap", url="https://official.example/root.xml")
    child = "https://official.example/child.xml"
    fetcher = FakeFetcher({
        source.url: FetchResponse(
            source.url, 200,
            f"<sitemapindex><sitemap><loc>{child}</loc></sitemap></sitemapindex>".encode(),
        ),
        child: FetchResponse(child, 503, b"unavailable"),
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage, fetcher=fetcher).crawl()

    persisted = storage.get_source("map")
    assert result.status == "completed_with_errors"
    assert persisted.last_http_status == 200
    assert child in persisted.last_error
    assert "503" in persisted.last_error


def test_archive_duplicate_cards_are_visible_in_discovery_metrics():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="archive", zoo_id="z1", kind="archive", url="https://official.example/archive",
        config={
            "official_host": "official.example", "allow_regex": r"/news/",
            "article_selector": ".card", "link_selector": "a",
        },
    )
    payload = """<div class='card'><a href='/news/one'>One</a></div>
    <div class='card'><a href='/news/one/'>Again</a></div>"""

    result = DiscoveryEngine(FakeFetcher({source.url: payload})).discover(source, zoo=zoo)

    assert len(result) == 1
    assert result.duplicate_candidate_count == 1


@pytest.mark.parametrize("body_factory", [bytearray, memoryview])
def test_html_hash_preserves_bytearray_and_memoryview_exactly(body_factory):
    config = _config()
    config.sources = config.sources[:1]
    feed = "<rss><channel><item><link>https://official.example/news/one</link></item></channel></rss>"
    raw = b"<html><article>Exact \xff bytes</article></html>"
    fetcher = FakeFetcher({
        config.sources[0].url: feed,
        "https://official.example/news/one": FetchResponse(
            "https://official.example/news/one", 200, body_factory(raw),
        ),
    })
    storage = SQLiteStorage(":memory:")

    Crawler(config, storage=storage, fetcher=fetcher).crawl()

    assert storage.list_articles()[0].html_hash == hashlib.sha256(raw).hexdigest()


def test_external_sitemap_child_is_rejected_before_transport_and_official_child_works():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="map", zoo_id="z1", kind="sitemap", url="https://official.example/root.xml",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    external = "https://evil.example/child.xml?token=secret"
    fetcher = FakeFetcher({
        source.url: f"<sitemapindex><sitemap><loc>{external}</loc></sitemap></sitemapindex>",
    })
    with pytest.raises(DiscoveryError, match="source map rejected request target"):
        DiscoveryEngine(fetcher).discover(source, zoo=zoo)
    assert fetcher.calls == [source.url]

    official = "https://official.example/child.xml"
    fetcher = FakeFetcher({
        source.url: f"<sitemapindex><sitemap><loc>{official}</loc></sitemap></sitemapindex>",
        official: "<urlset><url><loc>/news/one</loc></url></urlset>",
    })
    result = DiscoveryEngine(fetcher).discover(source, zoo=zoo)
    assert [item.url for item in result] == ["https://official.example/news/one"]
    assert fetcher.calls == [source.url, official]


def test_external_archive_pagination_is_rejected_before_transport_and_official_page_works():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="archive", zoo_id="z1", kind="archive", url="https://official.example/archive",
        config={
            "official_host": "official.example", "allow_regex": r"/news/",
            "article_selector": ".card", "link_selector": "a.article",
            "pagination_selector": "a.next", "max_pages": 2,
        },
    )
    external = "https://evil.example/page/2"
    fetcher = FakeFetcher({source.url: f"<a class='next' href='{external}'>Next</a>"})
    with pytest.raises(DiscoveryError, match="source archive rejected request target"):
        DiscoveryEngine(fetcher).discover(source, zoo=zoo)
    assert fetcher.calls == [source.url]

    next_page = "https://official.example/archive?page=2"
    fetcher = FakeFetcher({
        source.url: "<a class='next' href='/archive?page=2'>Next</a>",
        next_page: "<div class='card'><a class='article' href='/news/one'>One</a></div>",
    })
    result = DiscoveryEngine(fetcher).discover(source, zoo=zoo)
    assert [item.url for item in result] == ["https://official.example/news/one"]
    assert fetcher.calls == [source.url, next_page]


def test_initial_external_source_and_external_feed_article_never_reach_transport():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    external_source = Source(
        id="bad-root", zoo_id="z1", kind="rss", url="https://evil.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    fetcher = FakeFetcher({})
    with pytest.raises(RequestBoundaryError):
        DiscoveryEngine(fetcher).discover(external_source, zoo=zoo)
    assert fetcher.calls == []

    source = Source(
        id="feed", zoo_id="z1", kind="rss", url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    feed = "<rss><channel><item><link>https://evil.example/news/one</link></item></channel></rss>"
    fetcher = FakeFetcher({source.url: feed})
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=SQLiteStorage(":memory:"), fetcher=fetcher,
    ).crawl()
    assert result.status == "completed"
    assert result.discovered_count == 0
    assert fetcher.calls == [source.url]


def test_unmarked_injected_fetcher_is_rejected_without_any_call():
    calls = []

    class OpaqueFetcher:
        def fetch(self, url):
            calls.append(url)
            return FetchResponse(url, 200, b"<rss/>")

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="opaque", zoo_id="z1", kind="rss", url="https://official.example/feed",
        config={"official_host": "official.example"},
    )
    with pytest.raises(RequestBoundaryError, match="untrusted injected fetcher"):
        DiscoveryEngine(OpaqueFetcher()).discover(source, zoo=zoo)
    assert calls == []


def test_marked_injected_fetcher_must_return_verifiable_one_hop_url():
    calls = []

    class IncompletePolicyFetcher:
        supports_request_policy = True

        def fetch(self, url, *, request_policy):
            calls.append((url, request_policy.source_id))
            return "<rss/>"

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="incomplete", zoo_id="z1", kind="rss", url="https://official.example/feed",
        config={"official_host": "official.example"},
    )
    with pytest.raises(RequestBoundaryError, match="no one-hop response URL"):
        DiscoveryEngine(IncompletePolicyFetcher()).discover(source, zoo=zoo)
    assert calls == [(source.url, "incomplete")]


@pytest.mark.parametrize("redirect_host", ["official.example", "cdn.official.example"])
def test_discovery_fetcher_integration_accepts_validated_same_host_and_alias_redirect(redirect_host):
    calls = []
    source_url = "https://official.example/feed-start"
    final_url = f"https://{redirect_host}/feed-final"
    article_url = f"https://{redirect_host}/news/one"

    def transport(url, headers, timeout):
        calls.append(url)
        if url == source_url:
            return FetchResponse(url, 302, b"", {"Location": final_url})
        if url == final_url:
            return FetchResponse(
                url, 200,
                f"<rss><channel><item><link>{article_url}</link></item></channel></rss>".encode(),
            )
        raise AssertionError(url)

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="redirect-feed", zoo_id="z1", kind="rss", url=source_url,
        config={
            "official_hosts": ["official.example", "cdn.official.example"],
            "allow_regex": r"/news/",
        },
    )
    result = DiscoveryEngine(
        Fetcher(transport=transport, respect_robots=False, delay=0)
    ).discover(source, zoo=zoo)
    assert [item.url for item in result] == [article_url]
    assert calls == [source_url, final_url]


@pytest.mark.parametrize("redirect_host", ["official.example", "cdn.official.example"])
def test_crawler_article_fetcher_integration_accepts_validated_same_host_and_alias_redirect(redirect_host):
    calls = []
    source_url = "https://official.example/feed"
    candidate_url = "https://official.example/news/one"
    final_url = f"https://{redirect_host}/news/final"
    feed = f"<rss><channel><item><link>{candidate_url}</link></item></channel></rss>"

    def transport(url, headers, timeout):
        calls.append(url)
        if url == source_url:
            return FetchResponse(url, 200, feed.encode())
        if url == candidate_url:
            return FetchResponse(url, 302, b"", {"Location": final_url})
        if url == final_url:
            return FetchResponse(url, 200, b"<html><article>Redirected</article></html>")
        raise AssertionError(url)

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="redirect-article", zoo_id="z1", kind="rss", url=source_url,
        config={
            "official_hosts": ["official.example", "cdn.official.example"],
            "allow_regex": r"/news/",
        },
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage,
        fetcher=Fetcher(transport=transport, respect_robots=False, delay=0),
    ).crawl()
    assert result.stored_count == 1
    assert calls == [source_url, candidate_url, final_url]


def test_discovery_trailing_slash_redirect_integration_has_one_request_per_hop():
    calls = []
    source_url = "https://official.example/feed"
    final_url = "https://official.example/feed/"

    def transport(url, headers, timeout):
        calls.append(url)
        if url == source_url:
            return FetchResponse(url, 301, b"", {"Location": "/feed/"})
        if url == final_url:
            return FetchResponse(url, 200, b"<rss/>")
        raise AssertionError(url)

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="slash-feed", zoo_id="z1", kind="rss", url=source_url,
        config={"official_host": "official.example"},
    )
    result = DiscoveryEngine(
        Fetcher(transport=transport, respect_robots=False, delay=0, max_redirects=2)
    ).discover(source, zoo=zoo)

    assert result == []
    assert calls == [source_url, final_url]


def test_crawler_article_trailing_slash_redirect_integration_has_one_request_per_hop():
    calls = []
    source_url = "https://official.example/feed"
    candidate_url = "https://official.example/news/one"
    final_url = "https://official.example/news/one/"
    feed = f"<rss><channel><item><link>{candidate_url}</link></item></channel></rss>"

    def transport(url, headers, timeout):
        calls.append(url)
        if url == source_url:
            return FetchResponse(url, 200, feed.encode())
        if url == candidate_url:
            return FetchResponse(url, 301, b"", {"Location": "/news/one/"})
        if url == final_url:
            return FetchResponse(url, 200, b"<html><article>Slash target</article></html>")
        raise AssertionError(url)

    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="slash-article", zoo_id="z1", kind="rss", url=source_url,
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage,
        fetcher=Fetcher(transport=transport, respect_robots=False, delay=0, max_redirects=2),
    ).crawl()

    assert result.stored_count == 1
    assert calls == [source_url, candidate_url, final_url]
