from zoofan.parsers import parse_archive_page, parse_article_page, parse_feed, parse_sitemap
from zoofan.fetcher import FetchResponse, Fetcher, RobotsUnavailable


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
