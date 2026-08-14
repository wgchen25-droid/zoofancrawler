import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.adapters import AdapterRegistry, UnsupportedAdapterError
from zoofan.discovery import DiscoveryCandidate, DiscoveryEngine, DiscoveryError, DiscoveryResult, SourceRegistry, URLPolicy, _as_candidate
from zoofan.fetcher import Fetcher, RequestBoundaryError, RobotsDisallowed
from zoofan.models import FetchResponse, ParsedFeedItem, Source, Zoo
from zoofan.normalization import normalize_url
from zoofan.parsers import parse_json_api
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


def _phase0_no_site_zoo(
    zoo_id="mini-zoo-venray",
    *,
    name="Mini ZOO Venray",
    source_status="not_found",
):
    return Zoo(
        id=zoo_id,
        slug=zoo_id,
        name=name,
        website_url=None,
        language=None,
        source_status=source_status,
        groups=("regional_lab", "dutch_border"),
        metadata={
            "no_official_website_verified": True,
            "no_executable_source": True,
            "import_gate": {
                "blockers": ["missing_official_website_url", "missing_language"],
            },
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


def test_authoritative_phase0_no_source_is_success_no_items_without_network_or_source_writes():
    zoo = _phase0_no_site_zoo()
    fetcher = FakeFetcher({})
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(CrawlerConfig(zoos=[zoo], sources=[]), storage=storage, fetcher=fetcher)
    discovery_calls = []
    original_discover = crawler.discovery.discover

    def unexpected_discovery(*args, **kwargs):
        discovery_calls.append((args, kwargs))
        return original_discover(*args, **kwargs)

    crawler.discovery.discover = unexpected_discovery
    result = crawler.crawl()

    assert fetcher.calls == []
    assert discovery_calls == []
    assert result.status == "completed"
    assert result.failed_count == 0
    assert len(result.zoo_results) == 1
    zoo_result = result.zoo_results[0]
    assert zoo_result.status == "success_no_items"
    assert zoo_result.source_status == "source_not_found"
    assert zoo_result.error_category is None
    assert zoo_result.failed == 0
    assert zoo_result.source_url is None
    assert zoo_result.error_summary == "source_not_found: no enabled sources configured"
    assert storage.list_articles() == []
    assert storage.list_sources() == []
    assert len(storage.get_zoo_run_results(result.run.id)) == 1
    assert storage.get_crawl_run(result.run.id).status == "completed"


def test_empty_source_configuration_without_phase0_evidence_remains_failed():
    zoo = Zoo(id="ordinary", slug="ordinary", name="Ordinary Zoo", website_url=None, source_status="not_found")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[]),
        storage=SQLiteStorage(":memory:"),
        fetcher=FakeFetcher({}),
    ).crawl()

    assert result.status == "completed_with_errors"
    assert result.zoo_results[0].status == "failed"
    assert result.zoo_results[0].source_status == "configuration_error"
    assert result.zoo_results[0].error_category == "configuration_error"
    assert result.zoo_results[0].failed == 1


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
def test_authoritative_phase0_disabled_evidence_status_is_nonblocking_without_fetching(
    source_status,
):
    zoo = _phase0_no_site_zoo(source_status=source_status)
    zoo.website_url = "https://phase0.example/"
    source = Source(
        id=f"{zoo.id}-evidence",
        zoo_id=zoo.id,
        kind="unsupported",
        url="https://phase0.example/evidence?token=secret",
        enabled=False,
        source_status=source_status,
        config={
            "official_host": "phase0.example",
            "source_status_reason": f"Configured evidence for {source_status}.",
            "evidence_urls": ["https://phase0.example/evidence?token=secret"],
        },
    )
    fetcher = FakeFetcher({})
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl()

    assert fetcher.calls == []
    assert result.status == "completed"
    assert result.failed_count == 0
    assert len(result.zoo_results) == 1
    zoo_result = result.zoo_results[0]
    assert zoo_result.status == "success_no_items"
    assert zoo_result.source_status == source_status
    assert zoo_result.failed == 0
    assert zoo_result.source_url == "https://phase0.example/evidence"
    assert zoo_result.metadata["configured_source_status"] == source_status
    assert zoo_result.metadata["error_categories"][0] == source_status
    assert zoo_result.metadata["evidence_sources"] == [
        {
            "id": source.id,
            "status": source_status,
            "enabled": False,
            "url": "https://phase0.example/evidence",
            "reason": f"Configured evidence for {source_status}.",
        }
    ]
    assert [item.id for item in storage.list_sources()] == [source.id]
    assert storage.list_sources()[0].enabled is False
    assert len(storage.get_zoo_run_results(result.run.id)) == 1


@pytest.mark.parametrize("source_status", ["configured", "success"])
def test_authoritative_phase0_empty_sources_configured_or_success_remains_blocking(
    source_status,
):
    zoo = _phase0_no_site_zoo(source_status=source_status)
    zoo.website_url = "https://phase0.example/"
    source = Source(
        id=f"{zoo.id}-evidence",
        zoo_id=zoo.id,
        kind="unsupported",
        url="https://phase0.example/evidence",
        enabled=False,
        source_status=source_status,
    )
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=SQLiteStorage(":memory:"),
        fetcher=FakeFetcher({}),
    ).crawl()

    assert result.status == "completed_with_errors"
    zoo_result = result.zoo_results[0]
    assert zoo_result.status == "failed"
    assert zoo_result.source_status == "configuration_error"
    assert zoo_result.error_category == "configuration_error"
    assert zoo_result.failed == 1


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


def test_json_api_discovery_uses_field_paths_and_official_host_policy():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="json",
        zoo_id="z1",
        kind="json_api",
        url="https://official.example/api/news",
        config={
            "official_host": "official.example",
            "allow_regex": r"/news/",
            "items_path": "data.items",
            "fields": {"url": "links.html", "title": "headline", "date": "publishedAt"},
        },
    )
    payload = (
        '{"data":{"items":['
        '{"headline":"One","links":{"html":"/news/one"},"publishedAt":"2025-04-03"},'
        '{"headline":"Duplicate","links":{"html":"https://official.example/news/one?utm_source=api"}},'
        '{"headline":"Outside","links":{"html":"https://evil.example/news/two"}}]}}'
    )
    result = DiscoveryEngine(FakeFetcher({source.url: payload})).discover(source, zoo=zoo)
    assert [candidate.url for candidate in result] == ["https://official.example/news/one"]
    assert result.duplicate_candidate_count == 0  # parser removes normalized duplicates before policy
    assert result.root_http_status == 200


def test_custom_adapter_registry_is_injected_and_unknown_names_are_unsupported():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )

    class FixtureAdapter:
        name = "fixture_adapter"

        def discover(self, zoo_config):
            assert zoo_config.id == "z1"
            return ["https://official.example/news/custom", "https://evil.example/news/nope"]

    result = DiscoveryEngine(
        FakeFetcher({}), adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()})
    ).discover(source, zoo=zoo)
    assert [candidate.url for candidate in result] == ["https://official.example/news/custom"]

    unknown = Source(
        id="unknown",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="not_registered",
        url="https://official.example/custom",
        config={"official_host": "official.example"},
    )
    with pytest.raises(UnsupportedAdapterError):
        DiscoveryEngine(FakeFetcher({}), adapter_registry=AdapterRegistry()).discover(unknown, zoo=zoo)


def test_registry_retains_multiple_explicit_unsupported_entries_without_urls():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    first = Source(id="unsupported-one", zoo_id="z1", kind="unsupported", url="")
    second = Source(id="unsupported-two", zoo_id="z1", kind="unsupported", url="")
    registry = SourceRegistry([zoo], [first, second])
    assert len(registry.sources_for_zoo(zoo)) == 2


def test_crawler_dispatches_custom_adapter_without_adapter_owned_storage_logic():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )

    class FixtureAdapter:
        def discover(self, source, *, zoo=None, fetcher=None, request_policy=None):
            assert source.id == "custom"
            return [DiscoveryCandidate("https://official.example/news/custom", source)]

    article = "<html><head><title>Custom article</title></head><article>Body</article></html>"
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source], adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()})),
        storage=storage,
        fetcher=FakeFetcher({"https://official.example/news/custom": article}),
    ).crawl()
    assert result.stored_count == 1
    assert storage.list_articles()[0].title == "Custom article"


def test_custom_adapter_fixture_returns_normalized_candidates_and_filters_duplicate_external():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )

    class FixtureAdapter:
        def discover(self, source, *, zoo=None, fetcher=None, request_policy=None):
            payload = json.loads(
                (Path(__file__).parent / "fixtures" / "custom_adapter_v1.json").read_text(
                    encoding="utf-8"
                )
            )
            return parse_json_api(payload, base_url=source.url)

    article_html = {
        "https://official.example/news/custom-one": "<html><head><title>One</title></head><article>One body</article></html>",
        "https://official.example/news/custom-two": "<html><head><title>Two</title></head><article>Two body</article></html>",
    }
    fetcher = FakeFetcher(article_html)
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(
            zoos=[zoo],
            sources=[source],
            adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()}),
        ),
        storage=storage,
        fetcher=fetcher,
    ).crawl()

    assert result.discovered_count == 2
    assert result.stored_count == 2
    assert fetcher.calls == [
        "https://official.example/news/custom-one",
        "https://official.example/news/custom-two",
    ]


@pytest.mark.parametrize(
    ("fixture", "expected_count"),
    [
        ("custom_adapter_normal_v1.json", 1),
        ("custom_adapter_empty_v1.json", 0),
        ("custom_adapter_missing_date_v1.json", 1),
        ("custom_adapter_duplicate_v1.json", 1),
        ("custom_adapter_drift_v1.json", 1),
    ],
)
def test_custom_adapter_saved_fixture_matrix_preserves_fields_and_runtime_shape(
    fixture, expected_count
):
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom-matrix",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom-matrix",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )

    class FixtureAdapter:
        def discover(self, source, *, zoo=None, fetcher=None, request_policy=None):
            payload = json.loads(
                (Path(__file__).parent / "fixtures" / fixture).read_text(encoding="utf-8")
            )
            # Keep the duplicate pair visible to the discovery layer so its
            # duplicate metric is exercised, while other adapters can use the
            # parser's normal identity de-duplication.
            return parse_json_api(
                payload,
                base_url=source.url,
                deduplicate=fixture != "custom_adapter_duplicate_v1.json",
            )

    result = DiscoveryEngine(
        FakeFetcher({}), adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()})
    ).discover(source, zoo=zoo)

    assert len(result) == expected_count
    if fixture == "custom_adapter_normal_v1.json":
        assert result[0].title == "Custom adapter normal"
        assert result[0].published_at.isoformat() == "2025-04-03T09:30:00+00:00"
        assert result[0].summary == "Normal custom adapter evidence."
    elif fixture == "custom_adapter_empty_v1.json":
        assert result == []
    elif fixture == "custom_adapter_missing_date_v1.json":
        assert result[0].title == "Custom adapter without a date"
        assert result[0].published_at is None
        assert "published_at_raw" not in result[0].metadata
    elif fixture == "custom_adapter_duplicate_v1.json":
        assert result[0].title == "Custom duplicate first"
        assert result.duplicate_candidate_count == 1
    else:
        # Localized month coverage belongs to archive/article fixtures; this
        # custom-adapter drift case checks only tolerant field-shape mapping.
        assert result[0].title == "Custom adapter benign drift"
        assert result[0].published_at is not None


def test_custom_adapter_receives_scoped_context_and_forces_robots_for_raw_fetcher():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    adapter_url = "https://official.example/adapter-data"
    adapter_final_url = "https://official.example/adapter-data-final"
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /\n")
        if url == adapter_url:
            return FetchResponse(url, 302, b"", {"Location": adapter_final_url})
        if url == adapter_final_url:
            return FetchResponse(url, 200, b"adapter payload")
        raise AssertionError(url)

    class FixtureAdapter:
        def discover(self, source, *, fetcher=None, request_policy=None):
            assert fetcher is not None
            assert not isinstance(fetcher, Fetcher)
            assert not hasattr(fetcher, "transport")
            assert not hasattr(fetcher, "respect_robots")
            assert request_policy is not None
            response = fetcher.get(adapter_url, request_policy=request_policy)
            assert response.url == adapter_final_url
            assert response.content == b"adapter payload"
            return ["https://official.example/news/from-adapter"]

    result = DiscoveryEngine(
        Fetcher(transport=transport, respect_robots=False, delay=0, retries=0),
        adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()}),
    ).discover(source, zoo=zoo)

    assert [candidate.url for candidate in result] == [
        "https://official.example/news/from-adapter"
    ]
    assert calls == [
        "https://official.example/robots.txt",
        adapter_url,
        adapter_final_url,
    ]


def test_custom_adapter_context_rejects_cross_domain_and_policy_bypass_attempts():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        raise AssertionError("the rejected adapter request reached transport")

    class FixtureAdapter:
        def __init__(self, attempt):
            self.attempt = attempt

        def discover(self, source, *, fetcher=None, request_policy=None):
            if self.attempt == "cross-domain":
                fetcher.fetch("https://evil.example/steal")
            elif self.attempt == "policy-override":
                fetcher.fetch(source.url, request_policy=None)
            else:
                fetcher.get(source.url, respect_robots=False)
            return []

    for attempt in ("cross-domain", "policy-override", "robots-bypass"):
        with pytest.raises(RequestBoundaryError):
            DiscoveryEngine(
                Fetcher(transport=transport, respect_robots=False, delay=0, retries=0),
                adapter_registry=AdapterRegistry(
                    {"fixture_adapter": FixtureAdapter(attempt)}
                ),
            ).discover(source, zoo=zoo)
    assert calls == []


def test_custom_adapter_context_honors_robots_even_when_raw_fetcher_bypass_is_enabled():
    zoo = Zoo(id="z1", slug="test-zoo", website_url="https://official.example/")
    source = Source(
        id="custom",
        zoo_id="z1",
        kind="custom_adapter",
        adapter="fixture_adapter",
        url="https://official.example/custom",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    requested_url = "https://official.example/private/adapter-data"
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nDisallow: /private\n")
        raise AssertionError("robots must block adapter content")

    class FixtureAdapter:
        def discover(self, source, *, fetcher=None):
            fetcher.fetch(requested_url)
            return []

    with pytest.raises(RobotsDisallowed):
        DiscoveryEngine(
            Fetcher(transport=transport, respect_robots=False, delay=0, retries=0),
            adapter_registry=AdapterRegistry({"fixture_adapter": FixtureAdapter()}),
        ).discover(source, zoo=zoo)
    assert calls == ["https://official.example/robots.txt"]


def test_feed_canonical_is_preserved_and_probed_before_second_article_fetch():
    zoo = Zoo(id="z1", slug="test-zoo", name="Test Zoo", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    discovered_url = "https://official.example/news/alternate?utm_source=feed"
    canonical_url = "https://official.example/news/canonical"
    parsed_item = ParsedFeedItem(url=discovered_url, canonical_url=canonical_url)
    candidate = _as_candidate(parsed_item, source)
    assert candidate.url == "https://official.example/news/alternate"
    assert candidate.canonical_url == canonical_url

    class CanonicalDiscovery:
        def discover(self, configured_source, *, zoo=None):
            return DiscoveryResult([
                DiscoveryCandidate(
                    discovered_url,
                    configured_source,
                    canonical_url=canonical_url,
                )
            ])

    article = "<html><head><title>Canonical</title></head><article>Body</article></html>"
    fetcher = FakeFetcher({"https://official.example/news/alternate": article})
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    )
    crawler.discovery = CanonicalDiscovery()

    first = crawler.crawl()
    second = crawler.crawl()

    assert first.stored_count == 1
    assert second.fetched_count == 0
    assert second.already_known_count == 1
    assert fetcher.calls.count("https://official.example/news/alternate") == 1
    article_record = storage.list_articles()[0]
    assert article_record.canonical_url == canonical_url
    assert storage.list_discoveries()[0].discovered_url == "https://official.example/news/alternate"


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
    feed = (Path(__file__).parent / "fixtures" / "rss_duplicate_v1.xml").read_text(
        encoding="utf-8"
    )
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
            "https://official.example/news/rss-duplicate": "<html><article>RSS duplicate</article></html>",
        }),
    ).crawl()
    assert crawler_result.stats[0].duplicate_candidate_count == 1


def _bronx_wcs_source(*, identity_filter=True):
    config = {
        "official_host": "newsroom.wcs.org",
        "allow_regex": r"^https://newsroom\.wcs\.org/News-Releases/articleType/ArticleView/",
        "exclude_regex": [r"^https://newsroom\.wcs\.org/DesktopModules/"],
    }
    if identity_filter:
        config["identity_filter"] = {"required_text_any": ["Bronx Zoo", "Bronx, NY"]}
    return Source(
        id="bronx-zoo-wcs-rss",
        zoo_id="z1",
        kind="rss",
        url="https://newsroom.wcs.org/DesktopModules/DnnForge-%20NewsArticles/Rss.aspx?TabID=13614&ModuleID=28242&MaxCount=25",
        config=config,
    )


def test_shared_wcs_feed_identity_filter_keeps_matching_bronx_items_only():
    zoo = Zoo(id="z1", slug="bronx-zoo", website_url="https://bronxzoo.com/")
    source = _bronx_wcs_source()
    fixture = (Path(__file__).parent / "fixtures" / "rss_bronx_wcs_mixed_v1.xml").read_text(
        encoding="utf-8"
    )
    fetcher = FakeFetcher({source.url: FetchResponse(source.url, 200, fixture.encode("utf-8"))})

    result = DiscoveryEngine(fetcher).discover(source, zoo=zoo)

    assert [item.url for item in result] == [
        "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/101",
        "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/102",
    ]
    assert fetcher.calls == [source.url]


def test_identity_filter_matches_feed_metadata_but_not_candidate_url():
    zoo = Zoo(id="z1", website_url="https://newsroom.wcs.org/")
    source = _bronx_wcs_source()
    source.config["identity_filter"] = {"required_text_any": ["Bronx Zoo"]}
    values = [
        DiscoveryCandidate(
            "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/bronx-url",
            source,
            title="Unrelated WCS item",
        ),
        DiscoveryCandidate(
            "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/metadata",
            source,
            title="Unrelated WCS item",
            metadata={"department": "Bronx Zoo"},
        ),
    ]

    result = DiscoveryEngine._filter_candidates(values, source, zoo)

    assert [item.url for item in result] == [
        "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/metadata",
    ]


@pytest.mark.parametrize(
    "identity_filter",
    [
        None,
        {},
        {"required_text_any": []},
        {"required_text_any": "Bronx Zoo"},
        {"required_text_any": ["   "]},
        {"required_text_any": ["Bronx Zoo", 42]},
        {"required_text_any": ["Bronx Zoo"], "unexpected": True},
    ],
)
def test_malformed_identity_filter_fails_before_feed_request(identity_filter):
    zoo = Zoo(id="z1", website_url="https://newsroom.wcs.org/")
    source = _bronx_wcs_source()
    source.config["identity_filter"] = identity_filter
    fetcher = FakeFetcher({})

    with pytest.raises(ValueError, match="identity_filter"):
        DiscoveryEngine(fetcher).discover(source, zoo=zoo)
    assert fetcher.calls == []


def test_sources_without_identity_filter_keep_all_feed_items_for_compatibility():
    zoo = Zoo(id="z1", website_url="https://newsroom.wcs.org/")
    source = _bronx_wcs_source(identity_filter=False)
    fixture = (Path(__file__).parent / "fixtures" / "rss_bronx_wcs_mixed_v1.xml").read_text(
        encoding="utf-8"
    )
    result = DiscoveryEngine(
        FakeFetcher({source.url: FetchResponse(source.url, 200, fixture.encode("utf-8"))})
    ).discover(source, zoo=zoo)

    assert len(result) == 3


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

    if merge_by == "canonical":
        # A canonical identity collision with changed content is an update,
        # not a duplicate candidate.
        assert (result.stored_count, result.updated_count, result.duplicate_filtered_count) == (1, 1, 0)
    else:
        # An identical content hash remains a same-run duplicate.
        assert (result.stored_count, result.updated_count, result.duplicate_filtered_count) == (1, 0, 1)
    assert result.already_known_count == 0
    assert len(storage.list_articles()) == 1
    assert len(storage.list_discoveries()) == 2


def test_second_identical_crawl_is_already_known_not_updated_or_duplicate():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    article_url = "https://official.example/news/one"
    fetcher = FakeFetcher({
        source.url: f"<rss><channel><item><link>{article_url}</link></item></channel></rss>",
        article_url: "<html><article>Same evidence</article></html>",
    })
    storage = SQLiteStorage(":memory:")
    crawler = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    )

    first = crawler.crawl()
    second = crawler.crawl()

    assert first.stored_count == 1
    assert (second.stored_count, second.already_known_count) == (0, 1)
    assert second.updated_count == 0
    assert second.duplicate_filtered_count == 0
    assert len(storage.list_articles()) == 1


def test_since_days_filters_old_candidate_before_article_fetch(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("zoofan.crawler._now", lambda: now)
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    article_url = "https://official.example/news/old"
    fetcher = FakeFetcher({
        source.url: (
            "<rss><channel><item><pubDate>2020-01-01T00:00:00Z</pubDate>"
            f"<link>{article_url}</link></item></channel></rss>"
        ),
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl(since_days=30)

    assert fetcher.calls == [source.url]
    assert (result.discovered_count, result.fetched_count, result.parsed_count) == (1, 0, 0)
    assert (result.inserted_count, result.updated_count, result.failed_count) == (0, 0, 0)
    assert result.already_known_count == 0
    assert result.duplicate_filtered_count == 0
    assert result.since_filtered_count == 1
    assert result.stats[0].metadata["since_filtered_count"] == 1
    assert result.as_dict()["since_filtered"] == 1
    assert result.zoo_results[0].metadata["since_filtered"] == 1
    assert storage.list_articles() == []


def test_since_days_filters_old_article_after_fetch_without_upsert(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("zoofan.crawler._now", lambda: now)
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    article_url = "https://official.example/news/old"
    fetcher = FakeFetcher({
        source.url: f"<rss><channel><item><link>{article_url}</link></item></channel></rss>",
        article_url: (
            "<html><head><title>Old</title>"
            "<meta property='article:published_time' content='2020-01-01T00:00:00Z'>"
            "</head><article>Old body</article></html>"
        ),
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl(since_days=30)

    assert fetcher.calls == [source.url, article_url]
    assert (result.fetched_count, result.parsed_count) == (1, 1)
    assert result.inserted_count == 0
    assert result.since_filtered_count == 1
    assert result.failed_count == 0
    assert storage.list_articles() == []


def test_since_days_keeps_article_with_missing_publication_date(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("zoofan.crawler._now", lambda: now)
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    article_url = "https://official.example/news/unknown-date"
    fetcher = FakeFetcher({
        source.url: f"<rss><channel><item><link>{article_url}</link></item></channel></rss>",
        article_url: "<html><head><title>Unknown date</title></head><article>Body</article></html>",
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl(since_days=30)

    assert result.inserted_count == 1
    assert result.since_filtered_count == 0
    assert storage.list_articles()[0].published_at is None


def test_since_days_rejects_negative_values():
    config = _config()
    with pytest.raises(ValueError, match="non-negative"):
        Crawler(config, storage=SQLiteStorage(":memory:"), fetcher=FakeFetcher({})).crawl(
            since_days=-1
        )


def test_since_days_filtering_does_not_abort_following_zoo(monkeypatch):
    now = datetime(2025, 1, 10, tzinfo=timezone.utc)
    monkeypatch.setattr("zoofan.crawler._now", lambda: now)
    first_zoo = Zoo(id="old-zoo", slug="old-zoo", website_url="https://old.example/")
    second_zoo = Zoo(id="new-zoo", slug="new-zoo", website_url="https://new.example/")
    old_source = Source(
        id="old-rss",
        zoo_id="old-zoo",
        kind="rss",
        url="https://old.example/feed",
        config={"official_host": "old.example", "allow_regex": r"/news/"},
    )
    new_source = Source(
        id="new-rss",
        zoo_id="new-zoo",
        kind="rss",
        url="https://new.example/feed",
        config={"official_host": "new.example", "allow_regex": r"/news/"},
    )
    old_url = "https://old.example/news/old"
    new_url = "https://new.example/news/new"
    fetcher = FakeFetcher({
        old_source.url: (
            "<rss><channel><item><pubDate>2020-01-01T00:00:00Z</pubDate>"
            f"<link>{old_url}</link></item></channel></rss>"
        ),
        new_source.url: f"<rss><channel><item><link>{new_url}</link></item></channel></rss>",
        new_url: "<html><head><title>New</title></head><article>New body</article></html>",
    })
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[first_zoo, second_zoo], sources=[old_source, new_source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl(since_days=30)

    assert [item.zoo_id for item in result.zoo_results] == ["old-zoo", "new-zoo"]
    assert result.since_filtered_count == 1
    assert result.inserted_count == 1
    assert storage.list_articles()[0].url == new_url


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


def test_runtime_date_and_content_errors_are_classified_without_fabricating_missing_date():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="rss",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    bad_date = "https://official.example/news/bad-date"
    empty_content = "https://official.example/news/empty-content"
    feed = (
        "<rss><channel>"
        f"<item><link>{bad_date}</link></item>"
        f"<item><link>{empty_content}</link></item>"
        "</channel></rss>"
    )
    fixture_root = Path(__file__).parent / "fixtures"
    fetcher = FakeFetcher(
        {
            source.url: feed,
            bad_date: (fixture_root / "article_bad_date_v1.html").read_text(encoding="utf-8"),
            empty_content: (fixture_root / "article_empty_content_v1.html").read_text(encoding="utf-8"),
        }
    )
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[source]), storage=storage, fetcher=fetcher
    ).crawl()

    stat = result.stats[0]
    assert stat.metadata["error_classifications"] == [
        "date_parse_error",
        "content_parse_error",
    ]
    assert result.error_classifications == [
        "date_parse_error",
        "content_parse_error",
    ]
    assert "31. Februar 2025" in stat.errors[0]
    assert "article content" in stat.errors[1].lower()
    assert result.as_dict()["error_classifications"] == [
        "date_parse_error",
        "content_parse_error",
    ]
    assert storage.list_articles() == []


def test_explicit_javascript_requirement_is_recorded_and_does_not_abort_other_source():
    zoo = Zoo(id="z1", website_url="https://official.example/")
    js_source = Source(
        id="js",
        zoo_id="z1",
        kind="archive",
        url="https://official.example/js-archive",
        config={
            "official_host": "official.example",
            "allow_regex": r"/news/",
            "requires_javascript": True,
        },
    )
    good_source = Source(
        id="good",
        zoo_id="z1",
        kind="rss",
        url="https://official.example/good-feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    good_article = "https://official.example/news/good"
    fetcher = FakeFetcher(
        {
            good_source.url: f"<rss><channel><item><link>{good_article}</link></item></channel></rss>",
            good_article: "<html><head><title>Good</title></head><article>Good body</article></html>",
        }
    )
    storage = SQLiteStorage(":memory:")

    result = Crawler(
        CrawlerConfig(zoos=[zoo], sources=[js_source, good_source]),
        storage=storage,
        fetcher=fetcher,
    ).crawl()

    js_stat = next(stat for stat in result.stats if stat.source_id == "js")
    good_stat = next(stat for stat in result.stats if stat.source_id == "good")
    assert js_stat.metadata["error_classifications"] == ["javascript_required"]
    assert "explicitly configured" in js_stat.error
    assert good_stat.stored_count == 1
    assert result.error_classifications == ["javascript_required"]
    assert js_source.url not in fetcher.calls


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
