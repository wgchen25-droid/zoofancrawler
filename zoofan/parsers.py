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
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterable, List, Mapping, Optional, Union
from urllib.parse import urljoin, urlsplit
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


def _date_raw(value: Any) -> Optional[str]:
    """Return the source date text in a stable, display-friendly form.

    The parser deliberately keeps this separate from :func:`parse_datetime`:
    an unknown date must not leak arbitrary text into ``published_at`` merely
    because retaining it is useful for later review.
    """

    if value is None or isinstance(value, datetime):
        return None
    candidate = html_lib.unescape(str(value)).strip()
    return candidate or None


# A number of publishers use a date-shaped field for a deliberate "not yet
# available" marker.  It is evidence that the optional date is missing, not a
# malformed date and therefore must not turn an otherwise usable article into
# a failed crawl.
_MISSING_DATE_MARKERS = re.compile(
    r"""(?ix)^(?:
        n/?a|na|none|null|unknown|undated|no[ -]?date|date[ -]?to[ -]?be[ -]?announced|
        not[ -]?available|not[ -]?known|not[ -]?set|tba|tbd|pending|soon|
        noch[ \-_]?nicht(?:[ \-_]?veröffentlicht|[ \-_]?bekannt)?|
        nog[ \-_]?niet(?:[ \-_]?bekend|[ \-_]?gepubliceerd)?|
        keine[ \-_]?angabe|kein[ \-_]?datum|sin[ \-_]?fecha|sans[ \-_]?date
    )$"""
)


def _is_missing_date_marker(value: Any) -> bool:
    raw = _date_raw(value)
    return bool(raw and _MISSING_DATE_MARKERS.fullmatch(raw))


def _date_parse_detail(value: Any, *, field: str = "date") -> Optional[str]:
    """Return an evidence message only for a clearly malformed date value."""

    raw = _date_raw(value)
    if not raw or _is_missing_date_marker(raw) or parse_datetime(value) is not None:
        return None
    return f"{field} could not be parsed: {raw!r}"


def _record_error(metadata: dict[str, Any], classification: str, detail: str) -> None:
    """Attach stable categories and original details to parser evidence."""

    category = str(classification).strip().lower().replace("-", "_")
    categories = metadata.setdefault("error_classifications", [])
    if category not in categories:
        categories.append(category)
    metadata.setdefault("error_classification", category)
    details = metadata.setdefault("error_details", [])
    if detail not in details:
        details.append(detail)


def _month_key(value: Any) -> str:
    """Normalize an accented month token without depending on system locale."""

    text = str(value or "").strip().casefold().rstrip(".,;")
    # ``ß`` is not decomposed by NFKD, while German sources commonly use it
    # in ``März``/``Mär``.  The replacement also keeps the lookup table ASCII.
    text = text.replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char))


# These are intentionally explicit rather than locale-dependent.  A host may
# serve a language different from the machine locale, and locale availability
# differs across deployments.  Include common abbreviations seen in news
# feeds and visible article datelines as well as full names.
# Keep each language table independent so shared spellings (for example
# ``april`` and ``september``) are not duplicate literals in one dictionary.
_GERMAN_MONTHS = {
    "januar": 1, "jan": 1,
    "februar": 2, "feb": 2,
    "marz": 3, "maerz": 3, "mar": 3, "maer": 3,
    "april": 4, "apr": 4,
    "mai": 5,
    "juni": 6, "jun": 6,
    "juli": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "okt": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "dez": 12,
}
_ENGLISH_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_DUTCH_MONTHS = {
    "januari": 1, "februari": 2, "maart": 3, "mrt": 3,
    "april": 4, "mei": 5, "juni": 6, "juli": 7,
    "augustus": 8, "september": 9, "oktober": 10,
    "november": 11, "december": 12,
}
_FRENCH_MONTHS = {
    "janvier": 1, "janv": 1,
    "fevrier": 2, "fevr": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "aout": 8,
    "septembre": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11,
    "decembre": 12, "dec": 12,
}
_SPANISH_MONTHS = {
    "enero": 1, "ene": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8, "ago": 8,
    "septiembre": 9, "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12, "dic": 12,
}
_DATELINE_MONTHS = {
    **_GERMAN_MONTHS,
    **_ENGLISH_MONTHS,
    **_DUTCH_MONTHS,
    **_FRENCH_MONTHS,
    **_SPANISH_MONTHS,
}

# A date-only value is deliberately narrow.  Requiring a day, month and
# four-digit year prevents arbitrary prose (or a year mentioned in an article)
# from becoming a publication timestamp.
_DATE_ONLY_DAY_FIRST = re.compile(
    r"^\s*(?:(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
    r"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|"
    r"maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag|"
    r"lunes|martes|mi[eé]rcoles|jueves|viernes|s[aá]bado|domingo)\s*,?\s*)?"
    r"(?P<day>\d{1,2})(?:\s*[.]|(?:st|nd|rd|th))?\s+"
    r"(?:de\s+)?(?P<month>[^\W\d_]+\.?)\s+(?:de\s+)?(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)
_DATE_ONLY_MONTH_FIRST = re.compile(
    r"^\s*(?P<month>[^\W\d_]+\.?)\s+(?P<day>\d{1,2})\s*,?\s+(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)
_DATE_ONLY_NUMERIC_DAY_FIRST = re.compile(
    r"^\s*(?P<day>\d{1,2})\s*\.\s*(?P<month>\d{1,2})\s*\.\s*(?P<year>\d{4})\s*$"
)

# ``datetime.fromisoformat`` in the minimum supported Python version accepts
# only three or six fractional-second digits.  ISO-8601 permits one through
# nine, so normalize that narrow form before handing it to the stdlib parser.
# Keep the timezone grammar explicit: malformed offsets must not be silently
# reinterpreted as a different date format by a later parser fallback.
_ISO_FRACTIONAL_SECONDS = re.compile(
    r"^(?P<prefix>\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2})"
    r"\.(?P<fraction>\d{1,9})"
    r"(?P<timezone>Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)?$"
)
_ISO_FRACTIONAL_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}\."
)


def _normalize_iso_fractional_seconds(candidate: str) -> Optional[str]:
    """Normalize ISO fractional seconds to Python's six-digit precision.

    Short fractions are padded with zeroes and longer fractions are truncated
    (never rounded), preserving the representable prefix without fabricating
    sub-microsecond precision.  ``None`` means that the candidate is not this
    explicitly supported ISO shape and should use the normal parser path.
    """

    match = _ISO_FRACTIONAL_SECONDS.fullmatch(candidate)
    if match is None:
        return None
    fraction = match.group("fraction").ljust(6, "0")[:6]
    return f"{match.group('prefix')}.{fraction}{match.group('timezone') or ''}"


def _parse_numeric_date(candidate: str) -> Optional[datetime]:
    """Parse the explicit day-month-year dotted form used by German sources.

    Slash-separated numeric dates are intentionally not accepted here: without
    a source-locale argument, values such as ``04/03/2025`` are ambiguous.
    """

    match = _DATE_ONLY_NUMERIC_DAY_FIRST.match(candidate)
    if match is None:
        return None
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            tzinfo=timezone.utc,
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_localized_date(candidate: str) -> Optional[datetime]:
    """Parse a strict day/month/year date in supported common languages."""

    match = _DATE_ONLY_DAY_FIRST.match(candidate) or _DATE_ONLY_MONTH_FIRST.match(candidate)
    if match is None:
        return None
    try:
        day = int(match.group("day"))
        year = int(match.group("year"))
        month = _DATELINE_MONTHS.get(_month_key(match.group("month")))
        if month is None:
            return None
        return datetime(year, month, day, tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def parse_datetime(value: Any) -> Optional[datetime]:
    """Parse ISO-8601, RFC-2822 and supported localized date-only values.

    Unknown or malformed input returns ``None``.  Callers that need audit
    evidence should retain the original value in a dedicated ``*_raw`` field;
    returning arbitrary text here makes downstream timestamp handling unsafe.
    """

    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    candidate = _date_raw(value)
    if not candidate:
        return None
    try:
        normalized_iso = _normalize_iso_fractional_seconds(candidate)
        # Python's ISO parser is permissive about an empty fraction and can
        # normalize out-of-range offset minutes (for example ``+02:60``).
        # Once the candidate has an ISO fractional marker, reject anything
        # that did not match the strict form above instead of falling through
        # to that permissive behavior.
        if normalized_iso is None and _ISO_FRACTIONAL_PREFIX.match(candidate):
            return None
        iso_candidate = normalized_iso or candidate
        parsed = datetime.fromisoformat(iso_candidate.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = parsedate_to_datetime(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, OverflowError):
        pass
    return _parse_numeric_date(candidate) or _parse_localized_date(candidate)


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


def _resolve_transport(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """Resolve a server-request URL without changing query semantics.

    This path is for pagination and other intermediate transport requests.
    In particular, TYPO3 archive links may use query ordering, percent
    escaping, ``gclid``/``_gl`` and ``cHash`` as server-visible state.  Those
    values must reach the fetcher exactly as authored.  RequestPolicy remains
    responsible for scheme, host, robots and redirect checks.
    """

    if not value:
        return None
    try:
        return urljoin(base_url or "", str(value).strip())
    except (TypeError, ValueError, UnicodeError):
        return None


def _resolve_identity(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """Resolve an article/canonical URL into its normalized identity form."""

    resolved = _resolve_transport(value, base_url)
    if not resolved:
        return None
    try:
        normalized = normalize_url(resolved)
        parts = urlsplit(normalized)
        if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
            return None
        # Accessing hostname/port validates malformed IPv6 and ports too.
        if not parts.hostname:
            return None
        parts.port
        return normalized
    except (TypeError, ValueError, UnicodeError):
        return None


def _resolve(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """Backward-compatible identity resolver used by feed/page parsers."""

    return _resolve_identity(value, base_url)


def _resolve_http(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    """Resolve a URL and keep only absolute HTTP(S) targets.

    Canonical and image fields are persisted as typed URL strings.  Rejecting
    other schemes here prevents a ``javascript:`` or ``data:`` value from
    being mistaken for an article/image URL while retaining relative links
    from ordinary pages.
    """

    return _resolve_identity(value, base_url)


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
            resolved = _resolve_http(link.attrib.get("href") or _element_text(link), base_url)
            if resolved:
                return resolved
    for link in links:
        resolved = _resolve_http(link.attrib.get("href") or _element_text(link), base_url)
        if resolved:
            return resolved
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
            link = _resolve_http(guid, base_url)
        if not link:
            continue
        # ElementTree elements with text but no child elements are falsey;
        # select by presence rather than truthiness so ordinary RSS
        # ``description`` and Atom ``summary`` text remains feed evidence.
        summary_element = next(
            (
                element
                for element in (
                    _first_child(entry, "summary"),
                    _first_child(entry, "description"),
                    _first_child(entry, "content"),
                )
                if element is not None
            ),
            None,
        )
        pub = _element_text(_first_child(entry, "published")) or _element_text(
            _first_child(entry, "pubdate")
        ) or _element_text(_first_child(entry, "date"))
        updated = _element_text(_first_child(entry, "updated")) or _element_text(
            _first_child(entry, "lastbuilddate")
        ) or _element_text(_first_child(entry, "modified"))
        canonical = None
        for link_element in _children(entry, "link"):
            if (link_element.attrib.get("rel") or "").lower() == "canonical":
                canonical = _resolve_http(link_element.attrib.get("href") or _element_text(link_element), base_url)
                if canonical:
                    break
        metadata = {"source_format": "atom" if root_name == "feed" else "rss"}
        if pub:
            metadata["published_at_raw"] = pub
            date_error = _date_parse_detail(pub, field="published_at")
            if date_error:
                _record_error(metadata, "date_parse_error", date_error)
        if updated:
            metadata["updated_at_raw"] = updated
            updated_error = _date_parse_detail(updated, field="updated_at")
            if updated_error:
                _record_error(metadata, "date_parse_error", updated_error)
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
                metadata=metadata,
            )
        )
    return result


def parse_rss(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedFeedItem]:
    return parse_feed(payload, base_url)


def parse_atom(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedFeedItem]:
    return parse_feed(payload, base_url)


# ---------------------------------------------------------------------------
# Bounded generic JSON API parsing


def _json_path_parts(path: Any) -> Optional[list[str]]:
    """Turn a dotted/JSON-pointer field path into bounded key segments.

    Configuration is data only: paths are never evaluated as Python and do
    not support wildcards or callbacks.  A list/tuple is accepted as an
    explicit sequence of mapping keys/indexes for YAML ergonomics.
    """

    if path is None:
        return None
    if isinstance(path, Mapping):
        path = path.get("path", path.get("field"))
    if isinstance(path, (list, tuple)):
        raw_parts = list(path)
    elif isinstance(path, str):
        value = path.strip()
        if not value:
            return []
        if value.startswith("/"):
            raw_parts = [part.replace("~1", "/").replace("~0", "~") for part in value.split("/")[1:]]
        else:
            # Permit the common ``items[0].url`` notation without making the
            # parser a general expression language.
            if value.startswith(".") or value.endswith((".", "[", "]")) or ".." in value or ".[" in value:
                return None
            raw_parts = [part for part in re.split(r"\.|\[|\]", value) if part != ""]
    else:
        return None
    parts = [str(part).strip() for part in raw_parts]
    if len(parts) > 16 or any(not part or part in {"*", "**"} for part in parts):
        return None
    return parts


def _json_get(value: Any, path: Any) -> Any:
    """Read a configured JSON path, returning ``None`` for shape drift."""

    parts = _json_path_parts(path)
    if parts is None:
        return None
    current = value
    for part in parts:
        if isinstance(current, Mapping):
            if part in current:
                current = current[part]
                continue
            # Minor API revisions sometimes alter only key casing.
            lowered = part.casefold()
            match = next((key for key in current if str(key).casefold() == lowered), None)
            if match is None:
                return None
            current = current[match]
        elif isinstance(current, (list, tuple)):
            try:
                current = current[int(part)]
            except (TypeError, ValueError, IndexError):
                return None
        else:
            return None
    return current


def _json_path_candidates(config: Mapping[str, Any], field: str) -> list[Any]:
    """Collect explicit and compatibility field paths in deterministic order."""

    result: list[Any] = []
    for container_key in ("fields", "paths", "field_paths", "json_paths", "selectors"):
        container = config.get(container_key)
        if isinstance(container, Mapping) and field in container:
            result.append(container[field])
    for key in (
        f"{field}_path",
        f"{field}_field",
        field,
    ):
        if key in config:
            result.append(config[key])
    return result


def _json_first(value: Any, paths: Iterable[Any]) -> Any:
    for path in paths:
        candidate = _json_get(value, path)
        if candidate is not None:
            return candidate
    return None


def _json_items(root: Any, config: Mapping[str, Any]) -> list[Any]:
    """Locate a bounded collection while tolerating one level of drift."""

    explicit = None
    for key in ("items_path", "item_path", "results_path", "articles_path", "news_path", "data_path"):
        if key in config:
            explicit = config[key]
            break
    if explicit is None:
        for container_key in ("paths", "json_paths"):
            container = config.get(container_key)
            if isinstance(container, Mapping):
                explicit = container.get("items", container.get("items_path"))
                if explicit is not None:
                    break
    if explicit is not None:
        value = _json_get(root, explicit)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, Mapping):
            for nested_key in ("items", "results", "articles", "news", "entries", "records"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return list(nested)
        return []
    if isinstance(root, list):
        return list(root)
    if not isinstance(root, Mapping):
        return []
    # Most APIs use one of these collection keys.  ``data`` may wrap the
    # actual list, so inspect one additional mapping level only.
    for key in ("items", "results", "articles", "news", "entries", "records", "data"):
        value = root.get(key)
        if isinstance(value, list):
            return list(value)
        if isinstance(value, Mapping):
            for nested_key in ("items", "results", "articles", "news", "entries", "records"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return list(nested)
    return []


def _json_text(value: Any) -> Optional[str]:
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _text(value)
    return None


def parse_json_api(
    payload: Union[str, bytes, bytearray, Mapping[str, Any], list[Any]],
    config: Optional[Mapping[str, Any]] = None,
    base_url: Optional[str] = None,
    *,
    deduplicate: bool = True,
) -> List[ParsedFeedItem]:
    """Parse a bounded JSON API response into feed-like discovery items.

    ``config`` supplies JSON field paths (for example ``items_path:
    data.articles`` and ``fields: {url: links.html, date: publishedAt}``).
    Relative article links are resolved against ``base_url`` and normalized;
    malformed/non-HTTP links and records without a usable URL are skipped.
    The parser intentionally performs no network, persistence, or host-policy
    checks—the discovery layer applies the official-host URL policy after
    parsing.
    """

    if isinstance(payload, Mapping) or isinstance(payload, list):
        root: Any = payload
    else:
        text = payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
        root = json.loads(text)
    cfg = dict(config or {})
    max_items_value = cfg.get("max_items", cfg.get("limit", 500))
    try:
        max_items = int(max_items_value)
    except (TypeError, ValueError, OverflowError):
        max_items = 500
    max_items = max(1, min(max_items, 1000))

    items = _json_items(root, cfg)
    result: list[ParsedFeedItem] = []
    seen: set[str] = set()
    for raw_item in items[:max_items]:
        if isinstance(raw_item, str):
            raw_item = {"url": raw_item}
        if not isinstance(raw_item, Mapping):
            continue
        url_value = _json_first(raw_item, _json_path_candidates(cfg, "url"))
        if url_value is None:
            url_value = _json_first(raw_item, ("url", "link", "href", "permalink", "uri"))
        if isinstance(url_value, Mapping):
            url_value = _json_first(url_value, ("url", "href", "html", "web", "permalink"))
        url = _resolve_http(_json_text(url_value), base_url)
        if not url:
            continue
        canonical_value = _json_first(raw_item, _json_path_candidates(cfg, "canonical_url"))
        if canonical_value is None:
            canonical_value = _json_first(raw_item, ("canonical_url", "canonical", "url_canonical"))
        if isinstance(canonical_value, Mapping):
            canonical_value = _json_first(canonical_value, ("url", "href"))
        canonical = _resolve_http(_json_text(canonical_value), base_url)

        identity = canonical or url
        if deduplicate and identity in seen:
            continue
        seen.add(identity)

        title_value = _json_first(raw_item, _json_path_candidates(cfg, "title"))
        if title_value is None:
            title_value = _json_first(raw_item, ("title", "headline", "name"))
        if isinstance(title_value, Mapping):
            title_value = _json_first(title_value, ("text", "value"))
        date_value = _json_first(raw_item, _json_path_candidates(cfg, "published_at"))
        if date_value is None:
            date_value = _json_first(raw_item, _json_path_candidates(cfg, "date"))
        if date_value is None:
            date_value = _json_first(raw_item, ("published_at", "publishedAt", "datePublished", "date", "pubDate", "created_at", "createdAt"))
        updated_value = _json_first(raw_item, _json_path_candidates(cfg, "updated_at"))
        if updated_value is None:
            updated_value = _json_first(raw_item, ("updated_at", "updatedAt", "dateModified", "modified_at"))
        summary_value = _json_first(raw_item, _json_path_candidates(cfg, "summary"))
        if summary_value is None:
            summary_value = _json_first(raw_item, ("summary", "description", "excerpt", "teaser"))
        author_value = _json_first(raw_item, _json_path_candidates(cfg, "author"))
        if author_value is None:
            author_value = _json_first(raw_item, ("author", "byline", "creator"))
        if isinstance(author_value, Mapping):
            author_value = _json_first(author_value, ("name", "displayName", "text"))

        metadata: dict[str, Any] = {"source_format": "json_api"}
        date_raw = _date_raw(date_value)
        if date_raw:
            metadata["published_at_raw"] = date_raw
            date_error = _date_parse_detail(date_value, field="published_at")
            if date_error:
                _record_error(metadata, "date_parse_error", date_error)
        updated_raw = _date_raw(updated_value)
        if updated_raw:
            metadata["updated_at_raw"] = updated_raw
            updated_error = _date_parse_detail(updated_value, field="updated_at")
            if updated_error:
                _record_error(metadata, "date_parse_error", updated_error)
        if isinstance(raw_item.get("id"), (str, int)):
            metadata["api_id"] = str(raw_item["id"])

        result.append(
            ParsedFeedItem(
                url=url,
                canonical_url=canonical,
                title=_json_text(title_value),
                published_at=parse_datetime(date_value),
                updated_at_source=parse_datetime(updated_value),
                summary=_json_text(summary_value),
                author=_json_text(author_value),
                guid=_json_text(raw_item.get("id") or raw_item.get("uuid")),
                metadata=metadata,
            )
        )
    return result


# Naming aliases used by adapters and integrations.
parse_json = parse_json_api
parse_json_feed = parse_json_api


def parse_sitemap_urlset(payload: Union[str, bytes], base_url: Optional[str] = None) -> List[ParsedSitemapEntry]:
    """Parse a sitemap ``<urlset>`` document."""

    root = _xml_root(payload)
    entries: list[ParsedSitemapEntry] = []
    for url_element in [element for element in root.iter() if _local_name(element.tag) == "url"]:
        location = _resolve(_element_text(_first_child(url_element, "loc")), base_url)
        if location:
            lastmod_raw = _element_text(_first_child(url_element, "lastmod"))
            metadata = {"source_format": "sitemap_urlset"}
            if lastmod_raw:
                metadata["lastmod_raw"] = lastmod_raw
                date_error = _date_parse_detail(lastmod_raw, field="lastmod")
                if date_error:
                    _record_error(metadata, "date_parse_error", date_error)
            entries.append(
                ParsedSitemapEntry(
                    url=location,
                    lastmod=parse_datetime(lastmod_raw),
                    metadata=metadata,
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
            lastmod_raw = _element_text(_first_child(sitemap_element, "lastmod"))
            metadata = {"source_format": "sitemap_index"}
            if lastmod_raw:
                metadata["lastmod_raw"] = lastmod_raw
                date_error = _date_parse_detail(lastmod_raw, field="lastmod")
                if date_error:
                    _record_error(metadata, "date_parse_error", date_error)
            entries.append(
                ParsedSitemapEntry(
                    url=location,
                    lastmod=parse_datetime(lastmod_raw),
                    is_index=True,
                    metadata=metadata,
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


def _meta_value(soup: Any, *names: str) -> Optional[str]:
    wanted = {name.lower() for name in names}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in wanted and tag.get("content"):
            return str(tag.get("content")).strip() or None
    return None


def _meta(soup: Any, *names: str) -> Optional[str]:
    value = _meta_value(soup, *names)
    if value is not None:
        return _text(value)
    return None


def _language_tag(value: Any) -> Optional[str]:
    """Return a conservative normalized BCP-47-like language tag."""

    if value is None:
        return None
    candidate = str(value).strip().replace("_", "-")
    if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", candidate):
        return None
    parts = candidate.split("-")
    return "-".join([parts[0].lower(), *[part.upper() if len(part) == 2 else part for part in parts[1:]]])


def _select_one(node: Any, selector: Optional[str]) -> Any:
    if not selector:
        return None
    try:
        return node.select_one(selector)
    except Exception:
        return None


_TEASER_CLASS_MARKERS = frozenset(
    {
        "card",
        "cards",
        "article-card",
        "news-card",
        "post-card",
        "teaser",
        "related",
        "related-post",
        "related-article",
        "news-grid-item",
        "grid-item",
        "list-item",
        "news-list-item",
        "post-highlights-block",
        "highlight-card",
    }
)
_TEASER_CLASS_SUFFIXES = ("-card", "-teaser", "-grid-item", "-list-item")
_TEASER_CONTEXT_MARKERS = frozenset(
    {
        "related",
        "teaser",
        "recommend",
        "recommended",
        "highlights",
        "news-list",
        "archive",
        "navigation",
        "navbar",
        "menu",
        "breadcrumb",
    }
)


def _class_tokens(node: Any) -> set[str]:
    """Return normalized class/id tokens for content-root heuristics."""

    # BeautifulSoup's ``decompose`` clears ``Tag.attrs`` on the removed
    # subtree.  Generic-content cleanup intentionally works on snapshots of
    # nested nodes, so a stale Tag can reach this helper after its parent was
    # decomposed.  Treat such a node as having no classification instead of
    # calling ``Tag.get`` on a ``None`` attrs mapping.
    attrs = getattr(node, "attrs", None)
    if not isinstance(attrs, Mapping):
        return set()
    values: list[str] = []
    classes = attrs.get("class")
    if isinstance(classes, (list, tuple, set)):
        values.extend(str(value) for value in classes)
    elif classes:
        values.append(str(classes))
    identifier = attrs.get("id")
    if identifier:
        values.append(str(identifier))
    tokens: set[str] = set()
    for value in values:
        normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
        if normalized:
            tokens.add(normalized)
    return tokens


def _looks_like_teaser(node: Any) -> bool:
    """Recognize common listing/card wrappers without rejecting article prose."""

    tokens = _class_tokens(node)
    for token in tokens:
        if token in _TEASER_CLASS_MARKERS or token.endswith(_TEASER_CLASS_SUFFIXES):
            return True
        if token in _TEASER_CONTEXT_MARKERS or any(
            marker in token
            for marker in (
                "related",
                "recommend",
                "highlight",
                "card",
                "teaser",
                "grid-item",
                "list-item",
                "news-grid",
                "news-item",
            )
        ):
            return True
    return False


def _article_config_mapping(config: Any) -> Mapping[str, Any]:
    """Expose article selectors from mappings and ``ArchiveParserConfig``."""

    if isinstance(config, Mapping):
        return config
    if isinstance(config, ArchiveParserConfig):
        return {
            "article_content_selector": config.article_content_selector,
            "article_content_exclude_selector": config.article_content_exclude_selector,
        }
    return {}


def _configured_content_node(soup: Any, config: Mapping[str, Any]) -> Any:
    """Select and clone an explicitly configured article content root.

    A small number of publishers expose a CSS union for old and new article
    shells.  During a rollout the modern root can remain in the response as
    an empty placeholder while the legacy paragraph carries the story.  Pick
    the first configured match with usable text so that an empty placeholder
    cannot mask the configured fallback.  If every match is empty, retain the
    first match and let the normal content-parse error path report it.
    """

    selector = config.get("article_content_selector")
    if not isinstance(selector, str) or not selector.strip():
        return None
    try:
        nodes = soup.select(selector)
    except Exception:
        nodes = []
    if not nodes:
        return None
    first = nodes[0]
    for node in nodes:
        if _node_text(node):
            return deepcopy(node)
    return deepcopy(first)


def _is_descendant(node: Any, ancestor: Any) -> bool:
    """Return whether ``node`` is nested below ``ancestor`` by identity."""

    current = getattr(node, "parent", None)
    while current is not None:
        if current is ancestor:
            return True
        current = getattr(current, "parent", None)
    return False


def _decompose_top_level(nodes: Iterable[Any]) -> None:
    """Decompose matching nodes once, skipping descendants of removed nodes."""

    removals: list[Any] = []
    for node in nodes:
        if getattr(node, "name", None) is None or not isinstance(getattr(node, "attrs", None), Mapping):
            continue
        if any(_is_descendant(node, parent) for parent in removals):
            continue
        removals.append(node)
    for node in removals:
        # A previous decomposition can only occur through an ancestor in this
        # list, which is filtered above; this guard also keeps the helper safe
        # if callers pass a pre-mutated Tag iterable.
        if getattr(node, "name", None) is not None and isinstance(getattr(node, "attrs", None), Mapping):
            node.decompose()


def _reliable_generic_content_node(soup: Any) -> Any:
    """Choose an article/main root while avoiding listing and related cards."""

    for node in soup.find_all("article"):
        if not _looks_like_teaser(node):
            return deepcopy(node)

    # A page-level ``main`` can be a legitimate article body, but a main that
    # only wraps cards/nav is not.  Remove obvious listing descendants on a
    # clone solely for this confidence check; the original remains untouched.
    for node in soup.find_all("main"):
        if _looks_like_teaser(node):
            continue
        candidate = deepcopy(node)
        structural = candidate.find_all(["article", "aside", "nav", "footer", "header"])
        structural_to_remove = [
            unwanted
            for unwanted in structural
            if not (
                unwanted.name == "article"
                and not _looks_like_teaser(unwanted)
            )
        ]
        _decompose_top_level(structural_to_remove)
        # Build the teaser match list before decomposing anything.  This
        # avoids traversing stale descendants whose parent teardown has
        # already cleared their attrs/name.
        teaser_nodes = [unwanted for unwanted in candidate.find_all(True) if _looks_like_teaser(unwanted)]
        _decompose_top_level(teaser_nodes)
        text = _node_text(candidate)
        if not text:
            continue
        if candidate.find(["p", "blockquote", "figure", "table", "pre"]):
            return candidate
        # Keep support for publishers that put prose directly in divs, but
        # require a substantial non-link body so a navigation shell cannot be
        # mistaken for content.
        if len(text) >= 160 and not candidate.find("a"):
            return candidate
    return None


def _exclude_content_nodes(content_node: Any, config: Mapping[str, Any]) -> None:
    """Delete configured descendants before extracting article text/HTML."""

    selectors = config.get("article_content_exclude_selector")
    if isinstance(selectors, str):
        selectors = [selectors]
    elif isinstance(selectors, (list, tuple)):
        selectors = list(selectors)
    else:
        selectors = []
    for selector in selectors:
        if not isinstance(selector, str) or not selector.strip():
            continue
        try:
            matches = content_node.select(selector)
        except Exception:
            continue
        for match in matches:
            match.decompose()


def parse_archive_page(
    payload: Union[str, bytes],
    config: Optional[Union[ArchiveParserConfig, Mapping[str, Any]]] = None,
    base_url: Optional[str] = None,
    *,
    deduplicate: bool = True,
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
        if not resolved or (deduplicate and resolved in seen):
            continue
        seen.add(resolved)
        title_node = _select_one(card, cfg.title_selector) if cfg.title_selector else None
        title = _node_text(title_node) or _node_text(link_node)
        date_node = _select_one(card, cfg.date_selector) if cfg.date_selector else None
        date_value = (date_node.get("datetime") if date_node is not None else None) or _node_text(date_node)
        metadata = {"source_format": "archive"}
        if date_value:
            metadata["published_at_raw"] = str(date_value).strip()
            date_error = _date_parse_detail(date_value, field="published_at")
            if date_error:
                _record_error(metadata, "date_parse_error", date_error)
        result.append(
            ParsedFeedItem(
                url=resolved,
                title=title,
                published_at=parse_datetime(date_value),
                metadata=metadata,
            )
        )
    return result


def parse_archive(
    payload: Union[str, bytes],
    config: Optional[Union[ArchiveParserConfig, Mapping[str, Any]]] = None,
    base_url: Optional[str] = None,
    fetch_page: Optional[Callable[[str], Union[str, bytes]]] = None,
    max_pages: Optional[int] = None,
    *,
    preserve_duplicates: bool = False,
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
        for item in parse_archive_page(
            current_payload, cfg, current_url, deduplicate=not preserve_duplicates,
        ):
            if preserve_duplicates or item.url not in seen_urls:
                seen_urls.add(item.url)
                result.append(item)
        if not fetch_page or not cfg.pagination_selector or page_number + 1 >= page_limit:
            break
        soup = _soup(current_payload)
        next_node = _select_one(soup, cfg.pagination_selector)
        next_href = next_node.get("href") if next_node is not None else None
        if not next_href:
            break
        # Pagination is a transport hop, not an article identity.  Preserve
        # the complete href query (including order and cHash) and delegate
        # host/robots validation to the injected RequestPolicy-aware fetcher.
        next_url = _resolve_transport(next_href, current_url)
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


def _normalized_month(value: str) -> Optional[int]:
    return _DATELINE_MONTHS.get(_month_key(value))


def _configured_article_dateline_details(
    soup: Any, config: Optional[Mapping[str, Any]]
) -> tuple[Optional[str], Optional[str]]:
    """Return a validated configured date and the source dateline text."""

    cfg = config or {}
    selector = cfg.get("article_date_selector")
    pattern = cfg.get("article_date_pattern")
    if not isinstance(selector, str) or not selector or not isinstance(pattern, str) or not pattern:
        return None, None
    try:
        nodes = soup.select(selector)
        expression = re.compile(pattern, re.IGNORECASE)
    except (Exception, re.error):
        return None, None
    # A dateline is structural lead-in content. Restricting the search avoids
    # interpreting dates mentioned later in the story as publication dates.
    for node in nodes[:3]:
        value = _node_text(node)
        match = expression.match(value or "")
        if match is None:
            continue
        try:
            day = int(match.group("day"))
            month = _normalized_month(match.group("month"))
            year = int(match.group("year"))
            if month is None:
                continue
            return datetime(year, month, day).date().isoformat(), value
        except (IndexError, TypeError, ValueError, OverflowError):
            continue
    return None, None


def _configured_article_dateline(soup: Any, config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """Return a validated date-only value from leading configured paragraphs."""
    value, _ = _configured_article_dateline_details(soup, config)
    return value


def _validated_article_datetime(value: Any) -> Optional[datetime]:
    """Parse an article timestamp without retaining arbitrary invalid text."""

    parsed = parse_datetime(value)
    return parsed if isinstance(parsed, datetime) else None


def _meta_datetime(soup: Any, *names: str) -> Optional[datetime]:
    wanted = {name.lower() for name in names}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        if key in wanted:
            parsed = _validated_article_datetime(tag.get("content"))
            if parsed is not None:
                return parsed
    return None


_ARTICLE_TYPES = frozenset({"article", "newsarticle", "blogposting"})


def _jsonld_type_names(value: Any) -> set[str]:
    values = value if isinstance(value, list) else [value]
    names: set[str] = set()
    for item in values:
        if not item:
            continue
        # Schema.org values are occasionally emitted as a full URL.
        names.add(str(item).rsplit("/", 1)[-1].casefold())
    return names


def _is_article_jsonld(item: Mapping[str, Any]) -> bool:
    return bool(_jsonld_type_names(item.get("@type")) & _ARTICLE_TYPES)


def _jsonld_image_value(value: Any) -> Optional[str]:
    """Extract a URL-like image value from JSON-LD's string/list/object forms."""

    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        for key in ("url", "contentUrl", "thumbnailUrl"):
            candidate = _jsonld_image_value(value.get(key))
            if candidate:
                return candidate
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            candidate = _jsonld_image_value(item)
            if candidate:
                return candidate
    return None


def _srcset_first(value: Any) -> Optional[str]:
    if not value:
        return None
    # A srcset candidate is ``URL [descriptor]``; the first candidate is a
    # deterministic fallback when no viewport information is available.
    return str(value).split(",", 1)[0].strip().split(None, 1)[0] or None


_CONTENT_ALLOWED_TAGS = frozenset(
    {
        "a", "abbr", "b", "blockquote", "br", "caption", "cite", "code", "dd",
        "del", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2",
        "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre",
        "q", "s", "small", "span", "strong", "sub", "sup", "table", "tbody",
        "td", "tfoot", "th", "thead", "time", "tr", "u", "ul",
    }
)
_CONTENT_DROP_TAGS = frozenset(
    {
        "base", "button", "canvas", "embed", "form", "iframe", "input", "link",
        "math", "meta", "noscript", "object", "option", "script", "select", "source",
        "style", "svg", "template", "textarea", "track", "video", "audio",
    }
)
_CONTENT_ALLOWED_ATTRIBUTES = frozenset(
    {
        "alt", "aria-label", "class", "cite", "colspan", "datetime", "dir", "height",
        "id", "lang", "loading", "name", "role", "rowspan", "scope", "start", "title",
        "width", "href", "src",
    }
)
_CONTENT_URL_ATTRIBUTES = frozenset({"cite", "href", "src"})


def _safe_content_url(value: Any) -> Optional[str]:
    """Allow only HTTP(S) and relative URL references in stored markup."""

    if value is None or isinstance(value, (list, tuple, dict)):
        return None
    candidate = html_lib.unescape(str(value)).strip()
    if not candidate:
        return None
    # Browsers ignore controls around a scheme in several URL contexts, so
    # check a compact form as well as urlsplit's normal interpretation.
    compact = re.sub(r"[\x00-\x20]+", "", candidate)
    try:
        parsed = urlsplit(compact)
        scheme = parsed.scheme.casefold()
        if scheme:
            if scheme not in {"http", "https"} or not parsed.netloc:
                return None
            parsed.hostname
            parsed.port
    except (TypeError, ValueError, UnicodeError):
        return None
    return candidate


def _clean_content_node(content_node: Any) -> tuple[Optional[str], Optional[str]]:
    """Sanitize article markup with explicit tag and attribute allowlists.

    This produces a conservative content representation for storage and
    later escaping.  It is not a promise that callers may mark the result as
    trusted HTML; raw response evidence remains in ``raw_html``.
    """

    # Remove complete active/metadata subtrees before unwrapping harmless
    # publisher-specific wrapper tags.  In particular, SVG and MathML are
    # dropped instead of trying to sanitize their expansive URL/event surface.
    for unwanted in content_node.find_all(list(_CONTENT_DROP_TAGS)):
        unwanted.decompose()
    for tag in list(content_node.find_all(True)):
        name = str(tag.name or "").lower()
        if name not in _CONTENT_ALLOWED_TAGS:
            tag.unwrap()
            continue
        for attribute in list(tag.attrs):
            attr_name = str(attribute).lower()
            if attr_name not in _CONTENT_ALLOWED_ATTRIBUTES:
                del tag.attrs[attribute]
                continue
            if attr_name in _CONTENT_URL_ATTRIBUTES:
                safe_value = _safe_content_url(tag.attrs.get(attribute))
                if safe_value is None:
                    del tag.attrs[attribute]
                else:
                    tag.attrs[attribute] = safe_value
    content = _node_text(content_node)
    try:
        content_html = content_node.decode_contents().strip() or None
    except AttributeError:
        content_html = None
    return content, content_html


def parse_article_page(
    payload: Union[str, bytes],
    url: Optional[str] = None,
    config: Optional[Union[Mapping[str, Any], ArchiveParserConfig]] = None,
) -> ParsedArticle:
    """Extract an article while keeping raw response evidence untouched.

    ``content``/``content_html`` are sanitized article views.  ``raw_html`` is
    the exact decoded response body and is never reused as rendered content.
    """

    raw_html = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
    soup = _soup(raw_html)
    article_config = _article_config_mapping(config)
    canonical = None
    for link in soup.find_all("link", rel=True):
        rel = link.get("rel")
        rel_values = [str(value).lower() for value in rel] if isinstance(rel, list) else [str(rel).lower()]
        if "canonical" in rel_values and link.get("href"):
            canonical = _resolve_http(link.get("href"), url)
            if canonical:
                break
    canonical = canonical or _resolve_http(_meta_value(soup, "og:url", "twitter:url"), url)
    # Resolve an explicitly configured article root before title fallback.  A
    # number of server-rendered publishers put the site/page heading before
    # the article body, while the article's own h1 is the useful title.  Keep
    # metadata title priority and leave the generic (unconfigured) path
    # unchanged so arbitrary page h1/title values cannot broaden content
    # extraction heuristics.
    configured_selector = article_config.get("article_content_selector")
    if isinstance(configured_selector, str) and configured_selector.strip():
        content_node = _configured_content_node(soup, article_config)
    else:
        content_node = None

    title = _meta(soup, "og:title", "twitter:title")
    if not title and content_node is not None:
        title = _node_text(content_node.find("h1"))
    title = title or _node_text(soup.find("h1")) or _node_text(soup.find("title"))

    published_at: Optional[datetime] = None
    published_at_raw: Optional[str] = None
    published_date_errors: list[str] = []

    def consider_published(value: Any) -> None:
        nonlocal published_at, published_at_raw
        raw = _date_raw(value)
        if raw and published_at is None and published_at_raw is None:
            # Keep an unknown source representation for auditability when no
            # valid candidate exists; a later valid candidate replaces it.
            published_at_raw = raw
        parsed = _validated_article_datetime(value)
        date_error = _date_parse_detail(value, field="published_at")
        if date_error and date_error not in published_date_errors:
            published_date_errors.append(date_error)
        if published_at is None and parsed is not None:
            published_at = parsed
            # Keep raw evidence only for the candidate that actually supplied
            # the selected timestamp.  A malformed meta value must not mask a
            # later valid time/JSON-LD/dateline candidate.
            published_at_raw = raw

    for name in (
        "article:published_time", "datepublished", "publish_date", "date", "pubdate",
    ):
        raw_value = _meta_value(soup, name)
        if raw_value is not None:
            consider_published(raw_value)
            if published_at is not None:
                break

    updated_at: Optional[datetime] = None
    updated_date_errors: list[str] = []
    for name in ("article:modified_time", "datemodified", "lastmod", "updated"):
        raw_value = _meta_value(soup, name)
        if raw_value is not None:
            updated_at = _validated_article_datetime(raw_value)
            date_error = _date_parse_detail(raw_value, field="updated_at")
            if date_error and date_error not in updated_date_errors:
                updated_date_errors.append(date_error)
            if updated_at is not None:
                break
    author = _meta(soup, "author", "article:author", "byline")
    summary = None
    for name in ("og:description", "twitter:description", "description"):
        summary = _meta(soup, name)
        if summary is not None:
            break
    json_ld: list[Mapping[str, Any]] = list(_json_ld_objects(soup))
    structured_language = _language_tag(
        _meta(soup, "content-language", "language", "article:language", "og:locale")
    )
    for item in json_ld:
        is_article = _is_article_jsonld(item)
        if is_article and item.get("datePublished") is not None:
            consider_published(item.get("datePublished"))
        if updated_at is None and is_article:
            modified_value = item.get("dateModified")
            updated_at = _validated_article_datetime(modified_value)
            date_error = _date_parse_detail(modified_value, field="updated_at")
            if date_error and date_error not in updated_date_errors:
                updated_date_errors.append(date_error)
        if not author:
            author_value = item.get("author")
            if isinstance(author_value, Mapping):
                author = author_value.get("name")
            elif isinstance(author_value, list):
                author = next(
                    (entry.get("name") for entry in author_value if isinstance(entry, Mapping) and entry.get("name")),
                    None,
                )
            elif isinstance(author_value, str):
                author = author_value
        if not summary and item.get("description"):
            summary = item.get("description")
        if not structured_language and (_is_article_jsonld(item) or "webpage" in _jsonld_type_names(item.get("@type"))):
            structured_language = _language_tag(item.get("inLanguage"))
    if published_at is None:
        for time_node in soup.find_all("time"):
            candidates = [time_node.get("datetime"), _node_text(time_node)]
            for candidate in candidates:
                if candidate is None:
                    continue
                consider_published(candidate)
                if published_at is not None:
                    break
            if published_at is not None:
                break
    configured_published_date = None
    if published_at is None:
        configured_published_date, configured_raw = _configured_article_dateline_details(soup, article_config)
        if configured_raw:
            published_at_raw = configured_raw

    # An explicit root is authoritative.  If it is absent from a response,
    # keep content empty rather than silently falling back to a listing card
    # with an unrelated teaser.  The configured ``content_node`` selected above
    # for title fallback is reused here so title and content observe the same
    # root.
    if not (isinstance(configured_selector, str) and configured_selector.strip()):
        content_node = _reliable_generic_content_node(soup)
    if content_node is not None:
        _exclude_content_nodes(content_node, article_config)
        content, content_html = _clean_content_node(content_node)
    else:
        content, content_html = None, None

    content_error: Optional[str] = None
    if content_node is None:
        configured_selector = article_config.get("article_content_selector")
        if isinstance(configured_selector, str) and configured_selector.strip():
            content_error = (
                "configured article content selector did not match: "
                f"{configured_selector}"
            )
        else:
            content_error = "article content could not be extracted from response"
    elif not content:
        content_error = "article content root contained no usable text"

    image_url = _resolve_http(_meta_value(
        soup, "og:image", "og:image:url", "og:image:secure_url", "twitter:image", "twitter:image:src"
    ), url)
    if image_url is None:
        for item in json_ld:
            if not (_is_article_jsonld(item) or not image_url):
                continue
            image_url = _resolve_http(
                _jsonld_image_value(item.get("image") or item.get("thumbnailUrl")), url
            )
            if image_url is not None:
                break
    if image_url is None:
        # Prefer an image in the article content.  The content node has
        # already been cleaned, so removed script/style descendants cannot
        # contribute an image URL.
        image_nodes = content_node.find_all("img") if content_node is not None else []
        if not image_nodes:
            image_nodes = soup.find_all("img")
        for image in image_nodes:
            image_value = (
                image.get("src")
                or image.get("data-src")
                or image.get("data-lazy-src")
                or _srcset_first(image.get("srcset"))
            )
            image_url = _resolve_http(image_value, url)
            if image_url is not None:
                break

    metadata: dict[str, Any] = {}
    for meta in soup.find_all("meta"):
        key = meta.get("property") or meta.get("name") or meta.get("itemprop")
        value = meta.get("content")
        if key and value:
            metadata[str(key)] = _text(value)
    html_node = soup.find("html")
    html_language = _language_tag(html_node.get("lang") if html_node is not None else None)
    if html_language:
        metadata["html_language"] = html_language
    if structured_language:
        metadata["structured_language"] = structured_language

    # A malformed date is materially different from a source that simply does
    # not publish a date.  Only retain the classification when no valid value
    # was found for the corresponding field; a later valid candidate is a
    # legitimate recovery path and should not fail the article.
    if published_at is None and configured_published_date is None:
        for detail in published_date_errors:
            _record_error(metadata, "date_parse_error", detail)
    if updated_at is None:
        for detail in updated_date_errors:
            _record_error(metadata, "date_parse_error", detail)
    if content_error:
        _record_error(metadata, "content_parse_error", content_error)

    final_published_at: Any = configured_published_date or published_at
    if published_at_raw:
        metadata["published_at_raw"] = published_at_raw
    if content_error:
        parse_status = "content_parse_error"
    elif content and title and final_published_at is not None:
        parse_status = "success"
    elif content or title or canonical or final_published_at is not None:
        parse_status = "partial"
    else:
        parse_status = "failed"
    metadata["parse_status"] = parse_status

    identity_content = content or content_html
    values = {
        "url": _resolve(url, None) if url else None,
        "canonical_url": canonical,
        "title": title,
        "published_at": final_published_at,
        "published_at_raw": published_at_raw,
        "updated_at_source": updated_at,
        "author": _text(author),
        "summary": _text(summary),
        "content": content,
        "content_html": content_html,
        "image_url": image_url,
        "parse_status": parse_status,
        "content_hash": hashlib.sha256(identity_content.encode("utf-8")).hexdigest() if identity_content else None,
        "raw_html": raw_html,
        "metadata": metadata,
    }
    return ParsedArticle(**values)


def parse_article(
    payload: Union[str, bytes],
    url: Optional[str] = None,
    config: Optional[Union[Mapping[str, Any], ArchiveParserConfig]] = None,
) -> ParsedArticle:
    return parse_article_page(payload, url, config)
