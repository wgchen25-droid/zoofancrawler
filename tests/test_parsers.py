from zoofan.parsers import parse_archive_page, parse_article_page, parse_feed, parse_sitemap
import http.server
import threading

import pytest

from zoofan.fetcher import (
    FetchResponse, Fetcher, RequestBoundaryError, RequestPolicy, RobotsUnavailable,
)
from zoofan.models import Source, Zoo


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
