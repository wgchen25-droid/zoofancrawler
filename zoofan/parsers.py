"""RSS/Atom, sitemap, archive and article-page parsers.

All parsers return small records instead of persistence objects.  This keeps
parsing deterministic and lets crawlers choose whether/how to store a result.
BeautifulSoup is used for CSS selectors; XML feeds and sitemaps use the
standard library and therefore do not depend on a particular XML backend.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, List, Mapping, Optional, Union
from urllib.parse import urljoin
from xml.etree import ElementTree

from .config import ArchiveParserConfig
from .models import ParsedArticle, ParsedFeedItem, ParsedSitemapEntry
from .normalization import normalize_url


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value)
    value = re.sub(r"\s+", " ", html_lib.unescape(value)).strip()
    return value or None


def parse_datetime(value: Any) -> Optional[Union[datetime, str]]:
    """Parse common RSS/HTML dates, retaining unfamiliar values verbatim."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    candidate = str(value).strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        return candidate


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name.lower()]


def _first_child(element: ElementTree.Element, name: str) -> Optional[ElementTree.Element]:
    return next(iter(_children(element, name)), None)


def _element_text(element: Optional[ElementTree.Element]) -> Optional[str]:
    if element is None:
        return None
    return _text("".join(element.itertext()))


def _element_value(element: Optional[ElementTree.Element]) -> Optional[str]:
    if element is None:
        return None
    return _element_text(element)


def _resolve(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return normalize_url(urljoin(base_url or "", value.strip()))


def _xml_root(payload: Union[str, bytes]) -> ElementTree.Element:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    # A BOM and leading whitespace are accepted by ElementTree, but a few
    # feeds include an XML declaration with an incorrect encoding.  UTF-8 is a
    # safe fallback for web response bytes.
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError:
        return ElementTree.fromstring(payload.decode("utf-8", errors="replace"))


def _feed_link(parent: ElementTree.Element, base_url: Optional[str]) -> Optional[str]:
    links = _children(parent, "link")
    # Canonical/alternate links are stronger than a generic first link.
    for link in links:
        rel = (link.attrib.get("rel") or "").lower()
        if rel in {"canonical", "alternate"} and (link.attrib.get("href") or _element_text(link)):
            return _resolve(link.attrib.get("href") or _element_text(link), base_url)
    if links:
        link = links[0]
        return _resolve(link.attrib.get("href") or _element_text(link), base_url)
    return None


def _feed_author(parent: ElementTree.Element) -> Optional[str]:
    for child in list(parent):
        name = _local_name(child.tag)
        if name in {"author", "creator", "author_name"}:
            value = _element_text(_first_child(child, "name")) or _element_text(child)
            if value:
                return value
    return None


def parse_feed(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedFeedItem]:
    """Parse RSS 2/1 and Atom feeds into a common item shape."""

    root = _xml_root(payload)
    root_name = _local_name(root.tag)
    if root_name == "feed":
        entries = _children(root, "entry")
    elif root_name == "rss":
        channel = _first_child(root, "channel") or root
        entries = _children(channel, "item")
    elif root_name == "rdf":
        entries = [child for child in list(root) if _local_name(child.tag) == "item"]
    else:
        entries = [child for child in root.iter() if _local_name(child.tag) in {"item", "entry"}]

    result: list[ParsedFeedItem] = []
    for entry in entries:
        title = _element_text(_first_child(entry, "title"))
        link = _feed_link(entry, base_url)
        guid = _element_text(_first_child(entry, "guid")) or _element_text(_first_child(entry, "id"))
        if not link and guid and (guid.startswith(("http://", "https://", "/"))):
            link = _resolve(guid, base_url)
        if not link:
            continue
        summary_element = (
            _first_child(entry, "summary")
            or _first_child(entry, "description")
            or _first_child(entry, "content")
        )
        pub = _element_text(_first_child(entry, "published")) or _element_text(
            _first_child(entry, "pubdate")
        )
        updated = _element_text(_first_child(entry, "updated")) or _element_text(
            _first_child(entry, "lastbuilddate")
        )
        canonical = None
        for link_element in _children(entry, "link"):
            if (link_element.attrib.get("rel") or "").lower() == "canonical":
                canonical = _resolve(link_element.attrib.get("href") or _element_text(link_element), base_url)
                break
        result.append(
            ParsedFeedItem(
                url=link,
                canonical_url=canonical,
                title=title,
                published_at=parse_datetime(pub),
                updated_at_source=parse_datetime(updated),
                summary=_element_text(summary_element),
                author=_feed_author(entry),
                guid=guid,
                metadata={"source_format": "atom" if root_name == "feed" else "rss"},
            )
        )
    return result


def parse_rss(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedFeedItem]:
    return parse_feed(payload, base_url)


def parse_atom(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedFeedItem]:
    return parse_feed(payload, base_url)


def parse_sitemap_urlset(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedSitemapEntry]:
    """Parse a sitemap ``<urlset>`` document."""

    root = _xml_root(payload)
    entries: list[ParsedSitemapEntry] = []
    for url_element in [element for element in root.iter() if _local_name(element.tag) == "url"]:
        location = _resolve(_element_text(_first_child(url_element, "loc")), base_url)
        if location:
            entries.append(
                ParsedSitemapEntry(
                    url=location,
                    lastmod=parse_datetime(_element_text(_first_child(url_element, "lastmod"))),
                    metadata={"source_format": "sitemap_urlset"},
                )
            )
    return entries


def parse_sitemap_index(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedSitemapEntry]:
    """Parse a sitemap ``<sitemapindex>`` document."""

    root = _xml_root(payload)
    entries: list[ParsedSitemapEntry] = []
    for sitemap_element in [element for element in root.iter() if _local_name(element.tag) == "sitemap"]:
        location = _resolve(_element_text(_first_child(sitemap_element, "loc")), base_url)
        if location:
            entries.append(
                ParsedSitemapEntry(
                    url=location,
                    lastmod=parse_datetime(_element_text(_first_child(sitemap_element, "lastmod"))),
                    is_index=True,
                    metadata={"source_format": "sitemap_index"},
                )
            )
    return entries


def parse_sitemap(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedSitemapEntry]:
    """Parse either sitemap flavor, selecting by its root element."""

    root = _xml_root(payload)
    if _local_name(root.tag) == "sitemapindex":
        return parse_sitemap_index(payload, base_url)
    return parse_sitemap_urlset(payload, base_url)


def _soup(payload: Union[str, bytes]):
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - requirements installs it
        raise RuntimeError("beautifulsoup4 is required for HTML parsing") from exc
    return BeautifulSoup(payload, "html.parser")


def _node_text(node: Any) -> Optional[str]:
    if node is None:
        return None
    return _text(node.get_text(" ", strip=True))


def _meta(soup: Any, *names: str) -> Optional[str]:
    wanted = {name.lower() for name in names}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in wanted and tag.get("content"):
            return _text(tag.get("content"))
    return None


def _select_one(node: Any, selector: Optional[str]) -> Any:
    if not selector:
        return None
    try:
        return node.select_one(selector)
    except Exception:
        return None


def parse_archive_page(
    payload: Union[str, bytes],
    config: Optional[Union[ArchiveParserConfig, Mapping[str, Any]]] = None,
    base_url: Optional[str] = None,
) -> List[ParsedFeedItem]:
    """Parse one server-rendered archive page using configured CSS selectors."""

    cfg = config if isinstance(config, ArchiveParserConfig) else ArchiveParserConfig.from_mapping(config)
    soup = _soup(payload)
    cards = soup.select(cfg.article_selector) if cfg.article_selector else [soup]
    result: list[ParsedFeedItem] = []
    seen: set[str] = set()
    for card in cards:
        link_node = _select_one(card, cfg.link_selector) or (card if getattr(card, "name", None) == "a" else None)
        if link_node is None:
            continue
        href = link_node.get("href")
        resolved = _resolve(href, base_url)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        title_node = _select_one(card, cfg.title_selector) if cfg.title_selector else None
        title = _node_text(title_node) or _node_text(link_node)
        date_node = _select_one(card, cfg.date_selector) if cfg.date_selector else None
        date_value = (date_node.get("datetime") if date_node is not None else None) or _node_text(date_node)
        result.append(
            ParsedFeedItem(
                url=resolved,
                title=title,
                published_at=parse_datetime(date_value),
                metadata={"source_format": "archive"},
            )
        )
    return result


def parse_archive(
    payload: Union[str, bytes],
    config: Optional[Union[ArchiveParserConfig, Mapping[str, Any]]] = None,
    base_url: Optional[str] = None,
    fetch_page: Optional[Callable[[str], Union[str, bytes]]] = None,
    max_pages: Optional[int] = None,
) -> List[ParsedFeedItem]:
    """Parse an archive and, when supplied, follow a bounded next-page link.

    Pagination is opt-in through ``fetch_page`` and always bounded by
    ``config.max_pages`` (or the explicit ``max_pages`` argument).
    """

    cfg = config if isinstance(config, ArchiveParserConfig) else ArchiveParserConfig.from_mapping(config)
    page_limit = max(1, min(int(max_pages or cfg.max_pages), 100))
    current_payload: Union[str, bytes] = payload
    current_url = base_url
    result: list[ParsedFeedItem] = []
    seen_urls: set[str] = set()
    for page_number in range(page_limit):
        for item in parse_archive_page(current_payload, cfg, current_url):
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                result.append(item)
        if not fetch_page or not cfg.pagination_selector or page_number + 1 >= page_limit:
            break
        soup = _soup(current_payload)
        next_node = _select_one(soup, cfg.pagination_selector)
        next_href = next_node.get("href") if next_node is not None else None
        if not next_href:
            break
        next_url = _resolve(next_href, current_url)
        if not next_url:
            break
        current_url = next_url
        current_payload = fetch_page(next_url)
    return result


def _json_ld_objects(soup: Any) -> Iterable[Mapping[str, Any]]:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            value = json.loads(script.string or script.get_text())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, Mapping) and isinstance(item.get("@graph"), list):
                yield from (node for node in item["@graph"] if isinstance(node, Mapping))
            elif isinstance(item, Mapping):
                yield item


def parse_article_page(payload: Union[str, bytes], url: Optional[str] = None) -> ParsedArticle:
    """Extract canonical URL, metadata, readable content and a raw HTML hash."""

    raw_html = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    soup = _soup(raw_html)
    canonical = None
    for link in soup.find_all("link", rel=True):
        rel = link.get("rel")
        rel_values = [str(value).lower() for value in rel] if isinstance(rel, list) else [str(rel).lower()]
        if "canonical" in rel_values and link.get("href"):
            canonical = _resolve(link.get("href"), url)
            break
    canonical = canonical or _resolve(_meta(soup, "og:url", "twitter:url"), url)
    title = _meta(soup, "og:title", "twitter:title")
    title = title or _node_text(soup.find("h1")) or _node_text(soup.find("title"))
    published_value = _meta(
        soup,
        "article:published_time",
        "datepublished",
        "publish_date",
        "date",
        "pubdate",
    )
    updated_value = _meta(soup, "article:modified_time", "datemodified", "lastmod", "updated")
    author = _meta(soup, "author", "article:author", "byline")
    summary = _meta(soup, "description", "og:description", "twitter:description")
    json_ld: list[Mapping[str, Any]] = list(_json_ld_objects(soup))
    for item in json_ld:
        item_type = item.get("@type")
        if not published_value and str(item_type).lower() in {"article", "newsarticle", "blogposting"}:
            published_value = item.get("datePublished")
        if not updated_value and str(item_type).lower() in {"article", "newsarticle", "blogposting"}:
            updated_value = item.get("dateModified")
        if not author and isinstance(item.get("author"), Mapping):
            author = item["author"].get("name")
        if not summary and item.get("description"):
            summary = item.get("description")
    if not published_value:
        time_node = soup.find("time", datetime=True) or soup.find("time")
        if time_node is not None:
            published_value = time_node.get("datetime") or _node_text(time_node)

    content_node = soup.find("article") or soup.find("main") or soup.body or soup
    # Remove non-content elements before extracting readable text.
    for unwanted in content_node.find_all(["script", "style", "noscript", "template"]):
        unwanted.decompose()
    content = _node_text(content_node)
    metadata: dict[str, Any] = {}
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or meta.get("itemprop")
        value = meta.get("content")
        if key and value:
            metadata[str(key)] = _text(value)
    return ParsedArticle(
        url=_resolve(url, None) if url else None,
        canonical_url=canonical,
        title=title,
        published_at=parse_datetime(published_value),
        updated_at_source=parse_datetime(updated_value),
        author=_text(author),
        summary=_text(summary),
        content=content,
        content_hash=hashlib.sha256(raw_html.encode("utf-8")).hexdigest(),
        raw_html=raw_html,
        metadata=metadata,
    )


def parse_article(payload: Union[str, bytes], url: Optional[str] = None) -> ParsedArticle:
    return parse_article_page(payload, url)
