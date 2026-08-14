import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zoofan.parsers import parse_archive, parse_archive_page, parse_article_page, parse_datetime, parse_feed, parse_json_api, parse_sitemap
import http.server
import threading

import pytest

from zoofan.fetcher import (
    FetchResponse, Fetcher, RequestBoundaryError, RequestPolicy, RobotsUnavailable,
)
from zoofan.discovery import DiscoveryEngine, DiscoveryError
from zoofan.models import Source, Zoo
from zoofan.statuses import ContentParseError, DateParseError, JavascriptRequiredError, classify_error


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return (FIXTURES / name).read_bytes()


def _fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025-04-03", "2025-04-03"),
        ("2025-04-03T09:30:00Z", "2025-04-03T09:30:00+00:00"),
        ("Thu, 03 Apr 2025 09:30:00 GMT", "2025-04-03T09:30:00+00:00"),
        ("7. März 2025", "2025-03-07"),
        ("3 april 2025", "2025-04-03"),
        ("5 février 2024", "2024-02-05"),
        ("12 agosto 2022", "2022-08-12"),
    ],
)
def test_parse_datetime_supports_iso_rfc_and_explicit_localized_date_only(value, expected):
    parsed = parse_datetime(value)
    assert parsed is not None
    assert parsed.isoformat() == (expected if "T" in expected else expected + "T00:00:00+00:00")


@pytest.mark.parametrize("value", ["", "not-a-date", "32 März 2025", "3 unknownmonth 2025"])
def test_parse_datetime_rejects_unknown_or_invalid_values_without_returning_source_text(value):
    assert parse_datetime(value) is None


_ISO_FRACTION_CASES = {
    "1": ("1", 100000),
    "2": ("48", 480000),
    "3": ("481", 481000),
    "6": ("481234", 481234),
    # Python datetime stores microseconds only; the seventh through ninth
    # digits are intentionally truncated rather than rounded.
    "9": ("481234567", 481234),
}


@pytest.mark.parametrize("suffix", ["Z", "+02:00"], ids=["utc", "offset"])
@pytest.mark.parametrize("fraction_length", list(_ISO_FRACTION_CASES), ids=lambda value: f"fraction-{value}")
def test_parse_datetime_normalizes_one_to_nine_fractional_digits(suffix, fraction_length):
    fraction, microsecond = _ISO_FRACTION_CASES[fraction_length]
    parsed = parse_datetime(f"2024-03-18T12:15:25.{fraction}{suffix}")
    expected_timezone = timezone.utc if suffix == "Z" else timezone(timedelta(hours=2))
    assert parsed == datetime(2024, 3, 18, 12, 15, 25, microsecond, tzinfo=expected_timezone)


@pytest.mark.parametrize(
    "value",
    [
        "2024-03-18T12:15:25.Z",
        "2024-03-18T12:15:25.1234567890Z",
        "2024-03-18T12:15:25.12abcZ",
        "2024-03-18T12:15:25.12+2:00",
        "2024-03-18T12:15:25.12+0200",
        "2024-03-18T12:15:25.12+02",
        "2024-03-18T12:15:25.12+99:00",
        "2024-03-18T12:15:25.12+02:60",
    ],
)
def test_parse_datetime_rejects_malformed_fraction_or_offset(value):
    assert parse_datetime(value) is None


def test_copenhagen_sitemap_fractional_lastmod_parses_and_preserves_raw_evidence():
    raw_lastmod = "2024-03-18T12:15:25.48Z"
    payload = f"""
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://www.zoo.dk/nyheder/foraar-i-zoologisk-have</loc>
            <lastmod>{raw_lastmod}</lastmod>
          </url>
        </urlset>
    """

    entry = parse_sitemap(payload, "https://www.zoo.dk/sitemap.xml")[0]

    assert entry.lastmod == datetime(2024, 3, 18, 12, 15, 25, 480000, tzinfo=timezone.utc)
    assert entry.metadata["lastmod_raw"] == raw_lastmod
    assert "date_parse_error" not in entry.metadata.get("error_classifications", [])


def test_versioned_feed_and_sitemap_fixtures_keep_relative_urls_and_raw_date_evidence():
    items = parse_feed(_fixture("rss_mixed_v1.xml"), "https://www.zoo-berlin.de/de/aktuelles/news")
    assert [item.url for item in items] == [
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/jungtier-im-regenwald",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/lokales-datum",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/ohne-datum",
    ]
    assert items[0].published_at.isoformat() == "2025-04-03T09:30:00+00:00"
    assert items[1].published_at.isoformat() == "2025-03-07T00:00:00+00:00"
    assert items[2].published_at is None
    assert items[2].metadata["published_at_raw"] == "noch nicht veröffentlicht"
    assert parse_feed(_fixture("atom_empty_v1.xml")) == []

    entries = parse_sitemap(_fixture("sitemap_v1.xml"), "https://www.zoo-berlin.de/sitemap.xml")
    assert entries[0].url.endswith("/jungtier-im-regenwald")
    assert entries[0].lastmod.isoformat() == "2025-04-03T00:00:00+00:00"
    assert entries[1].lastmod is None
    assert entries[1].metadata["lastmod_raw"] == "not-a-date"


def test_saved_atom_fixture_covers_normal_relative_missing_and_duplicate_evidence():
    normal = parse_feed(_fixture("atom_normal_v1.xml"), "https://www.zoo-berlin.de/de/aktuelles/news")
    assert len(normal) == 1
    assert normal[0].url.endswith("/artikel/atom-one")
    assert normal[0].published_at.isoformat() == "2025-04-03T09:30:00+00:00"
    assert normal[0].updated_at_source.isoformat() == "2025-04-03T10:00:00+00:00"
    assert normal[0].author == "Zoo Berlin"

    missing = parse_feed(_fixture("atom_missing_date_v1.xml"), "https://www.zoo-berlin.de/news")
    assert missing[0].published_at is None
    assert "published_at_raw" not in missing[0].metadata
    duplicate = parse_feed(_fixture("atom_duplicate_v1.xml"), "https://www.zoo-berlin.de/news")
    assert [item.url for item in duplicate] == [
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/ein-artikel",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/ein-artikel",
    ]


def test_saved_rss_duplicate_fixture_leaves_duplicate_suppression_to_discovery():
    items = parse_feed(_fixture("rss_duplicate_v1.xml"), "https://official.example/feed")
    assert len(items) == 2
    assert [item.url for item in items] == [
        "https://official.example/news/rss-duplicate",
        "https://official.example/news/rss-duplicate",
    ]
    assert items[0].title == "RSS duplicate article"
    assert items[1].title == "RSS duplicate article (tracking)"
    assert items[0].published_at.isoformat() == "2025-04-03T09:30:00+00:00"
    assert items[1].published_at.isoformat() == "2025-04-03T00:00:00+00:00"


def test_json_api_fixture_handles_paths_relative_urls_noise_duplicates_and_missing_dates():
    items = parse_json_api(
        _fixture("json_api_v1.json"),
        {
            "items_path": "data.items",
            "fields": {
                "url": "links.html",
                "title": "headline",
                "date": "publishedAt",
                "summary": "summary",
            },
        },
        "https://official.example/api/news",
    )
    # The tracking-parameter duplicate is normalized to the first article;
    # records without a usable link are parser noise and are skipped.
    assert [item.url for item in items] == [
        "https://official.example/news/rainforest-resident",
        "https://official.example/news/date-to-be-announced",
        "https://official.example/",
        "https://outside.example/news/not-official",
    ]
    assert items[0].title == "New rainforest resident"
    assert items[0].published_at.isoformat() == "2025-04-03T09:30:00+00:00"
    assert items[1].published_at is None
    assert items[1].metadata["published_at_raw"] == "not-a-date"
    assert items[0].summary == "A new resident arrived."
    assert parse_json_api(_fixture("json_api_empty_v1.json"), {"items_path": "data.items"}) == []


def test_json_api_minor_schema_drift_uses_safe_default_fields_and_localized_date():
    items = parse_json_api(_fixture("json_api_drift_v1.json"), base_url="https://official.example/api/news")
    assert len(items) == 1
    assert items[0].url == "https://official.example/news/minor-drift"
    assert items[0].title == "Minor schema drift"
    assert items[0].published_at.date().isoformat() == "2025-03-07"
    assert items[0].summary == "The API changed only its collection and link shape."


def test_saved_sitemap_fixture_matrix_covers_empty_duplicate_and_benign_drift_without_dates():
    base_url = "https://official.example/sitemap.xml"
    assert parse_sitemap(_fixture("sitemap_empty_v1.xml"), base_url) == []

    duplicate = parse_sitemap(_fixture("sitemap_duplicate_v1.xml"), base_url)
    assert [entry.url for entry in duplicate] == [
        "https://official.example/news/sitemap-duplicate",
        "https://official.example/news/sitemap-duplicate",
        "https://official.example/news/sitemap-second",
    ]
    assert all(entry.lastmod is None for entry in duplicate)

    drift = parse_sitemap(_fixture("sitemap_benign_drift_v1.xml"), base_url)
    assert [entry.url for entry in drift] == [
        "https://official.example/news/sitemap-drift",
        "https://official.example/news/sitemap-drift-two",
    ]
    # Sitemap publication dates are optional; the drift fixture intentionally
    # contains no lastmod value rather than inventing one for matrix parity.
    assert all(entry.lastmod is None for entry in drift)


def test_versioned_archive_fixtures_skip_unrelated_cards_and_deduplicate_news():
    berlin = parse_archive_page(
        _fixture("berlin_archive_v1.html"),
        {
            "article_selector": "article.news-grid-item",
            "link_selector": 'a[href*="/de/aktuelles/news/artikel/"]',
            "title_selector": "[itemprop='headline']",
            "date_selector": "time[itemprop='datePublished']",
        },
        "https://www.zoo-berlin.de/de/aktuelles/news",
    )
    assert [(item.url, item.title) for item in berlin] == [
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/jungtier-im-regenwald", "Jungtier im Regenwald"),
    ]

    gaia = parse_archive_page(
        _fixture("gaia_archive_v1.html"),
        {"article_selector": "article.card", "link_selector": "a[href]", "title_selector": ".card-title", "date_selector": ".date"},
        "https://www.gaiazoo.nl/nieuws-vlogs/",
    )
    assert [item.url for item in gaia] == [
        "https://www.gaiazoo.nl/nieuws/een-nieuwe-bewoner",
        "https://www.gaiazoo.nl/nieuws/zonder-datum",
    ]
    assert gaia[0].published_at.isoformat() == "2025-04-03T00:00:00+00:00"
    assert gaia[1].published_at is None


def test_archive_benign_structural_drift_still_extracts_article_and_localized_date():
    items = parse_archive_page(
        _fixture("archive_benign_drift_v1.html"),
        {
            "article_selector": ".news-grid-item",
            "link_selector": "a.headline-link",
            "title_selector": ".headline",
            "date_selector": ".published",
        },
        "https://www.zoo-berlin.de/de/aktuelles/news",
    )
    assert [(item.url, item.title) for item in items] == [
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/drift-one", "Strukturelle Änderung")
    ]
    assert items[0].published_at.isoformat() == "2025-04-03T00:00:00+00:00"


def _archive_pagination_config():
    return {
        "article_selector": "article.news-grid-item",
        "link_selector": "a[href]",
        "title_selector": "[itemprop='headline']",
        "pagination_selector": "a.next-page[href]",
        "max_pages": 2,
    }


def test_archive_pagination_preserves_raw_typo3_query_for_transport_but_normalizes_candidates():
    root_url = "https://www.zoo-berlin.de/de/aktuelles/news"
    next_url = (
        "https://www.zoo-berlin.de/de/aktuelles/news"
        "?gclid=abc123&_gl=1%2Aabc%2Adef&cHash=dead%2Bbeef&currentPage=2"
    )
    calls = []

    def fetch_page(target):
        calls.append(target)
        assert target == next_url
        return _fixture("berlin_archive_pagination_page2_v1.html")

    items = parse_archive(
        _fixture("berlin_archive_pagination_root_v1.html"),
        _archive_pagination_config(),
        root_url,
        fetch_page=fetch_page,
    )

    assert calls == [next_url]
    assert [item.url for item in items] == [
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/seite-eins",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/seite-zwei",
    ]


class _ArchivePolicyFetcher:
    supports_request_policy = True

    def __init__(self, root_url, root_payload, *, page_url=None, page_payload=None):
        self.root_url = root_url
        self.root_payload = root_payload
        self.page_url = page_url
        self.page_payload = page_payload
        self.calls = []

    def fetch(self, url, *, request_policy):
        self.calls.append(url)
        if url == self.root_url:
            return FetchResponse(url, 200, self.root_payload)
        if self.page_url is not None and url == self.page_url:
            return FetchResponse(url, 200, self.page_payload)
        raise AssertionError(f"unexpected transport request: {url}")


def _archive_source(root_url, *, source_id="berlin-archive"):
    return Source(
        id=source_id,
        zoo_id="zoo-berlin",
        kind="archive",
        url=root_url,
        config={
            **_archive_pagination_config(),
            "official_host": "www.zoo-berlin.de",
            "max_pages": 2,
        },
    )


def test_archive_discovery_passes_raw_pagination_query_through_request_policy():
    root_url = "https://www.zoo-berlin.de/de/aktuelles/news"
    next_url = (
        "https://www.zoo-berlin.de/de/aktuelles/news"
        "?gclid=abc123&_gl=1%2Aabc%2Adef&cHash=dead%2Bbeef&currentPage=2"
    )
    fetcher = _ArchivePolicyFetcher(
        root_url,
        _fixture("berlin_archive_pagination_root_v1.html"),
        page_url=next_url,
        page_payload=_fixture("berlin_archive_pagination_page2_v1.html"),
    )
    zoo = Zoo(id="zoo-berlin", website_url="https://www.zoo-berlin.de/")
    result = DiscoveryEngine(fetcher).discover(_archive_source(root_url), zoo=zoo)

    assert fetcher.calls == [root_url, next_url]
    assert [candidate.url for candidate in result] == [
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/seite-eins",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/seite-zwei",
    ]


def test_archive_discovery_rejects_cross_domain_pagination_before_transport():
    root_url = "https://www.zoo-berlin.de/de/aktuelles/news"
    evil_url = "https://evil.example/archive?gclid=secret&cHash=dead%2Bbeef"
    fetcher = _ArchivePolicyFetcher(
        root_url,
        _fixture("berlin_archive_pagination_external_v1.html"),
    )
    zoo = Zoo(id="zoo-berlin", website_url="https://www.zoo-berlin.de/")

    with pytest.raises(DiscoveryError, match="rejected request target"):
        DiscoveryEngine(fetcher).discover(_archive_source(root_url), zoo=zoo)
    assert fetcher.calls == [root_url]
    assert evil_url not in fetcher.calls


def test_article_content_html_uses_allowlist_and_drops_xss_url_and_event_vectors():
    article = parse_article_page(
        _fixture("article_xss_v1.html"),
        "https://example.org/stories/source",
    )
    content_html = (article.content_html or "").lower()

    # The raw response remains evidence, while the stored content view drops
    # active/metadata tags and every tested attribute/URL execution vector.
    assert "javascript:" in article.raw_html.lower()
    assert "data:text/html" in article.raw_html.lower()
    for forbidden in (
        "<svg", "<meta", "<base", "<link", "<form", "<script", "style=",
        "xlink:href", "formaction", "srcset", "javascript:", "data:",
        "onload=", "onclick=", "onerror=",
    ):
        assert forbidden not in content_html
    assert "safe text remains" in content_html
    assert '<a href="/stories/safe">safe link</a>' in content_html
    assert article.image_url == "https://example.org/images/safe.jpg"


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html,payload",
    "ftp://example.org/file",
    "https://[invalid-ipv6",
    "https://example.org:99999/file",
])
def test_feed_and_sitemap_reject_bad_urls_without_affecting_later_valid_entries(bad_url):
    feed = f"""<rss><channel>
      <item><title>Bad</title><link>{bad_url}</link></item>
      <item><title>Good</title><link>https://example.org/news/good</link>
        <link rel='canonical'>{bad_url}</link></item>
    </channel></rss>"""
    items = parse_feed(feed, "https://example.org/feed")
    assert [item.url for item in items] == ["https://example.org/news/good"]
    assert items[0].canonical_url is None

    sitemap = f"""<urlset>
      <url><loc>{bad_url}</loc></url>
      <url><loc>https://example.org/news/good</loc></url>
    </urlset>"""
    entries = parse_sitemap(sitemap, "https://example.org/sitemap.xml")
    assert [entry.url for entry in entries] == ["https://example.org/news/good"]


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html,payload",
    "ftp://example.org/file",
    "https://[invalid-ipv6",
    "https://example.org:99999/file",
])
def test_archive_rejects_bad_candidate_urls_without_affecting_later_valid_card(bad_url):
    html = f"""<main>
      <article class='card'><a href='{bad_url}'><h2>Bad</h2></a></article>
      <article class='card'><a href='https://example.org/news/good'><h2>Good</h2></a></article>
    </main>"""
    items = parse_archive_page(
        html,
        {"article_selector": ".card", "link_selector": "a[href]", "title_selector": "h2"},
        "https://example.org/archive",
    )
    assert [(item.url, item.title) for item in items] == [("https://example.org/news/good", "Good")]


def test_bad_article_date_raw_is_replaced_by_raw_value_of_valid_time_candidate():
    article = parse_article_page(
        """<html><head><meta property='og:title' content='Dated'>
        <meta property='article:published_time' content='not-a-date'></head>
        <body><article><time datetime='2025-04-03'>3 April 2025</time><p>Body</p></article></body></html>""",
        "https://example.org/news/dated",
    )
    assert article.published_at.date().isoformat() == "2025-04-03"
    assert article.published_at_raw == "2025-04-03"
    assert article.metadata["published_at_raw"] == "2025-04-03"


def test_article_bad_canonical_and_image_urls_are_ignored_without_aborting_parse():
    article = parse_article_page(
        """<html><head><meta property='og:title' content='Safe'>
        <link rel='canonical' href='https://[invalid-ipv6'>
        <link rel='canonical' href='/safe-canonical'>
        <meta property='og:image' content='https://example.org:99999/bad.jpg'></head>
        <body><article><p>Body</p><img src='/safe-image.jpg'></article></body></html>""",
        "https://example.org/news/source",
    )
    assert article.canonical_url == "https://example.org/safe-canonical"
    assert article.image_url == "https://example.org/safe-image.jpg"
    assert article.content == "Body"


@pytest.mark.parametrize(
    ("fixture", "url", "expected"),
    [
        ("article_de_v1.html", "https://www.zoo-berlin.de/de/aktuelles/news/artikel/jungtier-im-regenwald", "2025-03-07"),
        ("article_nl_v1.html", "https://www.gaiazoo.nl/nieuws/een-nieuwe-bewoner", "2025-04-03"),
        ("article_fr_v1.html", "https://example.org/fr/article", "2024-02-05"),
        ("article_es_v1.html", "https://example.org/es/article", "2022-08-12"),
    ],
)
def test_article_fixture_localized_dates_and_clean_content_fields(fixture, url, expected):
    article = parse_article_page(_fixture(fixture), url)
    assert article.published_at is not None
    assert article.published_at.date().isoformat() == expected
    assert article.published_at_raw
    assert article.content and article.content_html
    assert "script" not in article.content_html.lower()
    assert "style" not in article.content_html.lower()
    assert article.parse_status == "success"


def test_article_fixture_preserves_raw_html_separately_and_resolves_relative_canonical_image():
    article = parse_article_page(_fixture("article_de_v1.html"), "https://www.zoo-berlin.de/de/aktuelles/news/artikel/source")
    assert article.canonical_url == "https://www.zoo-berlin.de/de/aktuelles/news/artikel/jungtier-im-regenwald"
    assert article.image_url == "https://www.zoo-berlin.de/media/jungtier.jpg"
    assert "window.untrusted" in article.raw_html
    assert "window.untrusted" not in article.content_html
    assert "<style" in article.raw_html
    assert "<style" not in article.content_html


def test_article_json_fixture_is_consumed_as_json_ld_not_as_a_new_source_kind():
    structured = json.loads(_fixture_text("article_newsarticle_v1.json"))
    html = """
    <html><head><meta property="og:title" content="Une nouvelle arrivée">
    <script type="application/ld+json">{payload}</script></head>
    <body><article><p>Une nouvelle arrivée est à découvrir.</p></article></body></html>
    """.format(payload=json.dumps(structured, ensure_ascii=False))
    article = parse_article_page(html, "https://example.org/fr/article")
    assert article.published_at.date().isoformat() == "2024-02-05"
    assert article.image_url == "https://example.org/media/arrivee.jpg"
    assert article.author == "Zoo editorial team"
    assert article.metadata["structured_language"] == "fr"


def test_article_missing_date_keeps_raw_candidate_and_never_uses_current_time():
    article = parse_article_page(_fixture("article_missing_date_v1.html"), "https://www.gaiazoo.nl/nieuws/zonder-datum")
    assert article.published_at is None
    assert article.published_at_raw == "nog niet bekend"
    assert article.parse_status == "partial"
    assert article.content == "Bericht zonder datum De datum ontbreekt in deze bronpagina."


def test_article_bad_date_fixture_exposes_date_parse_error_without_fabricating_timestamp():
    article = parse_article_page(
        _fixture("article_bad_date_v1.html"),
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/ungultiges-datum",
    )
    assert article.published_at is None
    assert article.published_at_raw == "31. Februar 2025"
    assert article.metadata["error_classification"] == "date_parse_error"
    assert article.metadata["error_classifications"] == ["date_parse_error"]
    assert "31. Februar 2025" in article.metadata["error_details"][0]
    assert article.parse_status == "partial"


def test_article_empty_content_fixture_exposes_content_parse_error():
    article = parse_article_page(
        _fixture("article_empty_content_v1.html"),
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/empty-shell",
    )
    assert article.content is None
    assert article.metadata["error_classification"] == "content_parse_error"
    assert article.metadata["error_classifications"] == ["content_parse_error"]
    assert article.parse_status == "content_parse_error"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (DateParseError("source date text: 31. Februar 2025"), "date_parse_error"),
        (ContentParseError("article content root was empty"), "content_parse_error"),
        (JavascriptRequiredError("source is explicitly configured for JavaScript"), "javascript_required"),
        (ValueError("date_parse_error: source field was malformed"), "date_parse_error"),
    ],
)
def test_runtime_classifier_preserves_specific_parser_categories(error, expected):
    assert classify_error(error, phase="article") == expected


KOELN_ARTICLE_DATE_CONFIG = {
    "article_date_selector": "main#brx-content p",
    "article_date_pattern": r"^(?:Köln|Koeln|Cologne),?\s+(?P<day>\d{1,2})\.?\s+(?P<month>[A-Za-zÄÖÜäöüß]+)\s+(?P<year>\d{4})(?:\b|$)",
}


def test_atom_namespace_feed_prefers_link_and_parses_date():
    payload = """<?xml version='1.0'?>
    <feed xmlns='http://www.w3.org/2005/Atom'>
      <entry><title>News</title>
        <link rel='alternate' href='/news/one/' />
        <published>2024-01-02T03:04:05Z</published>
        <summary>Summary</summary>
      </entry>
    </feed>"""
    items = parse_feed(payload, "https://example.org/feed")
    assert len(items) == 1
    assert items[0].url == "https://example.org/news/one"
    assert items[0].title == "News"
    assert items[0].published_at.year == 2024


def test_sitemap_index_and_urlset_namespace():
    index = "<sitemapindex xmlns='x'><sitemap><loc>/posts.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(index, "https://example.org/sitemap.xml")[0].is_index
    urls = "<urlset xmlns='x'><url><loc>/news/a/</loc><lastmod>2024-02-01</lastmod></url></urlset>"
    entries = parse_sitemap(urls, "https://example.org/sitemap.xml")
    assert entries[0].url == "https://example.org/news/a"


def test_archive_is_configurable_and_skips_cards_without_links():
    html = """<div class='card'><a href='/a'><h2 class='title'>A</h2></a><time class='date' datetime='2024-01-01'/></div>
    <div class='card'><h2 class='title'>No URL</h2></div>"""
    items = parse_archive_page(
        html,
        {"article_selector": ".card", "link_selector": "a", "title_selector": ".title", "date_selector": ".date"},
        "https://example.org/archive",
    )
    assert [(item.url, item.title) for item in items] == [("https://example.org/a", "A")]


def test_article_canonical_title_date_and_hash():
    html = """<html><head><link rel='canonical' href='/canonical/' />
      <meta property='og:title' content='Preferred title'/>
      <meta property='article:published_time' content='2024-03-04T05:06:07Z'/></head>
      <body><article><p>Body text</p></article></body></html>"""
    article = parse_article_page(html, "https://example.org/article")
    assert article.canonical_url == "https://example.org/canonical"
    assert article.title == "Preferred title"
    assert article.published_at.year == 2024
    assert article.content == "Body text"
    assert len(article.content_hash) == 64


@pytest.mark.parametrize(
    ("description_markup", "expected_summary"),
    [
        (
            """<meta name='description' content='Generic site boilerplate'>
            <meta property='og:description' content='  Article&nbsp; summary  '>""",
            "Article summary",
        ),
        (
            """<meta name='description' content='Generic site boilerplate'>
            <meta name='twitter:description' content='  Twitter article summary  '>""",
            "Twitter article summary",
        ),
        (
            "<meta name='description' content='  Generic article summary  '>",
            "Generic article summary",
        ),
        (
            """<meta property='og:description' content='   '>
            <meta name='twitter:description' content=''>
            <meta name='description' content='Generic article summary'>""",
            "Generic article summary",
        ),
    ],
)
def test_article_summary_metadata_priority_is_independent_of_dom_order(
    description_markup, expected_summary
):
    article = parse_article_page(
        f"""<html><head>
        <meta property='og:title' content='Preferred title'>
        <link rel='canonical' href='/canonical/'>
        <meta property='article:published_time' content='2024-03-04T05:06:07Z'>
        {description_markup}
        </head><body><article><p>Body text</p></article></body></html>""",
        "https://example.org/article",
    )

    assert article.summary == expected_summary
    assert article.title == "Preferred title"
    assert article.canonical_url == "https://example.org/canonical"
    assert article.published_at == datetime(2024, 3, 4, 5, 6, 7, tzinfo=timezone.utc)


def test_article_summary_metadata_uses_json_ld_after_empty_meta_values():
    article = parse_article_page(
        """<html><head>
        <meta property='og:description' content='   '>
        <meta name='twitter:description' content=''>
        <meta name='description' content='  '>
        <script type='application/ld+json'>
        {"@type":"NewsArticle", "description":"  Structured article summary  "}
        </script>
        </head><body><article><p>Body text</p></article></body></html>""",
        "https://example.org/article",
    )

    assert article.summary == "Structured article summary"


def test_article_language_evidence_distinguishes_html_and_structured_metadata():
    article = parse_article_page(
        """<html lang='de-DE'><head><meta name='language' content='nl'>
        <script type='application/ld+json'>{"@type":"NewsArticle","inLanguage":"fr"}</script>
        </head><article>Body</article></html>""",
        "https://example.org/article",
    )
    assert article.metadata["html_language"] == "de-DE"
    assert article.metadata["structured_language"] == "nl"


def test_article_rejects_untrustworthy_language_metadata():
    article = parse_article_page(
        "<html lang='not a language'><meta name='language' content='also invalid'><article>Body</article></html>",
        "https://example.org/article",
    )
    assert "html_language" not in article.metadata
    assert "structured_language" not in article.metadata


@pytest.mark.parametrize(
    ("paragraph", "expected"),
    [
        ("<p><strong>Köln, 7. März 2025</strong></p>", "2025-03-07"),
        ("<p>Koeln 18 Maerz 2024</p>", "2024-03-18"),
        ("<p>Cologne, 2 October 2023</p>", "2023-10-02"),
    ],
)
def test_configured_article_dateline_parses_strong_plain_german_and_english(paragraph, expected):
    article = parse_article_page(
        f"<main id='brx-content'>{paragraph}<p>Body</p></main>",
        "https://koelnerzoo.de/aktuellartical/news",
        KOELN_ARTICLE_DATE_CONFIG,
    )

    assert article.published_at == expected


@pytest.mark.parametrize(
    "paragraphs",
    [
        "<p>7. März 2025</p><p>Body</p>",
        "<p>Köln, 32. März 2025</p><p>Body</p>",
        "<p>Body without a date</p>",
        "<p>Lead one</p><p>Lead two</p><p>Lead three</p><p>Köln, 7. März 2025</p>",
    ],
)
def test_configured_article_dateline_rejects_unanchored_malformed_missing_and_late_dates(paragraphs):
    article = parse_article_page(
        f"<main id='brx-content'>{paragraphs}</main>",
        "https://koelnerzoo.de/aktuellartical/news",
        KOELN_ARTICLE_DATE_CONFIG,
    )

    assert article.published_at is None


def test_article_metadata_date_has_priority_over_configured_dateline():
    article = parse_article_page(
        """<meta property='article:published_time' content='2024-01-02T03:04:05Z'>
        <main id='brx-content'><p>Köln, 7. März 2025</p></main>""",
        "https://koelnerzoo.de/aktuellartical/news",
        KOELN_ARTICLE_DATE_CONFIG,
    )

    assert article.published_at.isoformat() == "2024-01-02T03:04:05+00:00"


@pytest.mark.parametrize(
    "invalid_date_markup",
    [
        "<meta property='article:published_time' content='not-a-date'>",
        """<script type='application/ld+json'>
        {"@type":"NewsArticle","datePublished":"not-a-date"}
        </script>""",
        "<time datetime='not-a-date'>Someday</time>",
    ],
)
def test_malformed_article_date_candidates_allow_configured_dateline_fallback(invalid_date_markup):
    article = parse_article_page(
        f"{invalid_date_markup}<main id='brx-content'><p>Köln, 7. März 2025</p></main>",
        "https://koelnerzoo.de/aktuellartical/news",
        KOELN_ARTICLE_DATE_CONFIG,
    )

    assert article.published_at == "2025-03-07"


@pytest.mark.parametrize(
    "invalid_date_markup",
    [
        "<meta property='article:published_time' content='not-a-date'>",
        """<script type='application/ld+json'>
        {"@type":"NewsArticle","datePublished":"not-a-date","dateModified":"also-bad"}
        </script>""",
        "<time datetime='not-a-date'>Someday</time>",
    ],
)
def test_malformed_article_date_candidates_without_valid_fallback_are_none(invalid_date_markup):
    article = parse_article_page(
        f"{invalid_date_markup}<main id='brx-content'><p>Body without dateline</p></main>",
        "https://koelnerzoo.de/aktuellartical/news",
        KOELN_ARTICLE_DATE_CONFIG,
    )

    assert article.published_at is None
    assert article.updated_at_source is None


def test_unconfigured_berlin_and_gaia_article_parsing_regressions():
    berlin = parse_article_page(
        "<article><time datetime='2025-04-03'>3. April 2025</time><p>Berlin body</p></article>",
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/example",
    )
    gaia = parse_article_page(
        "<article><p>Kerkrade, 3 april 2025</p><p>Gaia body</p></article>",
        "https://www.gaiazoo.nl/nieuws/example",
    )

    assert berlin.published_at.date().isoformat() == "2025-04-03"
    assert gaia.published_at is None


GAIA_ARTICLE_CONTENT_CONFIG = {
    "article_content_selector": "section#content.content-wrapper",
    "article_content_exclude_selector": [
        ".spacing-wrapper",
        ".page-detail",
        ".post-highlights-block",
        "style",
    ],
}


@pytest.mark.parametrize(
    ("fixture", "canonical", "unique_text"),
    [
        (
            "gaia_article_content_a_v1.html",
            "https://www.gaiazoo.nl/nieuws/nieuwe-leeuwenman",
            "nieuwe leeuwenman simba",
        ),
        (
            "gaia_article_content_b_v1.html",
            "https://www.gaiazoo.nl/nieuws/jonge-tapir",
            "jonge tapir geboren",
        ),
    ],
)
def test_configured_gaia_article_content_uses_body_root_and_excludes_related_cards(
    fixture, canonical, unique_text
):
    article = parse_article_page(
        _fixture(fixture),
        f"{canonical}/source",
        GAIA_ARTICLE_CONTENT_CONFIG,
    )

    assert article.canonical_url == canonical
    assert unique_text in (article.content or "").lower()
    assert "ontdek meer" not in (article.content or "").lower()
    assert "andere nieuwskaart" not in (article.content or "").lower()
    assert "spacing-wrapper" not in (article.content_html or "").lower()
    assert "page-detail" not in (article.content_html or "").lower()
    assert "post-highlights-block" not in (article.content_html or "").lower()
    assert "<style" not in (article.content_html or "").lower()
    assert article.content_hash and len(article.content_hash) == 64
    assert article.parse_status == "success"


def test_configured_gaia_article_variants_have_distinct_content_hashes():
    first = parse_article_page(
        _fixture("gaia_article_content_a_v1.html"),
        "https://www.gaiazoo.nl/nieuws/nieuwe-leeuwenman",
        GAIA_ARTICLE_CONTENT_CONFIG,
    )
    second = parse_article_page(
        _fixture("gaia_article_content_b_v1.html"),
        "https://www.gaiazoo.nl/nieuws/jonge-tapir",
        GAIA_ARTICLE_CONTENT_CONFIG,
    )
    assert first.content_hash != second.content_hash


def test_unconfigured_article_does_not_treat_card_or_news_grid_item_as_body():
    article = parse_article_page(
        """<html><head>
        <meta property='og:title' content='Nieuwsbericht'>
        <meta name='description' content='Samenvatting'>
        <link rel='canonical' href='/nieuws/bericht'>
        </head><body>
          <article class='card'><a href='/nieuws/related'>Ontdek meer</a></article>
          <article class='news-grid-item'><a href='/nieuws/other'>Andere nieuwskaart</a></article>
        </body></html>""",
        "https://www.gaiazoo.nl/nieuws/bericht",
    )
    assert article.content is None
    assert article.content_html is None
    assert article.content_hash is None
    assert article.parse_status in {"partial", "content_parse_error"}
    assert article.title == "Nieuwsbericht"
    assert article.canonical_url == "https://www.gaiazoo.nl/nieuws/bericht"
    assert article.summary == "Samenvatting"
    assert "Ontdek meer" in (article.raw_html or "")


def test_generic_main_cleanup_handles_nested_teasers_decomposed_by_parent():
    article = parse_article_page(
        _fixture("generic_nested_teaser_v1.html"),
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/berlin-body",
    )
    assert article.content and "Berlin body text remains" in article.content
    assert "Related teaser" not in article.content
    assert "Startseite" not in article.content
    assert article.content_hash and article.parse_status == "success"


def test_generic_berlin_main_article_with_nested_navigation_returns_body():
    article = parse_article_page(
        _fixture("berlin_article_main_v1.html"),
        "https://www.zoo-berlin.de/de/aktuelles/news/artikel/jungtier-im-regenwald",
    )
    assert article.content and "Jungtier erkundet" in article.content
    assert article.content_hash


def test_fetcher_robots_failure_fails_closed_and_redirect_checks_target():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url == "https://one.example/robots.txt":
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /")
        if url == "https://one.example/start":
            return FetchResponse(url, 302, b"", {"Location": "https://two.example/final"})
        if url == "https://two.example/robots.txt":
            return FetchResponse(url, 503, b"")
        return FetchResponse(url, 200, b"ok")

    fetcher = Fetcher(transport=transport, delay=0, retries=0)
    try:
        fetcher.fetch("https://one.example/start")
    except RobotsUnavailable:
        pass
    else:
        raise AssertionError("redirect target robots failure must fail closed")
    assert "https://two.example/robots.txt" in calls


def _request_policy(*hosts):
    zoo = Zoo(id="z1", website_url="https://official.example/")
    source = Source(
        id="source-one", zoo_id="z1", url="https://official.example/feed",
        config={"official_hosts": list(hosts or ("official.example",))},
    )
    return RequestPolicy.from_zoo_source(zoo, source)


def test_request_boundary_rejects_external_redirect_before_robots_or_content_transport():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /")
        return FetchResponse(url, 302, b"", {"Location": "https://evil.example/steal?token=secret"})

    fetcher = Fetcher(transport=transport, delay=0, retries=0)
    with pytest.raises(RequestBoundaryError) as exc_info:
        fetcher.fetch("https://official.example/start", request_policy=_request_policy())

    assert calls == ["https://official.example/robots.txt", "https://official.example/start"]
    assert "source-one" in str(exc_info.value)
    assert "https://evil.example/steal" in str(exc_info.value)
    assert "secret" not in str(exc_info.value)


def test_request_boundary_allows_official_redirect_and_checks_robots_before_next_hop():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /")
        if url.endswith("/start"):
            return FetchResponse(url, 302, b"", {"Location": "/final"})
        return FetchResponse(url, 200, b"ok")

    response = Fetcher(transport=transport, delay=0, retries=0).fetch(
        "https://official.example/start", request_policy=_request_policy()
    )
    assert response.content == b"ok"
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/start",
        "https://official.example/final",
    ]


def test_content_trailing_slash_redirect_preserves_transport_target_without_loop():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url == "https://official.example/start":
            return FetchResponse(url, 301, b"", {"Location": "/start/"})
        if url == "https://official.example/start/":
            return FetchResponse(url, 200, b"slash target")
        raise AssertionError(url)

    response = Fetcher(
        transport=transport, respect_robots=False, delay=0, max_redirects=2,
    ).fetch("https://official.example/start", request_policy=_request_policy())

    assert response.url == "https://official.example/start/"
    assert response.content == b"slash target"
    assert calls == ["https://official.example/start", "https://official.example/start/"]


def test_request_boundary_preserves_meaningful_query_order_and_trailing_slash():
    calls = []
    target = "https://OFFICIAL.example/search/?signature=z%2B1&cursor=a%2Fb&x=2&x=1#ignored"
    expected = "https://official.example/search/?signature=z%2B1&cursor=a%2Fb&x=2&x=1"

    def transport(url, headers, timeout):
        calls.append(url)
        return FetchResponse(url, 200, b"ok")

    response = Fetcher(transport=transport, respect_robots=False, delay=0).fetch(
        target, request_policy=_request_policy()
    )
    assert response.url == expected
    assert calls == [expected]


def test_robots_trailing_slash_redirect_preserves_target_and_fetches_content_once():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url == "https://official.example/robots.txt":
            return FetchResponse(url, 301, b"", {"Location": "/robots.txt/"})
        if url == "https://official.example/robots.txt/":
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /")
        if url == "https://official.example/start":
            return FetchResponse(url, 200, b"content")
        raise AssertionError(url)

    response = Fetcher(transport=transport, delay=0, max_redirects=2).fetch(
        "https://official.example/start", request_policy=_request_policy()
    )

    assert response.content == b"content"
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/robots.txt/",
        "https://official.example/start",
    ]


def test_request_boundary_rejects_response_url_escape_and_redirect_exhaustion():
    escaped_calls = []

    def escaped(url, headers, timeout):
        escaped_calls.append(url)
        return FetchResponse("https://evil.example/final", 200, b"escaped")

    with pytest.raises(RequestBoundaryError):
        Fetcher(transport=escaped, respect_robots=False, delay=0).fetch(
            "https://official.example/start", request_policy=_request_policy()
        )
    assert escaped_calls == ["https://official.example/start"]

    redirect_calls = []

    def redirects(url, headers, timeout):
        redirect_calls.append(url)
        return FetchResponse(url, 302, b"", {"Location": "/again"})

    with pytest.raises(RequestBoundaryError, match="redirect limit exceeded"):
        Fetcher(transport=redirects, respect_robots=False, delay=0, max_redirects=1).fetch(
            "https://official.example/start", request_policy=_request_policy()
        )
    assert redirect_calls == ["https://official.example/start", "https://official.example/again"]


@pytest.mark.parametrize("location", [None, "   ", "javascript:alert(1)", "ftp://official.example/file"])
def test_request_boundary_rejects_missing_or_invalid_redirect_location(location):
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        headers = {} if location is None else {"Location": location}
        return FetchResponse(url, 302, b"", headers)

    with pytest.raises(RequestBoundaryError):
        Fetcher(transport=transport, respect_robots=False, delay=0).fetch(
            "https://official.example/start", request_policy=_request_policy()
        )
    assert calls == ["https://official.example/start"]


def test_request_policy_requires_exact_host_unless_alias_is_explicit():
    policy = _request_policy("official.example", "cdn.official.example")
    assert policy.validate("https://official.example/feed")
    assert policy.validate("https://cdn.official.example/feed")
    with pytest.raises(RequestBoundaryError):
        policy.validate("https://other.official.example/feed")
    # An article allow pattern is intentionally absent from request policy and
    # therefore cannot authorize a foreign host.
    with pytest.raises(RequestBoundaryError):
        policy.validate("https://evil.example/news/allowed-by-article-regex")


@pytest.mark.parametrize(
    "url",
    [
        "https://official.example:not-a-port/path",
        "https://official.example:70000/path",
        "https://official.example:-1/path",
    ],
)
def test_request_policy_rejects_invalid_ports_before_transport(url):
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"unexpected")

    with pytest.raises(RequestBoundaryError):
        Fetcher(transport=transport, respect_robots=False).fetch(
            url, request_policy=_request_policy()
        )
    assert calls == []


def test_request_boundary_rejects_external_robots_redirect_without_calling_target():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return FetchResponse(url, 302, b"", {"Location": "https://evil.example/robots.txt"})

    with pytest.raises(RequestBoundaryError):
        Fetcher(transport=transport, delay=0).fetch(
            "https://official.example/start", request_policy=_request_policy()
        )
    assert calls == ["https://official.example/robots.txt"]


def test_request_boundary_rejects_whitespace_robots_location_before_content_call():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return FetchResponse(url, 302, b"", {"Location": " \t "})

    with pytest.raises(RequestBoundaryError, match="robots redirect missing Location"):
        Fetcher(transport=transport, delay=0).fetch(
            "https://official.example/start", request_policy=_request_policy()
        )
    assert calls == ["https://official.example/robots.txt"]


def test_urllib_transport_does_not_automatically_follow_redirects():
    paths = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/followed")
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        policy = RequestPolicy("local-source", ("127.0.0.1",))
        with pytest.raises(RequestBoundaryError, match="redirect limit exceeded"):
            Fetcher(respect_robots=False, delay=0, max_redirects=0).fetch(
                f"http://127.0.0.1:{port}/start", request_policy=policy
            )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert paths == ["/start"]
