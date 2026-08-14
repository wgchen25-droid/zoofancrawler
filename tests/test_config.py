from pathlib import Path
from copy import deepcopy
import re
import unicodedata
from urllib.parse import urlsplit

import yaml

import pytest

from zoofan.config import CrawlerConfig, load_config, validate_config
from zoofan.discovery import URLPolicy
from zoofan.models import Source, Zoo
from zoofan.normalization import normalize_url


def _phase0_roster_id(name: str) -> str:
    """Derive the stable Phase 0 ID, retaining reviewed spelling aliases."""

    reviewed_aliases = {
        "Kölner Zoo": "koeln-zoo",
        "Tiergarten Schönbrunn": "tiergarten-schonbrunn",
        "Zoo Zürich": "zoo-zurich",
    }
    if name in reviewed_aliases:
        return reviewed_aliases[name]
    value = name.casefold()
    for source, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        value = value.replace(source, replacement)
    value = value.replace("'", "")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-")


def test_registry_contains_phase0_germany_batch_and_preserves_existing_sources():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    germany_ids = {
        "zoo-berlin",
        "tierpark-berlin",
        "zoo-leipzig",
        "koeln-zoo",
        "zoo-frankfurt",
        "wilhelma",
        "tierpark-hellabrunn",
        "erlebnis-zoo-hannover",
        "tiergarten-nuernberg",
        "tierpark-hagenbeck",
    }
    assert germany_ids | {"gaiazoo"} <= {z.slug for z in config.zoos}

    by_zoo = {z.slug: [s for s in config.sources if s.zoo_id == z.id] for z in config.zoos}
    berlin = {source.id: source for source in by_zoo["zoo-berlin"]}
    assert set(berlin) == {"zoo-berlin-news", "zoo-berlin-press"}
    assert berlin["zoo-berlin-news"].kind == "archive"
    assert berlin["zoo-berlin-news"].url == "https://www.zoo-berlin.de/de/aktuelles/news"
    assert berlin["zoo-berlin-news"].config == {
        "official_host": "www.zoo-berlin.de",
        "article_selector": "article.news-grid-item",
        "link_selector": 'a[href*="/de/aktuelles/news/artikel/"]',
        "title_selector": '[itemprop="headline"]',
        "date_selector": 'time[itemprop="datePublished"]',
        "article_allow": r"^https://www\.zoo-berlin\.de/de/aktuelles/news/artikel/[^/?#]+/?$",
        "pagination_selector": '.page-navigation a[href*="tx_news_pi1"][href*="currentPage"]',
        "max_pages": 2,
    }
    assert berlin["zoo-berlin-press"].config == {"official_host": "www.zoo-berlin.de"}
    koeln = {source.id: source for source in by_zoo["koeln-zoo"]}
    assert set(koeln) == {"koeln-zoo-news-sitemap", "koeln-zoo-news-archive"}
    assert koeln["koeln-zoo-news-sitemap"].kind == "sitemap"
    assert koeln["koeln-zoo-news-sitemap"].url == "https://koelnerzoo.de/wp-sitemap-posts-aktuellartical-1.xml"
    assert koeln["koeln-zoo-news-sitemap"].config == {
        "official_host": "koelnerzoo.de",
        "allow_regex": r"^https://koelnerzoo\.de/aktuellartical/[^/]+/?$",
        "article_date_selector": "main#brx-content p",
        "article_date_pattern": r"^(?:Köln|Koeln|Cologne),?\s+(?P<day>\d{1,2})\.?\s+(?P<month>[A-Za-zÄÖÜäöüß]+)\s+(?P<year>\d{4})(?:\b|$)",
        "exclude_regex": ["monatsubersicht", "jahresubersicht"],
    }
    assert koeln["koeln-zoo-news-archive"].config == {
        "official_host": "koelnerzoo.de",
        "role": "archive",
    }
    gaia = by_zoo["gaiazoo"]
    assert [source.kind for source in gaia][:2] == ["rss", "archive"]
    assert [source.id for source in gaia] == [
        "gaiazoo-news-rss",
        "gaiazoo-news-archive",
        "gaiazoo-sitemap",
        "gaiazoo-press",
    ]
    expected_gaia_allow = r"^https://www\.gaiazoo\.nl/nieuws/[^/?#]+/?$"
    assert gaia[0].config["allow_regex"] == [expected_gaia_allow]
    assert gaia[1].config["allow_regex"] == [expected_gaia_allow]

    expected_rows = {
        "zoo-berlin": ("Zoo Berlin", "DE", "de", "Berlin", "Berlin", "Core"),
        "tierpark-berlin": ("Tierpark Berlin", "DE", "de", None, "Berlin", "Core"),
        "zoo-leipzig": ("Zoo Leipzig", "DE", "de", None, "Leipzig", "Core"),
        "koeln-zoo": ("Kölner Zoo", "DE", "de", "North Rhine-Westphalia", "Cologne", "Core"),
        "zoo-frankfurt": ("Zoo Frankfurt", "DE", "de", None, "Frankfurt", "Core"),
        "wilhelma": ("Wilhelma", "DE", "de", None, "Stuttgart", "Core"),
        "tierpark-hellabrunn": ("Tierpark Hellabrunn", "DE", "de", None, "Munich", "Core"),
        "erlebnis-zoo-hannover": ("Erlebnis-Zoo Hannover", "DE", "de", None, "Hannover", "Core"),
        "tiergarten-nuernberg": ("Tiergarten Nürnberg", "DE", "de", None, "Nuremberg", "Core"),
        "tierpark-hagenbeck": ("Tierpark Hagenbeck", "DE", "de", None, "Hamburg", "Core"),
    }
    for zoo_id, expected in expected_rows.items():
        zoo = next(item for item in config.zoos if item.id == zoo_id)
        name, country, language, region, city, tier = expected
        assert (zoo.name, zoo.country_code, zoo.language, zoo.region, zoo.city) == (
            name,
            country,
            language,
            region,
            city,
        )
        assert zoo.groups == ("core30", "germany_top10")
        assert zoo.metadata["tier"] == tier
        assert zoo.metadata["official_host"]
        assert zoo.list_provenance == [
            {
                "source": "data/zoofan_phase0_zoo_list_v0_1.md",
                "version": "Phase0 v0.1",
                "sha256": "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd",
                "authority": "user_designated_authoritative_membership_list",
                "expanded_count": 73,
                "source_line": {"zoo-berlin": 15, "tierpark-berlin": 16, "zoo-leipzig": 17, "koeln-zoo": 18, "zoo-frankfurt": 19, "wilhelma": 20, "tierpark-hellabrunn": 21, "erlebnis-zoo-hannover": 22, "tiergarten-nuernberg": 23, "tierpark-hagenbeck": 24}[zoo_id],
                "scope": "core30",
                "membership": "germany_top10",
                "checked_at": "2026-08-14",
            }
        ]

    assert next(z for z in config.zoos if z.id == "gaiazoo").groups == ("regional_lab", "dutch_border")
    assert next(z for z in config.zoos if z.id == "gaiazoo").source_status == "success"
    assert {z.source_status for z in config.zoos if z.id in germany_ids} == {
        "configured",
        "success",
        "javascript_required",
        "robots_disallowed",
    }
    expected_blocked = {
        "wilhelma": "javascript_required",
        "erlebnis-zoo-hannover": "javascript_required",
        "tierpark-hagenbeck": "robots_disallowed",
    }
    for zoo_id, status in expected_blocked.items():
        zoo_sources = by_zoo[zoo_id]
        assert zoo_sources and {source.source_status for source in zoo_sources} == {status}
    assert len({z.website_url.rstrip("/") for z in config.zoos if z.website_url}) == 69
    assert validate_config(config_path, strict=True).valid


def test_gaia_and_berlin_allow_rules_accept_only_verified_article_paths():
    config = load_config(Path(__file__).parents[1] / "config" / "zoos.yaml")
    zoos = {zoo.id: zoo for zoo in config.zoos}
    sources = {source.id: source for source in config.sources}

    gaia_cases = (
        ("https://www.gaiazoo.nl/nieuws/nieuwe-bewoner", True),
        ("https://www.gaiazoo.nl/nieuws/nieuwe-bewoner/", True),
        ("https://www.gaiazoo.nl/nieuwe-bewoner", False),
        ("https://www.gaiazoo.nl/nieuws-vlogs/", False),
        ("https://www.gaiazoo.nl/nieuws/", False),
        ("https://www.gaiazoo.nl/nieuws/category/park", False),
        ("https://www.gaiazoo.nl/tickets/", False),
        ("https://www.gaiazoo.nl/park/nieuws/nieuwe-bewoner", False),
        ("https://www.gaiazoo.nl.evil.example/nieuws/nieuwe-bewoner", False),
        ("https://outside.example/nieuws/nieuwe-bewoner", False),
    )
    for source_id in ("gaiazoo-news-rss", "gaiazoo-news-archive"):
        policy = URLPolicy.from_zoo_source(zoos["gaiazoo"], sources[source_id])
        for url, expected in gaia_cases:
            assert policy.accepts(url, source_url=sources[source_id].url) is expected, (
                source_id,
                url,
            )

    berlin_policy = URLPolicy.from_zoo_source(zoos["zoo-berlin"], sources["zoo-berlin-news"])
    berlin_cases = (
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/neuer-eisbaer", True),
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/neuer-eisbaer/", True),
        ("https://www.zoo-berlin.de/de/aktuelles/news", False),
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/", False),
        ("https://www.zoo-berlin.de/de/aktuelles/news/artikel/neuer-eisbaer/mehr", False),
        ("https://www.zoo-berlin.de/de/aktuelles/presse/neuer-eisbaer", False),
        ("https://www.zoo-berlin.de/de/park/news/artikel/neuer-eisbaer", False),
        ("https://www.zoo-berlin.de.evil.example/de/aktuelles/news/artikel/x", False),
        ("https://outside.example/de/aktuelles/news/artikel/x", False),
    )
    for url, expected in berlin_cases:
        assert berlin_policy.accepts(url, source_url=sources["zoo-berlin-news"].url) is expected, url


def test_live_selector_remediations_preserve_source_contracts_and_url_boundaries():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    zoos = {zoo.id: zoo for zoo in config.zoos}
    sources = {source.id: source for source in config.sources}

    expected_contracts = {
        "zoo-frankfurt-press-news": {
            "zoo_id": "zoo-frankfurt",
            "kind": "archive",
            "url": "https://www.zoo-frankfurt.de/de/presse/aktuell",
            "enabled": True,
            "official_host": "www.zoo-frankfurt.de",
            "allow_regex": r"^https://www\.zoo-frankfurt\.de/de/(?:presse|news)/news-detail/[^/?#]+/?$",
            "article_content_selector": "div.article",
        },
        "core-europe-prague-zoo-news-en": {
            "zoo_id": "prague-zoo",
            "kind": "archive",
            "url": "https://www.zoopraha.cz/en/about-zoo/news",
            "enabled": True,
            "official_host": "www.zoopraha.cz",
            "allow_regex": r"^https://www\.zoopraha\.cz/en/about-zoo/news/[^?#]+/?$",
            "article_content_selector": "#maincontent .para",
        },
        "core-europe-prague-zoo-sitemap-cs": {
            "zoo_id": "prague-zoo",
            "kind": "sitemap",
            "url": "https://www.zoopraha.cz/sitemap.xml",
            "enabled": True,
            "official_host": "www.zoopraha.cz",
            "allow_regex": r"^https://www\.zoopraha\.cz/aktualne/[^?#]+/?$",
            "article_content_selector": "#maincontent .para",
        },
        "core-europe-burgers-zoo-news-en": {
            "zoo_id": "burgers-zoo",
            "kind": "archive",
            "url": "https://www.burgerszoo.com/news",
            "enabled": True,
            "source_status": "success",
            "official_host": "www.burgerszoo.com",
            "allow_regex": r"^https://www\.burgerszoo\.com/news/\d{4}/\d{2}/[^?#]+/?$",
            "exclude_regex": [r"^https://www\.burgerszoo\.com/news/?(?:[?#].*)?$"],
            "article_content_selector": "main#main-content > .container-xl > .longread",
        },
        "core-europe-burgers-zoo-sitemap-news-en": {
            "zoo_id": "burgers-zoo",
            "kind": "sitemap",
            "url": "https://www.burgerszoo.com/sitemap.xml",
            "enabled": True,
            "source_status": "success",
            "official_host": "www.burgerszoo.com",
            "allow_regex": r"^https://www\.burgerszoo\.com/news/\d{4}/\d{2}/[^?#]+/?$",
            "exclude_regex": [r"^https://www\.burgerszoo\.com/news/?(?:[?#].*)?$"],
            "article_content_selector": "main#main-content > .container-xl > .longread",
        },
        "core-europe-copenhagen-zoo-news-da": {
            "zoo_id": "copenhagen-zoo",
            "kind": "archive",
            "url": "https://www.zoo.dk/nyheder",
            "enabled": True,
            "official_host": "www.zoo.dk",
            "allow_regex": r"^https://www\.zoo\.dk/nyheder/[^?#]+/?$",
            "article_content_selector": "div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH",
            "article_date_selector": "p.news-article-header_time__ooR7a",
            "article_date_pattern": r"^(?P<day>\d{1,2})\.?\s+(?P<month>januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december)\s+(?P<year>\d{4})$",
        },
        "core-europe-copenhagen-zoo-sitemap-news-da": {
            "zoo_id": "copenhagen-zoo",
            "kind": "sitemap",
            "url": "https://www.zoo.dk/sitemap.xml",
            "enabled": True,
            "official_host": "www.zoo.dk",
            "allow_regex": r"^https://www\.zoo\.dk/nyheder/[^?#]+/?$",
            "article_content_selector": "div.generic-text_component__hr0Ih, p.news-article-header_leadingParagraph__K7SWH",
            "article_date_selector": "p.news-article-header_time__ooR7a",
            "article_date_pattern": r"^(?P<day>\d{1,2})\.?\s+(?P<month>januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december)\s+(?P<year>\d{4})$",
        },
        "bronx-zoo-wcs-rss": {
            "zoo_id": "bronx-zoo",
            "kind": "rss",
            "url": "https://newsroom.wcs.org/DesktopModules/DnnForge%20-%20NewsArticles/Rss.aspx?TabID=13614&ModuleID=28242&MaxCount=25",
            "enabled": True,
            "official_host": "newsroom.wcs.org",
            "allow_regex": r"^https://newsroom\.wcs\.org/News-Releases/articleType/ArticleView/",
            "article_content_selector": ".article-body",
        },
        "brook-valley-zoo-news-rss": {
            "zoo_id": "brook-valley-zoo",
            "kind": "rss",
            "url": "https://brookvalleyzoo.com/feed/",
            "enabled": True,
            "official_host": "brookvalleyzoo.com",
            "allow_regex": [r"^https://brookvalleyzoo\.com/nieuws/[^/?#]+/?$"],
            "article_content_selector": ".elementor-widget-theme-post-content",
        },
        "het-dierenrijck-news-rss": {
            "zoo_id": "het-dierenrijck",
            "kind": "rss",
            "url": "https://dierenrijck.nl/category/nieuwedieren/feed/",
            "enabled": True,
            "official_host": "dierenrijck.nl",
            "allow_regex": [r"^https://dierenrijck\.nl/\d{4}/\d{2}/\d{2}/[^/?#]+/?$"],
            "article_content_selector": "#content .detail-content .bizberg_cocntent_wrapper",
        },
        "naturzoo-rheine-news": {
            "zoo_id": "naturzoo-rheine",
            "kind": "archive",
            "url": "https://www.naturzoo.de/aktuelles/",
            "enabled": True,
            "source_status": "success",
            "official_host": "www.naturzoo.de",
            "allow_regex": [r"^https://www\.naturzoo\.de/aktuelles/artikel/[^/?#]+/?$"],
            "exclude_regex": [r"^https://www\.naturzoo\.de/aktuelles/?$"],
            "article_content_selector": "#main > .content .news-single .news-text-wrap",
        },
        "apenheul-news-rss": {
            "zoo_id": "apenheul",
            "kind": "rss",
            "url": "https://www.apenheul.nl/rss/",
            "enabled": True,
            "source_status": "success",
            "official_host": "www.apenheul.nl",
            "allow_regex": [r"^https://www\.apenheul\.nl/nieuws/\d{4}/\d{2}/[^/?#]+/?$"],
            "exclude_regex": [],
            "article_content_selector": "#main > section.rte.container",
        },
    }
    assert set(expected_contracts) <= set(sources)
    for source_id, expected in expected_contracts.items():
        source = sources[source_id]
        assert source.zoo_id == expected["zoo_id"]
        assert source.kind == expected["kind"]
        assert source.url == expected["url"]
        assert source.enabled is expected["enabled"]
        assert source.config["official_host"] == expected["official_host"]
        assert source.config["allow_regex"] == expected["allow_regex"]
        if "source_status" in expected:
            assert source.source_status == expected["source_status"]
        if "exclude_regex" in expected:
            assert source.config["exclude_regex"] == expected["exclude_regex"]
        assert source.config["article_content_selector"] == expected["article_content_selector"]
        for key in ("article_date_selector", "article_date_pattern"):
            if key in expected:
                assert source.config[key] == expected[key]

    # Every configured content selector must be syntactically loadable through
    # the same validation path used by the CLI and crawler startup.
    assert validate_config(config_path, strict=True).valid

    copenhagen_date_pattern = sources["core-europe-copenhagen-zoo-news-da"].config["article_date_pattern"]
    assert re.fullmatch(copenhagen_date_pattern, "14. august 2026")
    assert re.fullmatch(copenhagen_date_pattern, "14 august 2026")
    assert not re.fullmatch(copenhagen_date_pattern, "14 okt 2026")
    assert not re.fullmatch(copenhagen_date_pattern, "14 augustus 2026")

    frankfurt = sources["zoo-frankfurt-press-news"]
    frankfurt_policy = URLPolicy.from_zoo_source(zoos["zoo-frankfurt"], frankfurt)
    frankfurt_cases = (
        ("https://www.zoo-frankfurt.de/de/presse/news-detail/elefanten-nachwuchs", True),
        ("https://www.zoo-frankfurt.de/de/news/news-detail/elefanten-nachwuchs", True),
        ("https://www.zoo-frankfurt.de/de/presse/aktuell", False),
        ("https://www.zoo-frankfurt.de/de/presse/aktuell?page=2", False),
        ("https://www.zoo-frankfurt.de/de/presse", False),
        ("https://www.zoo-frankfurt.de/de/news", False),
        ("https://www.zoo-frankfurt.de/de/presse/news-detail/elefanten-nachwuchs/extra", False),
        ("https://www.zoo-frankfurt.de/de/presse/news-detail/", False),
        ("https://www.zoo-frankfurt.de/de/presse/press-news-detail/elefanten-nachwuchs", False),
        ("https://www.zoo-frankfurt.de.evil.example/de/news/news-detail/elefanten-nachwuchs", False),
        ("https://outside.example/de/news/news-detail/elefanten-nachwuchs", False),
    )
    for url, expected in frankfurt_cases:
        assert frankfurt_policy.accepts(url, source_url=frankfurt.url) is expected, url

    bronx = sources["bronx-zoo-wcs-rss"]
    assert bronx.config["identity_filter"] == {"required_text_any": ["Bronx Zoo", "Bronx, NY"]}

    dierenrijck = sources["het-dierenrijck-news-rss"]
    assert dierenrijck.config["article_content_exclude_selector"] == [
        ".bizberg_post_date",
        ".blog-title",
        ".bizberg_user_comment_wrapper",
    ]
    assert dierenrijck.config["article_date_selector"] == ".bizberg_post_date"
    dutch_date_pattern = dierenrijck.config["article_date_pattern"]
    assert re.fullmatch(dutch_date_pattern, "3 maart 2025", flags=re.IGNORECASE)
    assert not re.fullmatch(dutch_date_pattern, "3 march 2025", flags=re.IGNORECASE)
    assert not re.fullmatch(dutch_date_pattern, "2025-03-03", flags=re.IGNORECASE)

    dierenrijck_policy = URLPolicy.from_zoo_source(zoos["het-dierenrijck"], dierenrijck)
    dierenrijck_cases = (
        ("https://dierenrijck.nl/2025/03/03/nieuwe-bewoner", True),
        ("https://dierenrijck.nl/2025/03/03/nieuwe-bewoner/", True),
        ("https://dierenrijck.nl/2025/3/3/nieuwe-bewoner", False),
        ("https://dierenrijck.nl/2025/03/03/nieuwe-bewoner/extra", False),
        ("https://dierenrijck.nl/category/nieuwedieren/nieuwe-bewoner", False),
        ("https://dierenrijck.nl/2025/03/03/nieuwe-bewoner?ref=archive", False),
        ("https://dierenrijck.nl.evil.example/2025/03/03/nieuwe-bewoner", False),
    )
    for url, expected in dierenrijck_cases:
        assert dierenrijck_policy.accepts(url, source_url=dierenrijck.url) is expected, url


def test_verified_dutch_retests_promote_only_brook_and_dierenrijck():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    zoos = {zoo.id: zoo for zoo in config.zoos}
    sources = {source.id: source for source in config.sources}
    fragment_path = Path(__file__).parents[1] / "reports" / "source-research" / "regional-dutch-config.yaml"
    fragment = yaml.safe_load(fragment_path.read_text(encoding="utf-8"))
    fragment_zoos = {row["id"]: row for row in fragment["zoos"]}

    expected_retests = {
        "brook-valley-zoo": {
            "source_id": "brook-valley-zoo-news-rss",
            "url": "https://brookvalleyzoo.com/feed/",
            "article_url": "https://brookvalleyzoo.com/nieuws/belangrijke-wijziging-open-dag-verplaatst-naar-25-oktober-2025",
            "discovered_candidate_count": 1,
            "fetched_article_count": 1,
            "parsed_article_count": 1,
            "stored_article_count": 1,
            "article_content_chars": 1531,
        },
        "het-dierenrijck": {
            "source_id": "het-dierenrijck-news-rss",
            "url": "https://dierenrijck.nl/category/nieuwedieren/feed/",
            "article_url": "https://dierenrijck.nl/2024/07/17/kippen-en-siesta-13-juli-2024",
            "discovered_candidate_count": 2,
            "fetched_article_count": 1,
            "parsed_article_count": 1,
            "stored_article_count": 1,
            "article_content_chars": 866,
        },
    }
    for zoo_id, expected in expected_retests.items():
        zoo = zoos[zoo_id]
        source = sources[expected["source_id"]]
        row = fragment_zoos[zoo_id]
        fragment_source = next(item for item in row["sources"] if item["id"] == expected["source_id"])

        assert zoo.source_status == "success"
        assert source.source_status == "success"
        assert source.kind == "rss"
        assert source.url == expected["url"]
        assert source.enabled is True
        assert row["source_status"] == "success"
        assert fragment_source["source_status"] == "success"
        assert fragment_source["enabled"] is True

        research = row["metadata"]["source_research"]
        evidence = research["bounded_live_evidence"]
        assert research["research_source_status"] == "verified_live"
        assert research["adapter_plan_status"] == "ready"
        assert research["recommended"] is True
        assert research["verified_live"] is True
        assert evidence["http_status"] == 200
        assert evidence["article_url"] == expected["article_url"]
        assert evidence["discovered_candidate_count"] == expected["discovered_candidate_count"]
        assert evidence["fetched_article_count"] == expected["fetched_article_count"]
        assert evidence["parsed_article_count"] == expected["parsed_article_count"]
        assert evidence["stored_article_count"] == expected["stored_article_count"]
        assert evidence["article_content_chars"] == expected["article_content_chars"]
        assert evidence["article_parse_status"] == "success"
        assert evidence["remediation_status"] == "verified_live"
        assert evidence["success_claim"] is True
        assert evidence["run_evidence"]["pass1"]["errors"] == []
        assert evidence["run_evidence"]["pass2"]["errors"] == []

    # The two explicitly unsupported/disabled plans remain untouched by the
    # status promotion of the successfully retested sources.
    assert zoos["kasteelpark-born"].source_status == "partial"
    assert sources["kasteelpark-born-news-archive"].source_status == "unsupported"
    assert sources["kasteelpark-born-news-archive"].enabled is False
    assert zoos["mondo-verde"].source_status == "partial"
    assert sources["mondo-verde-news-rss"].source_status == "unsupported"
    assert sources["mondo-verde-news-rss"].enabled is False


def test_registry_contains_phase0_nrw_batch_with_status_safe_sources():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    nrw_ids = {
        "zoo-duisburg",
        "zoo-krefeld",
        "gruener-zoo-wuppertal",
        "zoom-erlebniswelt",
        "zoo-dortmund",
        "allwetterzoo-muenster",
        "aachener-tierpark-euregiozoo",
        "tierpark-fossilium-bochum",
        "aquazoo-loebbecke-museum",
        "naturzoo-rheine",
        "tiergarten-kleve",
        "tiergarten-moenchengladbach",
        "terrazoo-rheinberg",
        "sea-life-oberhausen",
        "tiergehege-kaisergarten",
        "heimat-tierpark-olderdissen",
        "safariland-stukenbrock",
        "affen-und-vogelpark-eckenhagen",
        "wildpark-grafenberg",
        "biotopwildpark-anholter-schweiz",
        "naturwildpark-granat",
        "wildwald-vosswinkel",
        "hochwildpark-rheinland",
        "zoo-im-brueckenkopfpark",
        "adlerwarte-berlebeck",
    }
    assert len(config.zoos) == 73
    assert len(config.sources) == 116
    assert {z.id for z in config.zoos if "nrw" in z.groups} == nrw_ids
    assert all(z.groups == ("regional_lab", "nrw") for z in config.zoos if z.id in nrw_ids)

    expected_tiers = {
        **{zoo_id: "Priority" for zoo_id in {
            "zoo-duisburg",
            "zoo-krefeld",
            "gruener-zoo-wuppertal",
            "zoom-erlebniswelt",
            "zoo-dortmund",
            "allwetterzoo-muenster",
            "aachener-tierpark-euregiozoo",
            "tierpark-fossilium-bochum",
            "aquazoo-loebbecke-museum",
            "naturzoo-rheine",
        }},
        **{zoo_id: "Secondary" for zoo_id in nrw_ids - {
            "zoo-duisburg",
            "zoo-krefeld",
            "gruener-zoo-wuppertal",
            "zoom-erlebniswelt",
            "zoo-dortmund",
            "allwetterzoo-muenster",
            "aachener-tierpark-euregiozoo",
            "tierpark-fossilium-bochum",
            "aquazoo-loebbecke-museum",
            "naturzoo-rheine",
        }},
    }
    expected_statuses = {
        "zoo-duisburg": "robots_disallowed",
        "zoo-krefeld": "robots_disallowed",
        "gruener-zoo-wuppertal": "blocked",
        "zoom-erlebniswelt": "success",
        "zoo-dortmund": "robots_disallowed",
        "allwetterzoo-muenster": "unsupported",
        "aachener-tierpark-euregiozoo": "robots_disallowed",
        "tierpark-fossilium-bochum": "success",
        "aquazoo-loebbecke-museum": "success",
        "naturzoo-rheine": "success",
        "tiergarten-kleve": "unsupported",
        "tiergarten-moenchengladbach": "unsupported",
        "terrazoo-rheinberg": "robots_disallowed",
        "sea-life-oberhausen": "source_not_found",
        "tiergehege-kaisergarten": "source_not_found",
        "heimat-tierpark-olderdissen": "robots_disallowed",
        "safariland-stukenbrock": "robots_disallowed",
        "affen-und-vogelpark-eckenhagen": "source_not_found",
        "wildpark-grafenberg": "source_not_found",
        "biotopwildpark-anholter-schweiz": "source_not_found",
        "naturwildpark-granat": "unsupported",
        "wildwald-vosswinkel": "robots_disallowed",
        "hochwildpark-rheinland": "source_not_found",
        "zoo-im-brueckenkopfpark": "source_not_found",
        "adlerwarte-berlebeck": "success",
    }
    expected_source_lines = {
        zoo_id: line
        for line, zoo_id in enumerate(
            [
                "zoo-duisburg",
                "zoo-krefeld",
                "gruener-zoo-wuppertal",
                "zoom-erlebniswelt",
                "zoo-dortmund",
                "allwetterzoo-muenster",
                "aachener-tierpark-euregiozoo",
                "tierpark-fossilium-bochum",
                "aquazoo-loebbecke-museum",
                "naturzoo-rheine",
                "tiergarten-kleve",
                "tiergarten-moenchengladbach",
                "terrazoo-rheinberg",
                "sea-life-oberhausen",
                "tiergehege-kaisergarten",
                "heimat-tierpark-olderdissen",
                "safariland-stukenbrock",
                "affen-und-vogelpark-eckenhagen",
                "wildpark-grafenberg",
                "biotopwildpark-anholter-schweiz",
                "naturwildpark-granat",
                "wildwald-vosswinkel",
                "hochwildpark-rheinland",
                "zoo-im-brueckenkopfpark",
                "adlerwarte-berlebeck",
            ],
            start=50,
        )
    }
    source_by_zoo = {zoo_id: [s for s in config.sources if s.zoo_id == zoo_id] for zoo_id in nrw_ids}
    assert {
        source.id
        for source in config.sources
        if source.zoo_id in nrw_ids and source.enabled
    } == {
        "zoom-erlebniswelt-press",
        "tierpark-fossilium-bochum-news",
        "aquazoo-loebbecke-museum-news",
        "naturzoo-rheine-news",
        "adlerwarte-berlebeck-news",
    }
    executable_kinds = {"rss", "atom", "sitemap", "archive", "json_api", "custom_adapter"}
    for zoo in (z for z in config.zoos if z.id in nrw_ids):
        assert zoo.region == "NRW"
        assert zoo.country_code == "DE"
        assert zoo.language == "de"
        assert zoo.metadata["tier"] == expected_tiers[zoo.id]
        assert zoo.metadata["official_host"]
        assert zoo.metadata["source_research_status"]
        assert zoo.metadata["source_research_checked_at"] == "2026-08-14"
        assert zoo.metadata["adapter_plan_status"] in {"ready", "unsupported"}
        assert zoo.list_provenance == [
            {
                "source": "data/zoofan_phase0_zoo_list_v0_1.md",
                "version": "Phase0 v0.1",
                "sha256": "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd",
                "authority": "user_designated_authoritative_membership_list",
                "expanded_count": 73,
                "source_line": expected_source_lines[zoo.id],
                "scope": "Regional Lab",
                "membership": "NRW",
                "checked_at": "2026-08-14",
            }
        ]
        assert zoo.source_status == expected_statuses[zoo.id]
        assert source_by_zoo[zoo.id]
        for source in source_by_zoo[zoo.id]:
            assert source.id and source.language == "de" and source.kind
            assert source.config["official_host"] == zoo.metadata["official_host"]
            if source.kind == "unsupported":
                assert source.enabled is False
                assert source.source_status in {"unsupported", "blocked", "robots_disallowed", "source_not_found"}
                assert source.config.get("evidence_urls")
            else:
                assert source.enabled is True
                assert source.kind in executable_kinds

    assert {z.id for z in config.zoos if z.source_status == "source_not_found"} == {
        "sea-life-oberhausen",
        "tiergehege-kaisergarten",
        "affen-und-vogelpark-eckenhagen",
        "wildpark-grafenberg",
        "biotopwildpark-anholter-schweiz",
        "hochwildpark-rheinland",
        "zoo-im-brueckenkopfpark",
    }
    assert len({z.id for z in config.zoos}) == len(config.zoos)
    assert len({s.id for s in config.sources}) == len(config.sources)
    assert len({z.website_url.rstrip("/") for z in config.zoos if z.website_url}) == 69
    assert len({s.url for s in config.sources}) == len(config.sources)
    assert validate_config(config_path, strict=True).valid


def test_registry_contains_phase0_core_europe_batch_with_source_safety():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    europe_ids = {
        "chester-zoo",
        "zsl-london-zoo",
        "prague-zoo",
        "tiergarten-schonbrunn",
        "pairi-daiza",
        "diergaarde-blijdorp",
        "burgers-zoo",
        "copenhagen-zoo",
        "zoo-zurich",
        "zooparc-de-beauval",
    }
    assert len(config.zoos) == 73
    assert len(config.sources) == 116
    assert {z.id for z in config.zoos if "europe_top10" in z.groups} == europe_ids
    assert all(z.groups == ("core30", "europe_top10") for z in config.zoos if z.id in europe_ids)

    expected_rows = {
        "chester-zoo": ("Chester Zoo", "GB", "en", "Chester", "https://www.chesterzoo.org/", "www.chesterzoo.org", "success"),
        "zsl-london-zoo": ("ZSL London Zoo", "GB", "en", "London", "https://www.londonzoo.org/", "www.londonzoo.org", "blocked"),
        "prague-zoo": ("Prague Zoo", "CZ", "en", "Prague", "https://www.zoopraha.cz/en/", "www.zoopraha.cz", "success"),
        "tiergarten-schonbrunn": ("Tiergarten Schönbrunn", "AT", "de", "Vienna", "https://www.zoovienna.at/en/", "www.zoovienna.at", "success"),
        "pairi-daiza": ("Pairi Daiza", "BE", "en", "Brugelette", "https://www.pairidaiza.eu/en/", "www.pairidaiza.eu", "success"),
        "diergaarde-blijdorp": ("Diergaarde Blijdorp", "NL", "en", "Rotterdam", "https://diergaardeblijdorp.nl/en/", "diergaardeblijdorp.nl", "success"),
        "burgers-zoo": ("Burgers' Zoo", "NL", "en", "Arnhem", "https://www.burgerszoo.com/", "www.burgerszoo.com", "success"),
        "copenhagen-zoo": ("Copenhagen Zoo", "DK", "da", "Copenhagen", "https://www.zoo.dk/en", "www.zoo.dk", "success"),
        "zoo-zurich": ("Zoo Zürich", "CH", "de", "Zurich", "https://www.zoo.ch/en", "www.zoo.ch", "success"),
        "zooparc-de-beauval": ("ZooParc de Beauval", "FR", "en", "Saint-Aignan", "https://www.zoobeauval.com/en/", "www.zoobeauval.com", "success"),
    }
    expected_source_lines = {
        "chester-zoo": 25,
        "zsl-london-zoo": 26,
        "prague-zoo": 27,
        "tiergarten-schonbrunn": 28,
        "pairi-daiza": 29,
        "diergaarde-blijdorp": 30,
        "burgers-zoo": 31,
        "copenhagen-zoo": 32,
        "zoo-zurich": 33,
        "zooparc-de-beauval": 34,
    }
    source_by_zoo = {z.id: [s for s in config.sources if s.zoo_id == z.id] for z in config.zoos}
    for zoo_id, expected in expected_rows.items():
        zoo = next(item for item in config.zoos if item.id == zoo_id)
        name, country, language, city, website, official_host, status = expected
        assert (zoo.name, zoo.country_code, zoo.language, zoo.city, zoo.website_url, zoo.source_status) == (
            name,
            country,
            language,
            city,
            website,
            status,
        )
        assert zoo.metadata["official_host"] == official_host
        assert zoo.metadata["tier"] == "Core"
        assert zoo.metadata["tier_status"] == "draft"
        assert zoo.list_provenance == [
            {
                "source": "data/zoofan_phase0_zoo_list_v0_1.md",
                "version": "Phase0 v0.1",
                "sha256": "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd",
                "authority": "user_designated_authoritative_membership_list",
                "expanded_count": 73,
                "source_line": expected_source_lines[zoo_id],
                "scope": "core30",
                "membership": "europe_top10",
                "checked_at": "2026-08-14",
            }
        ]
        assert source_by_zoo[zoo_id]

    expected_source_ids = {
        "chester-zoo": {
            "core-europe-chester-zoo-news-archive",
            "core-europe-chester-zoo-sitemap-reject-noncanonical",
            "core-europe-chester-zoo-press-registry",
        },
        "zsl-london-zoo": {
            "core-europe-zsl-london-zoo-news-blocked",
            "core-europe-zsl-london-zoo-blog-blocked",
            "core-europe-zsl-london-zoo-sitemap-blocked",
            "core-europe-zsl-london-zoo-press-registry",
        },
        "prague-zoo": {
            "core-europe-prague-zoo-news-en",
            "core-europe-prague-zoo-sitemap-cs",
            "core-europe-prague-zoo-press-registry",
        },
        "tiergarten-schonbrunn": {
            "core-europe-tiergarten-schonbrunn-news-de",
            "core-europe-tiergarten-schonbrunn-press-de",
            "core-europe-tiergarten-schonbrunn-news-en-sparse",
        },
        "pairi-daiza": {
            "core-europe-pairi-daiza-news-en",
            "core-europe-pairi-daiza-sitemap-news-en",
        },
        "diergaarde-blijdorp": {
            "core-europe-diergaarde-blijdorp-media-en",
            "core-europe-diergaarde-blijdorp-sitemap-media-en",
            "core-europe-diergaarde-blijdorp-media-nl-fallback",
        },
        "burgers-zoo": {
            "core-europe-burgers-zoo-news-en",
            "core-europe-burgers-zoo-sitemap-news-en",
        },
        "copenhagen-zoo": {
            "core-europe-copenhagen-zoo-news-da",
            "core-europe-copenhagen-zoo-sitemap-news-da",
            "core-europe-copenhagen-zoo-press-registry",
        },
        "zoo-zurich": {
            "core-europe-zoo-zurich-zoonews-de",
            "core-europe-zoo-zurich-media-de",
            "core-europe-zoo-zurich-media-archive-de",
            "core-europe-zoo-zurich-sitemap-zoonews-de",
            "core-europe-zoo-zurich-zoonews-en-sparse",
        },
        "zooparc-de-beauval": {
            "core-europe-zooparc-de-beauval-actus-en",
            "core-europe-zooparc-de-beauval-sitemap-actus-en",
            "core-europe-zooparc-de-beauval-press-registry",
            "core-europe-zooparc-de-beauval-main-sitemap-health",
            "core-europe-zooparc-de-beauval-actus-fr-fallback",
        },
    }
    europe_sources = [source for source in config.sources if source.zoo_id in europe_ids]
    assert {source.id for source in europe_sources} == set().union(*expected_source_ids.values())
    enabled_source_ids = {
        "core-europe-chester-zoo-news-archive",
        "core-europe-chester-zoo-press-registry",
        "core-europe-zsl-london-zoo-press-registry",
        "core-europe-prague-zoo-news-en",
        "core-europe-prague-zoo-sitemap-cs",
        "core-europe-prague-zoo-press-registry",
        "core-europe-tiergarten-schonbrunn-news-de",
        "core-europe-tiergarten-schonbrunn-press-de",
        "core-europe-pairi-daiza-news-en",
        "core-europe-pairi-daiza-sitemap-news-en",
        "core-europe-diergaarde-blijdorp-media-en",
        "core-europe-diergaarde-blijdorp-sitemap-media-en",
        "core-europe-burgers-zoo-news-en",
        "core-europe-burgers-zoo-sitemap-news-en",
        "core-europe-copenhagen-zoo-news-da",
        "core-europe-copenhagen-zoo-sitemap-news-da",
        "core-europe-copenhagen-zoo-press-registry",
        "core-europe-zoo-zurich-zoonews-de",
        "core-europe-zoo-zurich-media-de",
        "core-europe-zoo-zurich-media-archive-de",
        "core-europe-zoo-zurich-sitemap-zoonews-de",
        "core-europe-zooparc-de-beauval-actus-en",
        "core-europe-zooparc-de-beauval-press-registry",
    }
    assert {source.id for source in europe_sources if source.enabled} == enabled_source_ids
    assert len(enabled_source_ids) == 23
    assert len(europe_sources) == 33

    for source in europe_sources:
        owner = next(zoo for zoo in config.zoos if zoo.id == source.zoo_id)
        aliases = set(owner.metadata.get("official_host_aliases", []))
        assert source.config["official_host"] in {owner.metadata["official_host"], *aliases}
        assert source.config["research"]["evidence_urls"]
        if source.enabled:
            assert source.kind in {"archive", "sitemap", "registry_only"}
            if source.kind == "registry_only":
                assert source.config["role"] == "health_check_only"
            else:
                assert source.config.get("allow_regex")
                assert source.config.get("max_pages", source.config.get("max_sitemaps"))
        else:
            assert source.source_status in {"blocked", "partial"}
            assert source.config["research"]["verification"]
            assert source.config["research"]["plan_status"] in {
                "disabled_noncanonical_candidates",
                "blocked_direct_probe",
                "advertised_but_probe_blocked",
                "disabled_sparse_fallback",
                "disabled_language_fallback",
                "health_only_no_news_detail_contract",
                "disabled_no_candidates_under_max_sitemaps_2",
            }

    assert len({z.id for z in config.zoos}) == 73
    assert len({source.id for source in config.sources}) == 116
    assert len({z.website_url.rstrip("/") for z in config.zoos if z.website_url}) == 69
    assert len({source.url for source in config.sources}) == 116
    assert validate_config(config_path, strict=True).valid


def test_registry_contains_phase0_core_global_batch_with_source_safety():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    global_ids = {
        "san-diego-zoo",
        "bronx-zoo",
        "smithsonians-national-zoo",
        "toronto-zoo",
        "singapore-zoo",
        "ueno-zoo",
        "taipei-zoo",
        "taronga-zoo-sydney",
        "sao-paulo-zoo",
        "national-zoological-garden",
    }
    assert len(config.zoos) == 73
    assert len(config.sources) == 116
    assert {z.id for z in config.zoos if "global_top10" in z.groups} == global_ids
    assert all(z.groups == ("core30", "global_top10") for z in config.zoos if z.id in global_ids)

    expected_rows = {
        "san-diego-zoo": ("San Diego Zoo", "US", "en", "San Diego", "https://zoo.sandiegozoo.org/", "zoo.sandiegozoo.org", "success"),
        "bronx-zoo": ("Bronx Zoo", "US", "en", "New York", "https://bronxzoo.com/", "bronxzoo.com", "success"),
        "smithsonians-national-zoo": ("Smithsonian's National Zoo", "US", "en", "Washington, DC", "https://nationalzoo.si.edu/", "nationalzoo.si.edu", "partial"),
        "toronto-zoo": ("Toronto Zoo", "CA", "en", "Toronto", "https://www.torontozoo.com/", "www.torontozoo.com", "robots_disallowed"),
        "singapore-zoo": ("Singapore Zoo", "SG", "en", "Singapore", "https://www.mandai.com/en/singapore-zoo.html", "www.mandai.com", "partial"),
        "ueno-zoo": ("Ueno Zoo", "JP", "ja", "Tokyo", "https://www.tokyo-zoo.net/ueno/index.html", "www.tokyo-zoo.net", "robots_disallowed"),
        "taipei-zoo": ("Taipei Zoo", "TW", "zh", "Taipei", "https://www.zoo.gov.taipei/", "www.zoo.gov.taipei", "robots_disallowed"),
        "taronga-zoo-sydney": ("Taronga Zoo Sydney", "AU", "en", "Sydney", "https://www.taronga.org.au/", "www.taronga.org.au", "partial"),
        "sao-paulo-zoo": ("São Paulo Zoo", "BR", "pt", "São Paulo", "https://www.zoologico.com.br/", "www.zoologico.com.br", "robots_disallowed"),
        "national-zoological-garden": ("National Zoological Garden", "ZA", "en", "Pretoria", "https://www.pretoriazoo.org/", "www.pretoriazoo.org", "partial"),
    }
    expected_source_lines = {
        "san-diego-zoo": 35,
        "bronx-zoo": 36,
        "smithsonians-national-zoo": 37,
        "toronto-zoo": 38,
        "singapore-zoo": 39,
        "ueno-zoo": 40,
        "taipei-zoo": 41,
        "taronga-zoo-sydney": 42,
        "sao-paulo-zoo": 43,
        "national-zoological-garden": 44,
    }
    for zoo_id, expected in expected_rows.items():
        zoo = next(item for item in config.zoos if item.id == zoo_id)
        name, country, language, city, website, official_host, status = expected
        assert (zoo.name, zoo.country_code, zoo.language, zoo.city, zoo.website_url, zoo.source_status) == (
            name,
            country,
            language,
            city,
            website,
            status,
        )
        assert zoo.metadata["official_host"] == official_host
        assert zoo.metadata["tier"] == "Core"
        assert zoo.metadata["phase_bucket"] == "core30"
        assert zoo.metadata["phase_subgroup"] == "global_top10"
        assert zoo.metadata["roster_region_bucket"] == "Global"
        assert zoo.list_provenance == [
            {
                "source": "data/zoofan_phase0_zoo_list_v0_1.md",
                "version": "Phase0 v0.1",
                "sha256": "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd",
                "authority": "user_designated_authoritative_membership_list",
                "expanded_count": 73,
                "source_line": expected_source_lines[zoo_id],
                "scope": "core30",
                "membership": "global_top10",
                "checked_at": "2026-08-14",
            }
        ]

    expected_source_ids = {
        "san-diego-zoo-story-hub",
        "bronx-zoo-wcs-rss",
        "smithsonians-national-zoo-current-news",
        "smithsonians-national-zoo-historical-archive",
        "toronto-zoo-press-2026",
        "singapore-zoo-mandai-media-sitemap",
        "singapore-zoo-mandai-media-index",
        "singapore-zoo-mandai-media-api",
        "ueno-zoo-news-ja",
        "taipei-zoo-press-rss",
        "taipei-zoo-press-json",
        "taipei-zoo-press-xml",
        "taipei-zoo-press-archive",
        "taipei-zoo-sitemap-health",
        "taronga-zoo-sydney-article-api",
        "taronga-zoo-sydney-news-sitemap",
        "sao-paulo-zoo-press-api",
        "sao-paulo-zoo-press-page",
        "national-zoological-garden-rss",
        "national-zoological-garden-wp-api",
    }
    enabled_source_ids = {
        "san-diego-zoo-story-hub",
        "bronx-zoo-wcs-rss",
    }
    global_sources = [source for source in config.sources if source.zoo_id in global_ids]
    assert {source.id for source in global_sources} == expected_source_ids
    assert {source.id for source in global_sources if source.enabled} == enabled_source_ids
    assert len(global_sources) == 20
    assert len(enabled_source_ids) == 2

    disabled_statuses = {"partial", "robots_disallowed", "javascript_required", "unsupported"}
    for source in global_sources:
        assert source.config["official_host"] == urlsplit(source.url).hostname
        research = source.config["research"]
        assert research["evidence_urls"]
        if source.enabled:
            assert source.source_status == "success"
            assert research["source_status"] == "supported"
            assert source.kind in {"archive", "rss", "json_api"}
        else:
            assert source.source_status in disabled_statuses
            assert research["source_status"] in disabled_statuses | {"registry_only"}
            assert research["disable_reason"]

    assert len({z.id for z in config.zoos}) == 73
    assert len({source.id for source in config.sources}) == 116
    assert len({z.website_url.rstrip("/") for z in config.zoos if z.website_url}) == 69
    assert len({source.url for source in config.sources}) == 116
    assert validate_config(config_path, strict=True).valid


def test_final_roster_membership_provenance_and_fragment_reconciliation():
    config_path = Path(__file__).parents[1] / "config" / "zoos.yaml"
    config = load_config(config_path)
    by_id = {zoo.id: zoo for zoo in config.zoos}
    roster_path = Path(__file__).parents[1] / "data" / "zoofan_phase0_zoo_list_v0_1.md"
    country_codes = {
        "Germany": "DE", "United Kingdom": "GB", "Czechia": "CZ", "Austria": "AT",
        "Belgium": "BE", "Netherlands": "NL", "Denmark": "DK", "Switzerland": "CH",
        "France": "FR", "United States": "US", "Canada": "CA", "Singapore": "SG",
        "Japan": "JP", "Taiwan": "TW", "Australia": "AU", "Brazil": "BR",
        "South Africa": "ZA",
    }
    roster = {}
    for line_number, line in enumerate(roster_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 5 and cells[0] not in {"Region", "---"}:
            roster[cells[2]] = {
                "line": line_number,
                "country": country_codes[cells[1]],
                "city": cells[3] or None,
                "tier": cells[4],
    }
    assert len(roster) == len(config.zoos) == 73
    assert set(roster) == {zoo.name for zoo in config.zoos}
    assert all(zoo.enabled for zoo in config.zoos)
    assert {
        zoo.name: zoo.id for zoo in config.zoos
    } == {
        name: _phase0_roster_id(name) for name in roster
    }
    assert len(config.sources) == 116
    assert len({source.id for source in config.sources}) == 116
    expected_provenance_membership = {
        ("core30", "germany_top10"): ("core30", "germany_top10"),
        ("core30", "europe_top10"): ("core30", "europe_top10"),
        ("core30", "global_top10"): ("core30", "global_top10"),
        ("regional_lab", "nrw"): ("Regional Lab", "NRW"),
        ("regional_lab", "dutch_border"): ("Regional Lab", "dutch_border"),
    }
    for zoo in config.zoos:
        row = roster[zoo.name]
        assert (zoo.country_code, zoo.city, zoo.metadata["tier"]) == (
            row["country"], row["city"], row["tier"]
        )
        provenance = zoo.list_provenance[0]
        assert (provenance["scope"], provenance["membership"]) == expected_provenance_membership[
            tuple(zoo.groups)
        ]
        assert provenance["source"] == "data/zoofan_phase0_zoo_list_v0_1.md"
        assert provenance["version"] == "Phase0 v0.1"
        assert provenance["sha256"] == _PHASE0_SHA256
        assert provenance["authority"] == "user_designated_authoritative_membership_list"
        assert provenance["expanded_count"] == 73
        assert provenance["source_line"] == row["line"]
        assert provenance["checked_at"] == "2026-08-14"

    assert {zoo.id for zoo in config.zoos if zoo.website_url is None} == {
        "mini-zoo-venray", "kleindierwandelpark-hedel", "dierenpark-de-bontekoe",
        "mini-animal-park-deventer",
    }
    assert {zoo.id for zoo in config.zoos if zoo.city is None} == {
        "bird-park-de-lorkeershoeve", "ten-kates-dierenpark", "dierenpark-de-bontekoe"
    }
    assert {zoo.id for zoo in config.zoos if zoo.language is None} == {"mini-zoo-venray"}
    websites = [normalize_url(zoo.website_url) for zoo in config.zoos if zoo.website_url]
    assert len(set(websites)) == len(websites) == 69
    source_urls = [normalize_url(source.url) for source in config.sources if source.url]
    assert len(set(source_urls)) == len(source_urls) == 116
    assert {zoo.id for zoo in config.zoos if zoo.source_status == "source_not_found"} == {
        "sea-life-oberhausen", "tiergehege-kaisergarten", "affen-und-vogelpark-eckenhagen",
        "wildpark-grafenberg", "biotopwildpark-anholter-schweiz", "hochwildpark-rheinland",
        "zoo-im-brueckenkopfpark",
    }

    for fragment_path, group in (
        ("reports/source-research/core-europe-config.yaml", "europe_top10"),
        ("reports/source-research/core-global-config.yaml", "global_top10"),
        ("reports/source-research/regional-dutch-config.yaml", "dutch_border"),
    ):
        fragment = yaml.safe_load(Path(fragment_path).read_text(encoding="utf-8"))["zoos"]
        assert {zoo.id for zoo in config.zoos if group in zoo.groups} == {
            row["id"] for row in fragment
        }
        for row in fragment:
            zoo = by_id[row["id"]]
            assert zoo.groups == tuple(row["groups"])
            expected_status = "partial" if row["id"] in {"kasteelpark-born", "mondo-verde"} else row["source_status"]
            assert zoo.source_status == expected_status
            actual = {source.id: source for source in config.sources if source.zoo_id == zoo.id}
            if row["id"] == "gaiazoo":
                assert set(actual) == {
                    "gaiazoo-news-rss", "gaiazoo-news-archive", "gaiazoo-sitemap", "gaiazoo-press"
                }
                continue
            assert set(actual) == {source["id"] for source in row.get("sources", [])}
            for source_row in row.get("sources", []):
                source = actual[source_row["id"]]
                assert source.kind == source_row["kind"]
                assert source.enabled == source_row.get("enabled", True)
                assert source.source_status == source_row.get("source_status")

    assert {zoo.id for zoo in config.zoos if "core30" in zoo.groups}.__len__() == 30
    assert {zoo.id for zoo in config.zoos if "regional_lab" in zoo.groups}.__len__() == 43
    assert all(
        zoo.region == "NRW" for zoo in config.zoos if "nrw" in zoo.groups
    )
    assert next(zoo for zoo in config.zoos if zoo.id == "zoo-berlin").region == "Berlin"
    assert next(zoo for zoo in config.zoos if zoo.id == "koeln-zoo").region == "North Rhine-Westphalia"
    assert next(zoo for zoo in config.zoos if zoo.id == "gaiazoo").region == "Limburg"
    for zoo in config.zoos:
        if zoo.source_status not in {"success", "configured"}:
            continue
        enabled = [source for source in config.sources if source.zoo_id == zoo.id and source.enabled]
        assert enabled, f"{zoo.id} has no enabled source despite status {zoo.source_status}"
        assert any(source.kind != "unsupported" for source in enabled)
    assert validate_config(config_path, strict=True).valid


def _write_config(tmp_path, *, country="DE", zoo_language="de", source_language="de", kind="rss"):
    path = tmp_path / "zoos.yaml"
    values = {
        "country": f"    country_code: {country}\n" if country is not None else "",
        "zoo_language": f"    language: {zoo_language}\n" if zoo_language is not None else "",
        "source_language": f"        language: {source_language}\n" if source_language is not None else "",
        "kind": kind,
    }
    path.write_text(
        "zoos:\n  - id: z\n    slug: z\n    name: Zoo\n"
        + values["country"] + values["zoo_language"]
        + "    source_status: configured\n    groups: []\n"
        + "    website_url: https://example.org\n    sources:\n      - id: s\n"
        + values["source_language"] + f"        kind: {values['kind']}\n        url: https://example.org/feed\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"country": None}, "country_code is required"),
        ({"zoo_language": None}, "language is required"),
        ({"source_language": None}, "language is required"),
        ({"country": "ZZ"}, "country_code is unsupported or malformed"),
        ({"country": "de"}, "country_code is unsupported or malformed"),
        ({"zoo_language": "DE"}, "language is unsupported or malformed"),
        ({"source_language": "xx"}, "language is unsupported or malformed"),
        ({"kind": "browser"}, "kind is unsupported"),
    ],
)
def test_config_rejects_missing_or_invalid_metadata(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        load_config(_write_config(tmp_path, **kwargs))


def test_config_accepts_required_metadata(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert config.zoos[0].country_code == "DE"
    assert config.zoos[0].language == "de"
    assert config.sources[0].language == "de"
    assert config.sources[0].kind == "rss"


def _valid_payload():
    return {
        "zoos": [
            {
                "id": "test-zoo",
                "slug": "test-zoo",
                "name": "Test Zoo",
                "country_code": "DE",
                "language": "de",
                "website_url": "https://example.org/",
                "source_status": "configured",
                "groups": [],
                "sources": [
                    {
                        "id": "test-news",
                        "language": "de",
                        "kind": "rss",
                        "url": "https://example.org/news/feed",
                        "config": {"official_host": "example.org"},
                    }
                ],
            }
        ]
    }


_PHASE0_SHA256 = "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd"
_PHASE0_LINES = {
    "mini-zoo-venray": 82,
    "kleindierwandelpark-hedel": 87,
    "bird-park-de-lorkeershoeve": 89,
    "ten-kates-dierenpark": 90,
    "dierenpark-de-bontekoe": 91,
    "mini-animal-park-deventer": 92,
}


def _phase0_provenance(zoo_id):
    return [
        {
            "source": "data/zoofan_phase0_zoo_list_v0_1.md",
            "version": "Phase0 v0.1",
            "sha256": _PHASE0_SHA256,
            "authority": "user_designated_authoritative_membership_list",
            "expanded_count": 73,
            "source_line": _PHASE0_LINES[zoo_id],
            "checked_at": "2026-08-14",
        }
    ]


def _phase0_gap_payload():
    def row(zoo_id, name, *, city, language="nl", website_url=None, blockers=None, city_blank=False):
        metadata = {
            "phase_bucket": "regional_lab",
            "phase_subgroup": "dutch_border",
            "roster_region_bucket": "Dutch Border",
            "tier": "Directory",
        }
        if website_url is None:
            metadata.update(
                {
                    "no_official_website_verified": True,
                    "no_executable_source": True,
                }
            )
        gate = {"blockers": list(blockers or [])}
        if city_blank:
            gate["roster_city_is_intentionally_blank"] = True
        metadata["import_gate"] = gate
        return {
            "id": zoo_id,
            "slug": zoo_id,
            "name": name,
            "country_code": "NL",
            "language": language,
            "city": city,
            "groups": ["regional_lab", "dutch_border"],
            "source_status": "not_found" if website_url is None else "partial",
            "enabled": True,
            "website_url": website_url,
            "list_provenance": _phase0_provenance(zoo_id),
            "metadata": metadata,
            "sources": [],
        }

    return {
        "zoos": [
            row(
                "mini-zoo-venray",
                "Mini ZOO Venray",
                city="Venray",
                language=None,
                blockers=["missing_official_website_url", "missing_language"],
            ),
            row(
                "kleindierwandelpark-hedel",
                "Kleindierwandelpark Hedel",
                city="Hedel",
                blockers=["missing_official_website_url"],
            ),
            row(
                "dierenpark-de-bontekoe",
                "Dierenpark De Bontekoe",
                city=None,
                blockers=["missing_official_website_url", "city_null_requires_validator_support"],
            ),
            row(
                "mini-animal-park-deventer",
                "Mini animal park Deventer",
                city="Deventer",
                blockers=["missing_official_website_url"],
            ),
            row(
                "bird-park-de-lorkeershoeve",
                "Bird Park De Lorkeershoeve",
                city=None,
                website_url="https://www.vogelparkdelorkeershoeve.nl/",
                blockers=[],
                city_blank=True,
            ),
            row(
                "ten-kates-dierenpark",
                "Ten Kate's Dierenpark",
                city=None,
                website_url="https://www.dierenparktenkate.nl/",
                blockers=[],
                city_blank=True,
            ),
        ]
    }


def test_phase0_null_field_rows_require_exact_local_evidence_and_remain_enabled(tmp_path):
    payload = _phase0_gap_payload()
    result = validate_config(payload, strict=True)
    assert result.valid, result.error_messages

    config = load_config(_write_payload(tmp_path, payload))
    by_id = {zoo.id: zoo for zoo in config.zoos}
    assert set(by_id) == set(_PHASE0_LINES)
    assert all(zoo.enabled for zoo in by_id.values())
    assert all(not zoo.website_url for zoo in by_id.values() if zoo.id in {
        "mini-zoo-venray",
        "kleindierwandelpark-hedel",
        "dierenpark-de-bontekoe",
        "mini-animal-park-deventer",
    })
    assert by_id["mini-zoo-venray"].language is None
    assert by_id["dierenpark-de-bontekoe"].city is None
    assert by_id["bird-park-de-lorkeershoeve"].city is None
    assert by_id["ten-kates-dierenpark"].city is None


@pytest.mark.parametrize(
    ("mutation", "expected_path"),
    [
        (lambda row: row["metadata"].update({"no_official_website_verified": False}), "official_url"),
        (lambda row: row.update({"source_status": "partial"}), "official_url"),
        (lambda row: row["metadata"]["import_gate"]["blockers"].remove("missing_official_website_url"), "official_url"),
        (lambda row: row.update({"enabled": False}), "official_url"),
        (lambda row: row["list_provenance"][0].update({"source": "https://example.org/roster.yaml"}), "list_provenance"),
        (lambda row: row["list_provenance"].append("mixed provenance"), "list_provenance"),
        (lambda row: row["list_provenance"][0].update({"sha256": "0" * 64}), "list_provenance"),
        (lambda row: row["list_provenance"][0].update({"source_line": 999}), "list_provenance"),
        (lambda row: row.update({"website_url": "https://fabricated.example/"}), "official_url"),
        (lambda row: row["sources"].append({"id": "fake", "kind": "rss", "url": "https://example.org/feed"}), "official_url"),
        (lambda row: row.update({"groups": ["regional_lab", "unknown"]}), "groups"),
    ],
)
def test_phase0_no_site_rejects_unsafe_gap_permutations(mutation, expected_path):
    payload = _phase0_gap_payload()
    row = payload["zoos"][0]
    mutation(row)
    result = validate_config(payload, strict=True)
    assert not result.valid
    assert any(expected_path in issue.path for issue in result.errors)


def test_phase0_blank_city_and_unknown_language_require_their_specific_gates():
    payload = _phase0_gap_payload()
    payload["zoos"][4]["metadata"]["import_gate"]["roster_city_is_intentionally_blank"] = False
    payload["zoos"][5]["metadata"]["import_gate"]["roster_city_is_intentionally_blank"] = False
    payload["zoos"][0]["metadata"]["import_gate"]["blockers"].remove("missing_language")
    result = validate_config(payload, strict=True)
    assert not result.valid
    assert any("bird-park-de-lorkeershoeve" in issue.path and issue.path.endswith(".city") for issue in result.errors)
    assert any("ten-kates-dierenpark" in issue.path and issue.path.endswith(".city") for issue in result.errors)
    assert any("mini-zoo-venray" in issue.path and issue.path.endswith(".language") for issue in result.errors)


def _write_payload(tmp_path, payload):
    import yaml

    path = tmp_path / "payload.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def test_validate_config_aggregates_multiple_errors_without_side_effects(tmp_path):
    payload = _valid_payload()
    payload["zoos"][0]["id"] = "bad id"
    payload["zoos"][0]["source_status"] = None
    payload["zoos"][0]["groups"] = {"not": "a list"}
    payload["zoos"][0]["sources"][0]["kind"] = "browser"
    result = validate_config(payload, strict=True)
    assert not result.valid
    assert len(result.errors) >= 4
    assert any(issue.code == "identifier" for issue in result.errors)
    assert any("source_status" in issue.path for issue in result.errors)
    assert any("groups" in issue.path for issue in result.errors)
    assert any("kind is unsupported" in issue.message for issue in result.errors)
    assert not (tmp_path / "data.db").exists()


def test_config_rejects_duplicate_normalized_official_urls(tmp_path):
    payload = _valid_payload()
    second = deepcopy(payload["zoos"][0])
    second["id"] = "second-zoo"
    second["slug"] = "second-zoo"
    second["website_url"] = "https://example.org?utm_source=duplicate"
    second["sources"] = []
    payload["zoos"].append(second)
    result = validate_config(payload)
    assert any(issue.code == "duplicate" and "official_url" in issue.path for issue in result.errors)
    with pytest.raises(ValueError, match="duplicates normalized URL"):
        load_config(_write_payload(tmp_path, payload))


def test_config_rejects_bad_archive_selector_and_regex(tmp_path):
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source["kind"] = "archive"
    source["config"] = {
        "official_host": "example.org",
        "article_selector": "[",
        "link_selector": "a[href]",
        "article_allow": "[",
    }
    result = validate_config(payload)
    assert any(issue.code == "selector" for issue in result.errors)
    assert any(issue.code == "regex" for issue in result.errors)
    with pytest.raises(ValueError, match="invalid CSS selector|invalid regex"):
        load_config(_write_payload(tmp_path, payload))


def test_config_accepts_explicit_host_alias_and_rejects_suffix_spoofing():
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source["url"] = "https://news.example.net/feed"
    source["config"] = {"host_aliases": ["news.example.net"]}
    assert validate_config(payload).valid

    source["config"] = {"official_host": "example.org"}
    result = validate_config(payload)
    assert any(issue.code == "host" for issue in result.errors)


def test_legacy_missing_source_fields_are_loaded_with_stable_defaults(tmp_path):
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source.pop("id")
    source.pop("kind")
    payload["zoos"][0].pop("source_status")
    path = _write_payload(tmp_path, payload)

    first = load_config(path)
    second = load_config(path)
    assert first.sources[0].id == second.sources[0].id
    assert first.sources[0].id.startswith("source-")
    assert first.sources[0].kind == "rss"
    assert first.zoos[0].source_status == "configured"

    no_source = deepcopy(payload["zoos"][0])
    no_source["id"] = "empty-zoo"
    no_source["slug"] = "empty-zoo"
    no_source["sources"] = []
    payload["zoos"] = [no_source]
    empty = load_config(_write_payload(tmp_path, payload))
    assert empty.zoos[0].source_status == "not_found"


def test_validate_config_strict_requires_canonical_fields():
    payload = _valid_payload()
    payload["zoos"][0].pop("source_status")
    payload["zoos"][0]["sources"][0].pop("id")
    payload["zoos"][0]["sources"][0].pop("kind")
    assert validate_config(payload).valid
    result = validate_config(payload, strict=True)
    assert any(issue.code == "required" and "source_status" in issue.path for issue in result.errors)
    assert any(issue.code == "required" and issue.path.endswith(".id") for issue in result.errors)
    assert any(issue.code == "required" and issue.path.endswith(".kind") for issue in result.errors)


def test_mapping_style_zoo_and_source_keys_fill_ids_and_names(tmp_path):
    payload = {
        "zoos": {
            "zoo-key": {
                "website_url": "https://example.org",
                "country_code": "DE",
                "language": "de",
                "sources": {
                    "news-key": {
                        "url": "https://example.org/feed",
                        "language": "de",
                    }
                },
            }
        }
    }
    config = load_config(_write_payload(tmp_path, payload))
    assert config.zoos[0].id == "zoo-key"
    assert config.zoos[0].name == "zoo-key"
    assert config.sources[0].id == "news-key"
    assert config.sources[0].name == "news-key"
    assert config.sources[0].kind == "rss"


@pytest.mark.parametrize(
    "bad_host",
    [" example.org", "example.org ", "https://example.org", "example.org/path", "user@example.org", "example.org:443"],
)
def test_validate_and_load_reject_dirty_or_non_hostname_official_host(tmp_path, bad_host):
    payload = _valid_payload()
    payload["zoos"][0]["sources"][0]["config"] = {"official_host": bad_host}
    result = validate_config(payload)
    assert any(issue.code == "host" for issue in result.errors)
    with pytest.raises(ValueError, match="valid hostname|official host"):
        load_config(_write_payload(tmp_path, payload))


def test_source_host_requires_exact_zoo_host_or_exact_alias():
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source["url"] = "https://news.example.org/feed"
    source["config"] = {}
    result = validate_config(payload)
    assert any(issue.code == "host" for issue in result.errors)
    source["config"] = {"official_host": "news.example.org"}
    assert validate_config(payload).valid


def test_article_date_selector_is_validated_for_sitemap_sources():
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source["kind"] = "sitemap"
    source["config"] = {"article_date_selector": "["}
    result = validate_config(payload)
    assert any(issue.code == "selector" and "article_date_selector" in issue.path for issue in result.errors)


def test_article_content_selectors_are_validated_for_rss_and_allow_exclude_lists():
    payload = _valid_payload()
    source = payload["zoos"][0]["sources"][0]
    source["config"].update(
        {
            "article_content_selector": "section#content.content-wrapper",
            "article_content_exclude_selector": [".post-highlights-block", "style"],
        }
    )
    assert validate_config(payload).valid

    source["config"]["article_content_selector"] = "["
    source["config"]["article_content_exclude_selector"] = [".valid", "["]
    result = validate_config(payload)
    assert any(
        issue.code == "selector" and issue.path.endswith("article_content_selector")
        for issue in result.errors
    )
    assert any(
        issue.code == "selector" and "article_content_exclude_selector[1]" in issue.path
        for issue in result.errors
    )


def test_validate_config_reports_isolated_crawler_source():
    config = CrawlerConfig(
        zoos=[Zoo(id="z", slug="z", website_url="https://example.org", country_code="DE", language="de", source_status="configured")],
        sources=[Source(id="orphan-source", zoo_id="missing-zoo", url="https://example.org/feed", language="de")],
    )
    result = validate_config(config)
    assert any(issue.code == "association" and "missing-zoo" in issue.message for issue in result.errors)


def test_adapter_taxonomy_aliases_are_canonicalized_and_custom_name_is_preserved(tmp_path):
    payload = {
        "zoos": [
            {
                "id": "alias-zoo",
                "slug": "alias-zoo",
                "name": "Alias Zoo",
                "country_code": "DE",
                "language": "de",
                "website_url": "https://official.example/",
                "source_status": "configured",
                "list_provenance": ["fixture"],
                "sources": [
                    {"id": "feed", "kind": "feed", "language": "de", "url": "https://official.example/feed"},
                    {"id": "html", "kind": "generic_html", "language": "de", "url": "https://official.example/news"},
                    {"id": "health", "kind": "health-check", "language": "de", "url": "https://official.example/health"},
                    {
                        "id": "api",
                        "kind": "json",
                        "language": "de",
                        "url": "https://official.example/api",
                        "config": {"items_path": "data.items", "fields": {"url": "links.html"}},
                    },
                    {
                        "id": "custom",
                        "kind": "custom_adapter",
                        "adapter": "fixture_adapter",
                        "language": "de",
                        "url": "https://official.example/custom",
                    },
                    {"id": "unavailable", "kind": "unsupported", "language": "de", "source_status": "unsupported"},
                ],
            }
        ]
    }
    result = validate_config(payload, strict=True)
    assert result.valid, result.error_messages
    config = load_config(_write_payload(tmp_path, payload))
    assert [source.kind for source in config.sources] == [
        "rss", "archive", "registry_only", "json_api", "custom_adapter", "unsupported"
    ]
    assert [source.adapter for source in config.sources] == [
        "rss", "archive", "registry_only", "json_api", "fixture_adapter", "unsupported"
    ]
    assert config.sources[-1].url == ""
    assert config.sources[-1].source_status == "unsupported"


def test_strict_config_rejects_unknown_source_status_and_malformed_json_paths():
    payload = _valid_payload()
    payload["zoos"][0]["list_provenance"] = ["fixture"]
    payload["zoos"][0]["source_status"] = "invented_status"
    source = payload["zoos"][0]["sources"][0]
    source["kind"] = "json_api"
    source["config"] = {"items_path": "data[*]", "fields": {"url": "links..href"}}
    result = validate_config(payload, strict=True)
    assert any(issue.code == "status" for issue in result.errors)
    assert any(issue.code == "path" for issue in result.errors)
