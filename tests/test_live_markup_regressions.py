"""Compact regression fixtures for the six live article-page markup shapes.

The fixtures intentionally preserve only selector-relevant server-rendered
markup.  They are not snapshots of full publisher pages: their purpose is to
keep the configured content roots, exclusion boundaries, metadata precedence,
and raw-versus-clean content contract deterministic.
"""

from __future__ import annotations

from pathlib import Path

from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.parsers import parse_archive_page, parse_article_page
from zoofan.reporting import _source_navigation_match
from zoofan.storage import SQLiteStorage


FIXTURES = Path(__file__).parent / "fixtures" / "live_markup"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


FRANKFURT_CONFIG = {
    "article_content_selector": "div.article",
}

PRAGUE_CONFIG = {
    "article_content_selector": "#maincontent .para",
}

COPENHAGEN_CONFIG = {
    # The suffix is part of the observed SSR class contract; do not broaden
    # this to an arbitrary page-wide div/class heuristic.
    "article_content_selector": "div.generic-text_component__hr0Ih",
}

COPENHAGEN_LEGACY_CONFIG = {
    # Current SSR markup and the older article shell are both observed.  The
    # fixture deliberately puts an empty modern match first to exercise the
    # fallback contract rather than relying on document-order luck.
    "article_content_selector": (
        "div.generic-text_component__hr0Ih, "
        "p.news-article-header_leadingParagraph__K7SWH"
    ),
    "article_content_exclude_selector": [
        "div.latest-news_component___3vnv",
        "footer",
    ],
    "article_date_selector": "p.news-article-header_time__ooR7a",
    "article_date_pattern": (
        r"^(?P<day>\d{1,2})\.?\s+"
        r"(?P<month>januar|februar|marts|april|maj|juni|juli|august|"
        r"september|oktober|november|december)\s+(?P<year>\d{4})$"
    ),
}

BRONX_CONFIG = {
    "article_content_selector": ".article-body",
}

BROOK_CONFIG = {
    "article_content_selector": ".elementor-widget-theme-post-content",
}

DIERENRIJCK_CONFIG = {
    # ``cocntent`` is the publisher's observed typo and is intentionally
    # explicit rather than replaced with a broad ``.content`` selector.
    "article_content_selector": "#content .detail-content .bizberg_cocntent_wrapper",
    "article_content_exclude_selector": [
        ".bizberg_post_date",
        ".blog-title",
        ".bizberg_user_comment_wrapper",
    ],
    "article_date_selector": ".bizberg_post_date",
    "article_date_pattern": (
        r"^(?P<day>\d{1,2})\s+"
        r"(?P<month>januari|februari|maart|april|mei|juni|juli|"
        r"augustus|september|oktober|november|december)\s+"
        r"(?P<year>\d{4})$"
    ),
}

BOCHUM_CONFIG = {
    # The article page places the site visit shortcuts before the article in
    # the same ``main`` element.  Keep the direct article column as the
    # source-scoped root instead of falling back to page-wide ``main`` text.
    "article_content_selector": "main .ais-news-details > .col-md-12",
    "article_date_selector": "span.detail-date time",
    "article_date_pattern": (
        r"^(?P<day>\d{1,2})\.\s+"
        r"(?P<month>jan(?:uar)?\.?|feb(?:ruar)?\.?|märz\.?|maerz\.?|"
        r"apr(?:il)?\.?|mai|juni?\.?|juli?\.?|aug(?:ust)?\.?|"
        r"sep(?:tember)?\.?|okt(?:ober)?\.?|nov(?:ember)?\.?|"
        r"dez(?:ember)?\.?)\s+(?P<year>\d{4})$"
    ),
}

APENHEUL_CONFIG = {
    "article_content_selector": "#main > section.rte.container",
}

NATURZOO_CONFIG = {
    "article_content_selector": "#main > .content .news-single .news-text-wrap",
}

NATURZOO_ARCHIVE_CONFIG = {
    "article_selector": "div.row.news-list-item",
    "link_selector": 'a[href*="/aktuelles/artikel/"]',
    "title_selector": 'h3[itemprop="headline"]',
    "date_selector": 'time[itemprop="datePublished"]',
}

BURGERS_CONFIG = {
    "article_content_selector": "main#main-content > .container-xl > .longread",
    "article_date_selector": "p.page-subtitle",
    "article_date_pattern": (
        r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"(?P<day>\d{1,2})\s+(?P<month>January|February|March|April|May|June|July|"
        r"August|September|October|November|December)\s+(?P<year>\d{4})$"
    ),
}


def test_frankfurt_explicit_root_h1_wins_over_page_heading_and_keeps_date_and_relative_canonical():
    article = parse_article_page(
        _fixture("frankfurt_article_v1.html"),
        "https://www.zoo-frankfurt.de/de/presse/news-detail/source",
        FRANKFURT_CONFIG,
    )

    assert article.title == "Abschied von den Turen im Zoo Frankfurt"
    assert article.title != "Presseservice und Pressemeldungen"
    assert article.canonical_url == "https://www.zoo-frankfurt.de/de/presse/news-detail/abschied-von-den-turen"
    assert article.published_at is not None
    assert article.published_at.date().isoformat() == "2026-07-14"
    assert article.published_at_raw == "14.07.2026"
    assert article.content and "seltenen Westkaukasischen Steinböcke" in article.content
    assert "Presseservice und Pressemeldungen" not in article.content
    assert article.parse_status == "success"


def test_prague_para_root_excludes_social_and_navigation_noise():
    article = parse_article_page(
        _fixture("prague_article_v1.html"),
        "https://www.zoopraha.cz/en/about-zoo/news/16312-ground-hornbill",
        PRAGUE_CONFIG,
    )

    assert article.title == "Southern Ground-hornbill Chick: Exceptionally on View to Visitors This Year"
    assert article.canonical_url == "https://www.zoopraha.cz/en/about-zoo/news/16312-ground-hornbill"
    assert article.content and "ground-hornbill chick" in article.content
    assert "Facebook" not in article.content
    assert "Back to news" not in article.content
    assert "socialnav" not in (article.content_html or "")
    assert "<nav" not in (article.content_html or "")


def test_copenhagen_hashed_generic_text_excludes_latest_news_and_footer_and_keeps_safe_canonical():
    article = parse_article_page(
        _fixture("copenhagen_article_v1.html"),
        "https://www.zoo.dk/nyheder/pandaunge?utm_source=feed",
        COPENHAGEN_CONFIG,
    )

    assert article.title == "Er der en pandaunge på vej?"
    assert article.canonical_url == "https://www.zoo.dk/nyheder/pandaunge"
    assert article.content and "ekstra spænding" in article.content
    assert "LatestNews card contamination" not in article.content
    assert "Footer navigation contamination" not in article.content
    assert "latest-news" not in (article.content_html or "").lower()
    assert "<footer" not in (article.content_html or "").lower()
    # JSON/RSC transport evidence remains raw response evidence only.
    assert "__NEXT_DATA__" in (article.raw_html or "")
    assert "__NEXT_DATA__" not in (article.content_html or "")


def test_copenhagen_legacy_union_falls_through_empty_generic_text_and_parses_danish_date():
    article = parse_article_page(
        _fixture("copenhagen_legacy_article_v1.html"),
        "https://www.zoo.dk/nyheder/gammel-panda-nyhed?utm_source=feed",
        COPENHAGEN_LEGACY_CONFIG,
    )

    assert article.title == "Gammel nyhed om pandaer"
    assert article.canonical_url == "https://www.zoo.dk/nyheder/gammel-panda-nyhed"
    assert article.content and "Pandaerne trives" in article.content
    assert article.published_at == "2023-01-25"
    assert article.published_at_raw == "25. januar 2023"
    assert article.parse_status == "success"
    assert "LatestNews legacy contamination" not in article.content
    assert "Footer legacy contamination" not in article.content
    assert "latest-news" not in (article.content_html or "").lower()
    assert "<footer" not in (article.content_html or "").lower()


def test_bronx_article_body_excludes_cards_and_preserves_metadata_title_and_canonical():
    article = parse_article_page(
        _fixture("bronx_article_v1.html"),
        "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/26530/bronx-polar-bears.aspx",
        BRONX_CONFIG,
    )

    assert article.title == "Bronx Zoo polar bear study"
    assert article.canonical_url == "https://newsroom.wcs.org/News-Releases/articleType/ArticleView/articleId/26530/bronx-polar-bears.aspx"
    assert article.content and "polar bear study" in article.content
    assert "Unrelated latest-news card" not in article.content
    assert "Media contacts and resource cards" not in article.content
    assert article.content_html and "article-body" not in article.content_html


def test_brook_elementor_body_uses_meta_date_and_canonical():
    article = parse_article_page(
        _fixture("brook_article_v1.html"),
        "https://brookvalleyzoo.com/nieuws/open-dag-verplaatst/",
        BROOK_CONFIG,
    )

    assert article.title == "Belangrijke wijziging: Open dag verplaatst naar 25 oktober 2025 - Brook Valley Zoo"
    assert article.canonical_url == "https://brookvalleyzoo.com/nieuws/open-dag-verplaatst"
    assert article.published_at is not None
    assert article.published_at.isoformat() == "2025-09-22T10:37:46+00:00"
    assert article.published_at_raw == "2025-09-22T10:37:46+00:00"
    assert article.content and "open dag is verplaatst" in article.content
    assert "Home" not in article.content


def test_dierenrijck_typo_wrapper_excludes_related_and_comments_and_parses_visible_dutch_date():
    article = parse_article_page(
        _fixture("dierenrijck_article_v1.html"),
        "https://dierenrijck.nl/2024/07/17/kippen-en-siesta/",
        DIERENRIJCK_CONFIG,
    )

    assert article.title == "Kippen en siësta 13 juli 2024"
    assert article.canonical_url == "https://dierenrijck.nl/2024/07/17/kippen-en-siesta"
    # Configured article datelines intentionally return the normalized
    # date-only evidence string (the storage boundary persists the same ISO
    # value), unlike a datetime-valued <time> candidate.
    assert article.published_at == "2024-07-17"
    assert article.published_at_raw == "17 juli 2024"
    assert article.content and "jong vorwerk kipje" in article.content
    assert "Andere berichten" not in article.content
    assert "Reacties en commentaren" not in article.content
    assert "related-posts" not in (article.content_html or "")


def test_bochum_article_root_excludes_main_navigation_and_parses_strict_visible_date():
    article = parse_article_page(
        _fixture("bochum_article_v1.html"),
        "https://www.tierpark-bochum.de/neuigkeiten/aktuelles/detail/2026-08-bk-2026",
        BOCHUM_CONFIG,
    )

    assert article.title == "Lernen, staunen, entdecken"
    assert article.canonical_url == (
        "https://www.tierpark-bochum.de/neuigkeiten/aktuelles/detail/2026-08-bk-2026"
    )
    assert article.published_at is not None
    assert article.published_at.isoformat() == "2026-08-11T00:00:00+00:00"
    assert article.published_at_raw == "2026-08-11T00:00:00"
    assert article.content and article.content.startswith(
        "Lernen, staunen, entdecken 11. Aug. 2026 "
        "Bildungskooperation bringt Erstklässler in den Tierpark"
    )
    assert "Öffnungszeiten" not in article.content
    assert "Eintrittskarten" not in article.content
    assert "Zurück zur Übersicht" not in article.content
    source = Source(
        id="tierpark-fossilium-bochum-news",
        zoo_id="tierpark-fossilium-bochum",
        kind="archive",
        url="https://www.tierpark-bochum.de/neuigkeiten/aktuelles",
        config={"official_host": "www.tierpark-bochum.de"},
    )
    assert not _source_navigation_match(
        source,
        article.canonical_url or article.url or "",
        article.title or "",
        article.content or "",
    )
    assert article.metadata.get("parse_status") == "success"


def test_apenheul_rte_root_excludes_visit_and_navigation_boilerplate():
    article = parse_article_page(
        _fixture("apenheul_article_v1.html"),
        "https://www.apenheul.nl/nieuws/2026/07/zes-doodshoofdaapjes-geboren",
        APENHEUL_CONFIG,
    )

    assert article.title == "Zes doodshoofdaapjes geboren"
    assert article.canonical_url == "https://www.apenheul.nl/nieuws/2026/07/zes-doodshoofdaapjes-geboren"
    assert article.published_at is None
    assert article.content and "zes jongen" in article.content
    for noise in (
        "openingstijden",
        "tickets",
        "gerelateerde nieuws",
        "contact",
        "directions",
        "download",
        "back",
    ):
        assert noise not in article.content.casefold()
    assert article.parse_status == "partial"


def test_naturzoo_news_text_root_keeps_article_time_and_excludes_site_chrome():
    article = parse_article_page(
        _fixture("naturzoo_article_v1.html"),
        "https://www.naturzoo.de/aktuelles/artikel/bunt-giftig-und-neu",
        NATURZOO_CONFIG,
    )

    assert article.title == "Bunt, giftig und neu"
    assert article.canonical_url == "https://www.naturzoo.de/aktuelles/artikel/bunt-giftig-und-neu"
    assert article.published_at is not None
    assert article.published_at.isoformat() == "2026-05-29T10:55:00+02:00"
    assert article.published_at_raw == "2026-05-29T10:55:00+02:00"
    assert article.content and "zwei neue Tierarten" in article.content
    for noise in (
        "öffnungszeiten",
        "tickets",
        "verwandte nachrichten",
        "kontakt",
        "anfahrt",
        "directions",
        "download",
        "zurück",
    ):
        assert noise not in article.content.casefold()


def test_naturzoo_archive_time_selector_preserves_candidate_date():
    archive = """
    <div class="row news-list-item">
      <h3 itemprop="headline"><a href="/aktuelles/artikel/bunt-giftig-und-neu">Bunt, giftig und neu</a></h3>
      <time itemprop="datePublished" datetime="2026-05-29">29.05.2026</time>
    </div>
    """
    items = parse_archive_page(
        archive,
        NATURZOO_ARCHIVE_CONFIG,
        "https://www.naturzoo.de/aktuelles/",
    )

    assert len(items) == 1
    assert items[0].url == "https://www.naturzoo.de/aktuelles/artikel/bunt-giftig-und-neu"
    assert items[0].title == "Bunt, giftig und neu"
    assert items[0].published_at is not None
    assert items[0].published_at.date().isoformat() == "2026-05-29"
    assert items[0].metadata["published_at_raw"] == "2026-05-29"


def test_burgers_longread_root_keeps_full_prose_and_excludes_page_chrome():
    article = parse_article_page(
        _fixture("burgers_article_v1.html"),
        "https://www.burgerszoo.com/news/2026/08/burgers-zoo-developing-worlds-largest-seagrass-aquarium",
        BURGERS_CONFIG,
    )

    assert article.title == "Burgers' Zoo developing world's largest seagrass aquarium"
    assert article.canonical_url == (
        "https://www.burgerszoo.com/news/2026/08/"
        "burgers-zoo-developing-worlds-largest-seagrass-aquarium"
    )
    assert article.published_at == "2026-08-06"
    assert article.published_at_raw == "Thursday, 6 August 2026"
    assert article.content and "world’s largest seagrass aquarium" in article.content
    assert "A crucial ocean ecosystem" in article.content
    assert "The biggest challenges" in article.content
    assert "Global research into seagrass restoration" in article.content
    for noise in (
        "homepage",
        "go back",
        "share this article",
        "related news contamination",
        "newsletter modal contamination",
    ):
        assert noise not in article.content.casefold()
    assert "share-article" not in (article.content_html or "")
    assert "news-overview-container" not in (article.content_html or "")
    assert 'id="usermodal"' not in (article.content_html or "").casefold()


def test_invalid_configured_selector_fails_content_parse_without_generic_fallback():
    article = parse_article_page(
        _fixture("frankfurt_article_v1.html"),
        "https://www.zoo-frankfurt.de/de/presse/news-detail/source",
        {"article_content_selector": "["},
    )

    assert article.content is None
    assert article.content_html is None
    assert article.content_hash is None
    assert article.parse_status == "content_parse_error"
    assert article.metadata["error_classification"] == "content_parse_error"
    assert "configured article content selector did not match" in article.metadata["error_details"][0]
    # A bad explicit root cannot cause a page-wide article/card fallback, but
    # title metadata/page evidence remains available independently.
    assert article.title == "Presseservice und Pressemeldungen"


def test_clean_content_is_separate_from_raw_html_evidence():
    article = parse_article_page(
        _fixture("copenhagen_article_v1.html"),
        "https://www.zoo.dk/nyheder/pandaunge",
        COPENHAGEN_CONFIG,
    )

    assert article.raw_html and article.content_html
    assert article.raw_html != article.content_html
    assert "LatestNews card contamination" in article.raw_html
    assert "LatestNews card contamination" not in article.content_html


def test_crawler_persists_apenheul_rss_date_fallback_and_naturzoo_archive_time():
    host_apenheul = "www.apenheul.nl"
    apenheul_feed_source_url = f"https://{host_apenheul}/rss/"
    apenheul_feed_url = f"https://{host_apenheul}/rss"
    apenheul_article_url = (
        f"https://{host_apenheul}/nieuws/2026/07/zes-doodshoofdaapjes-geboren"
    )
    host_naturzoo = "www.naturzoo.de"
    naturzoo_archive_source_url = f"https://{host_naturzoo}/aktuelles/"
    naturzoo_archive_url = f"https://{host_naturzoo}/aktuelles"
    naturzoo_article_url = f"https://{host_naturzoo}/aktuelles/artikel/bunt-giftig-und-neu"

    zoo = Zoo(
        id="regional-live-markup-zoo",
        slug="regional-live-markup-zoo",
        name="Regional Live Markup Zoo",
        website_url=f"https://{host_apenheul}/",
    )
    apenheul_source = Source(
        id="regional-apenheul-rss",
        zoo_id=zoo.id,
        kind="rss",
        url=apenheul_feed_source_url,
        config={
            "official_host": host_apenheul,
            "allow_regex": rf"^https://{host_apenheul}/nieuws/\d{{4}}/\d{{2}}/[^/?#]+/?$",
            **APENHEUL_CONFIG,
        },
    )
    naturzoo_source = Source(
        id="regional-naturzoo-archive",
        zoo_id=zoo.id,
        kind="archive",
        url=naturzoo_archive_source_url,
        config={
            "official_host": host_naturzoo,
            "allow_regex": rf"^https://{host_naturzoo}/aktuelles/artikel/[^/?#]+/?$",
            **NATURZOO_ARCHIVE_CONFIG,
            **NATURZOO_CONFIG,
        },
    )
    rss = f"""
    <rss version="2.0"><channel><item>
      <title>Zes doodshoofdaapjes geboren</title>
      <link>{apenheul_article_url}</link>
      <pubDate>Tue, 28 Jul 2026 13:19:09 Z</pubDate>
    </item></channel></rss>
    """
    archive = """
    <div class="row news-list-item">
      <h3 itemprop="headline"><a href="/aktuelles/artikel/bunt-giftig-und-neu">Bunt, giftig und neu</a></h3>
      <time itemprop="datePublished" datetime="2026-05-29">29.05.2026</time>
    </div>
    """
    fetcher = _FixtureFetcher(
        {
            apenheul_feed_url: rss,
            apenheul_article_url: _fixture("apenheul_article_v1.html"),
            naturzoo_archive_url: archive,
            naturzoo_article_url: _fixture("naturzoo_article_v1.html"),
        }
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(
            zoos=[zoo],
            sources=[apenheul_source, naturzoo_source],
            request_delay=0,
        ),
        storage=storage,
        fetcher=fetcher,
    ).crawl("all")

    assert result.status == "completed"
    assert result.stored_count == 2
    assert {
        apenheul_feed_url,
        apenheul_article_url,
        naturzoo_archive_url,
        naturzoo_article_url,
    } <= set(fetcher.calls)
    articles = {article.canonical_url: article for article in storage.list_articles()}
    apenheul = articles[apenheul_article_url]
    assert apenheul.content and "zes jongen" in apenheul.content
    assert apenheul.published_at is not None
    assert apenheul.published_at.isoformat() == "2026-07-28T13:19:09+00:00"
    assert apenheul.published_at_raw == "Tue, 28 Jul 2026 13:19:09 Z"
    naturzoo = articles[naturzoo_article_url]
    assert naturzoo.content and "zwei neue Tierarten" in naturzoo.content
    assert naturzoo.published_at is not None
    assert naturzoo.published_at.isoformat() == "2026-05-29T10:55:00+02:00"


class _FixtureFetcher:
    """Small request-policy-aware transport for the Crawler integration check."""

    supports_request_policy = True

    def __init__(self, payloads: dict[str, bytes | str]):
        self.payloads = payloads
        self.calls: list[str] = []

    def fetch(self, url: str, *, request_policy):
        assert request_policy.validate(url) == url
        self.calls.append(url)
        payload = self.payloads[url]
        body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        return FetchResponse(url, 200, body)


def test_crawler_persists_configured_root_for_rss_and_archive_candidates():
    """Exercise the parser config at the Crawler/storage boundary once."""

    host = "www.zoo-frankfurt.de"
    rss_url = f"https://{host}/feed.xml"
    archive_url = f"https://{host}/de/presse/aktuell"
    rss_article_url = f"https://{host}/de/presse/news-detail/rss-article"
    archive_article_url = f"https://{host}/de/presse/news-detail/archive-article"
    allow = rf"^https://{host}/de/presse/news-detail/[^/?#]+/?$"

    rss_source = Source(
        id="live-markup-rss",
        zoo_id="live-markup-zoo",
        kind="rss",
        url=rss_url,
        config={
            "official_host": host,
            "allow_regex": allow,
            "article_content_selector": "div.article",
        },
    )
    archive_source = Source(
        id="live-markup-archive",
        zoo_id="live-markup-zoo",
        kind="archive",
        url=archive_url,
        config={
            "official_host": host,
            "allow_regex": allow,
            "article_selector": "article.card",
            "link_selector": "a[href]",
            "title_selector": "h2",
            "article_content_selector": "div.article",
        },
    )
    zoo = Zoo(
        id="live-markup-zoo",
        slug="live-markup-zoo",
        name="Live Markup Zoo",
        website_url=f"https://{host}/",
    )

    rss_page = f"<rss><channel><item><title>RSS candidate</title><link>{rss_article_url}</link></item></channel></rss>"
    archive_page = '<article class="card"><a href="/de/presse/news-detail/archive-article"><h2>Archive candidate</h2></a></article>'
    base_article = _fixture("frankfurt_article_v1.html")
    rss_article = base_article.replace(
        b"/de/presse/news-detail/abschied-von-den-turen",
        b"/de/presse/news-detail/rss-article",
    )
    archive_article = base_article.replace(
        b"/de/presse/news-detail/abschied-von-den-turen",
        b"/de/presse/news-detail/archive-article",
    ).replace(
        "seltenen Westkaukasischen Steinböcke".encode("utf-8"),
        b"neuen Schuetzenfisch",
    ).replace(
        b"Abschied von den Turen im Zoo Frankfurt",
        b"Archive-only Nachricht im Zoo Frankfurt",
    )
    fetcher = _FixtureFetcher(
        {
            rss_url: rss_page,
            archive_url: archive_page,
            rss_article_url: rss_article,
            archive_article_url: archive_article,
        }
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(
        CrawlerConfig(
            zoos=[zoo],
            sources=[rss_source, archive_source],
            request_delay=0,
        ),
        storage=storage,
        fetcher=fetcher,
    ).crawl("all")

    assert result.status == "completed"
    assert result.stored_count == 2
    assert {rss_url, archive_url, rss_article_url, archive_article_url} <= set(fetcher.calls)
    articles = storage.list_articles()
    assert {article.canonical_url for article in articles} == {rss_article_url, archive_article_url}
    assert all(article.content for article in articles)
    assert any("seltenen Westkaukasischen Steinböcke" in article.content for article in articles)
    assert any("neuen Schuetzenfisch" in article.content for article in articles)
    assert all(article.parse_status == "success" for article in articles)
