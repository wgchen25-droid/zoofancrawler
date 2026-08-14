"""Read-only acceptance/report projection for ZooFanCrawler.

The reporting layer intentionally does not use :class:`SQLiteStorage`'s write
or domain-record APIs.  A report is useful precisely when the database is
missing, partially migrated, or was produced by an older crawler, so this
module reads the small set of tables through SQLite introspection and treats
missing columns/tables as unknown values.

``build_projection`` is the single source of truth for all report formats.
``build_reports`` writes the projection-derived JSON, Markdown, CSV and a
self-contained HTML acceptance page.  Files are replaced one at a time with
an atomic rename; no existing files outside the five explicitly generated
paths are touched.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, fields as dataclass_fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import uuid
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit

from .adapters import CANONICAL_ADAPTER_KINDS, canonical_adapter_kind, normalize_adapter_token
from .config import CrawlerConfig, load_config
from .models import Source, Zoo
from .normalization import normalize_url


# Keep this vocabulary derived from the adapter registry rather than copying a
# second taxonomy into the reporting layer.  The preferred order keeps report
# output stable while the sorted tail makes newly-added canonical adapters
# visible without requiring an unsafe import or a reporting/adapters cycle.
_ADAPTER_DISPLAY_ORDER: Tuple[str, ...] = (
    "rss",
    "atom",
    "sitemap",
    "archive",
    "json_api",
    "custom_adapter",
    "registry_only",
    "unsupported",
)
KNOWN_ADAPTERS: Tuple[str, ...] = tuple(
    kind for kind in _ADAPTER_DISPLAY_ORDER if kind in CANONICAL_ADAPTER_KINDS
) + tuple(sorted(CANONICAL_ADAPTER_KINDS.difference(_ADAPTER_DISPLAY_ORDER)))
# ``unsupported`` is a known, explicit roster outcome, not an executable
# adapter.  Keep it in ``KNOWN_ADAPTERS`` for taxonomy/report rendering while
# excluding it from the support criterion and ``SUPPORTED_ADAPTERS``.
SUPPORTED_ADAPTERS: Tuple[str, ...] = tuple(
    kind for kind in KNOWN_ADAPTERS if kind != "unsupported"
)

# ``registry_only`` performs an official endpoint health check but does not
# discover article URLs.  Keep the historical adapter vocabulary above for
# compatibility while exposing the two operational source populations
# separately in coverage projections.
ARTICLE_CAPABLE_ADAPTERS: Tuple[str, ...] = tuple(
    kind for kind in SUPPORTED_ADAPTERS if kind != "registry_only"
)
HEALTH_ONLY_ADAPTERS = frozenset({"registry_only"})

# Stable run classification vocabulary rendered in every report.  The first
# six mirror crawler result/source statuses; the remaining three retain the
# most useful failure classes without leaking arbitrary exception metadata.
COMPLETION_CATEGORIES: Tuple[str, ...] = (
    "success",
    "success_no_items",
    "partial",
    "failed",
    "source_not_found",
    "unsupported",
    "parse_error",
    "blocked",
    "configuration_error",
)

# Public completion vocabulary.  The lower-case ``COMPLETION_CATEGORIES``
# values above remain in the projection for callers that consumed the first
# reporting contract; these labels are the stable, human-facing vocabulary
# used by every rendered report.
PUBLIC_COMPLETION_CLASSIFICATIONS: Tuple[str, ...] = (
    "SUPPORTED",
    "SUPPORTED_NO_CURRENT_NEWS",
    "PARTIALLY_SUPPORTED",
    "SOURCE_NOT_FOUND",
    "ROBOTS_DISALLOWED",
    "JAVASCRIPT_REQUIRED",
    "BLOCKED",
    "UNSUPPORTED",
    "FAILED",
)

# These are real operational outcomes, but they describe an external
# limitation rather than a defect in the report/crawl contract.  Keep them in
# completion counters and row evidence while excluding them from blocking
# ``failure_reasons`` and the overall acceptance result.
LIMITATION_COMPLETION_CATEGORIES = frozenset(
    {
        "source_not_found",
        "robots_disallowed",
        "javascript_required",
        "success_no_items",
        "blocked",
        "unsupported",
        "success_no_items",
    }
)

# These are the stable runtime classes that describe an external/environment
# boundary.  They remain visible in failure evidence, but do not fail the
# report's run-failure criterion unless a separate blocking class is present.
# ``partial`` is deliberately kept separate: it is a known public completion
# outcome and is nonblocking, but is not itself an external limitation.
EXTERNAL_FAILURE_CLASSES = frozenset(
    {
        "source_not_found",
        "robots_disallowed",
        "javascript_required",
        "blocked",
        "unsupported",
        "http_error",
        "timeout",
        "dns_error",
        "tls_error",
        "connection_error",
        "source_unavailable",
        "official_source_unavailable",
        "official_site_unavailable",
    }
)
NONBLOCKING_FAILURE_CLASSES = EXTERNAL_FAILURE_CLASSES | {
    "partial",
    "success_no_items",
}

_PUBLIC_CLASSIFICATION_BY_CATEGORY: Dict[str, str] = {
    "success": "SUPPORTED",
    "success_no_items": "SUPPORTED_NO_CURRENT_NEWS",
    "partial": "PARTIALLY_SUPPORTED",
    "source_not_found": "SOURCE_NOT_FOUND",
    "robots_disallowed": "ROBOTS_DISALLOWED",
    "javascript_required": "JAVASCRIPT_REQUIRED",
    "blocked": "BLOCKED",
    "unsupported": "UNSUPPORTED",
    # Parse/configuration failures are retained as raw error categories, but
    # are not separate public completion states.  They are failed support.
    "parse_error": "FAILED",
    "configuration_error": "FAILED",
    "failed": "FAILED",
}

_FAILURE_CLASS_ALIASES: Dict[str, str] = {
    "robots": "robots_disallowed",
    "robots_error": "robots_disallowed",
    "robots_unavailable": "robots_disallowed",
    "date_parse_error": "parse_error",
    "content_parse_error": "parse_error",
    "javascript": "javascript_required",
    "js_required": "javascript_required",
    "http": "http_error",
    "network": "http_error",
    "dns": "dns_error",
    "tls": "tls_error",
    "config": "configuration_error",
    "no_items": "success_no_items",
    "connection": "connection_error",
    "connection_failure": "connection_error",
    "connection_failed": "connection_error",
    "source_unavailable": "source_unavailable",
    "official_source_unavailable": "official_source_unavailable",
    "official_site_unavailable": "official_site_unavailable",
}

_QUALITY_BLOCKING_CODES = frozenset(
    {
        "empty_title",
        "bad_url",
        "empty_content",
        "duplicate_url",
        "duplicate_title",
        "non_official_host",
        "list_page_suspect",
        "navigation_content_suspect",
    }
)

QUALITY_WARNING_LABELS: Dict[str, str] = {
    "empty_title": "Empty title",
    "bad_url": "Invalid article URL",
    "future_date": "Publication date is in the future",
    "over_old": "Publication date is unusually old",
    "duplicate_url": "Duplicate article URL",
    "duplicate_title": "Duplicate article title",
    "empty_content": "Empty article body",
    "non_official_host": "Article host is not official",
    "stale": "Article evidence is stale",
    "same_date": "Articles share one publication date",
    "long_title": "Article title is unusually long",
    "list_page_suspect": "URL resembles a listing page",
    "navigation_content_suspect": "Content resembles navigation",
    "missing_result": "Missing or duplicate latest-run zoo result",
    "source_error": "Source/run error",
}

# These values are part of the report contract.  Keeping them constants makes
# quality findings deterministic for callers and tests that provide ``now``.
DEFAULT_OLD_DAYS = 365
DEFAULT_STALE_DAYS = 30
FUTURE_GRACE = timedelta(minutes=5)

_TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "zoos": ("zoos",),
    "sources": ("sources",),
    "articles": ("articles",),
    "discoveries": ("article_discoveries", "discoveries", "article_sources"),
    "runs": ("crawl_runs", "runs"),
    "stats": ("crawl_run_stats", "run_stats", "crawl_stats"),
    "zoo_results": ("crawl_zoo_results", "zoo_run_results", "crawl_results"),
    "identities": ("article_zoo_identities", "article_identities"),
}

_LIST_PATH_RE = re.compile(
    r"(?:^|/)(?:page(?:/|$)|category(?:/|$)|categories(?:/|$)|tag(?:/|$)|tags(?:/|$)|"
    r"archive(?:/|$)|archives(?:/|$)|search(?:/|$)|author(?:/|$)|feed(?:/|$)|news/?$)",
    re.IGNORECASE,
)
# ``navigation_content_suspect`` is a content-quality finding, not a second
# URL policy.  In particular, article paths commonly contain words such as
# ``about`` or ``news`` even when the parsed body is clean.  Keep the
# vocabulary in small, field-specific expressions so a URL token can never
# promote an otherwise good article to a blocking quality finding.
_NAVIGATION_TITLE_TOKEN = (
    r"(?:home(?:page)?|go\s+back|back\s+to(?:\s+(?:news|article))?|"
    r"contact(?:\s+us)?|privacy(?:\s+policy)?|cookie(?:\s+policy)?|"
    r"terms(?:\s+and\s+conditions)?|tickets?|shop|restaurant|jobs?|"
    r"careers?|vacatures?|sitemap|menu|about(?:\s+the\s+zoo)?|"
    r"route(?:\s*beschrijving)?|directions?|anfahrt|opening(?:\s+|-)hours?|"
    r"openingstijden|öffnungszeiten|share(?:\s+this\s+article)?|"
    r"related(?:\s+news)?|gerelateerde\s+nieuws|verwandte\s+nachrichten|"
    r"kontakt|zurück(?:\s+zu)?|terug(?:\s+naar)?|download)"
)
_NAVIGATION_TITLE_RE = re.compile(
    rf"^\s*{_NAVIGATION_TITLE_TOKEN}"
    rf"(?:\s*(?:[|:/·,\-]|\band\b|&)\s*{_NAVIGATION_TITLE_TOKEN})*\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_NAVIGATION_BODY_PREFIX_RE = re.compile(
    rf"^\s*(?:{_NAVIGATION_TITLE_TOKEN})\b",
    re.IGNORECASE,
)
# Keep this alias for callers that imported the old private expression while
# making its intended scope explicit: it is now title/body vocabulary only,
# never an article URL matcher.
_NAVIGATION_RE = _NAVIGATION_TITLE_RE
_WHITESPACE_RE = re.compile(r"\s+")
_SENSITIVE_QUERY_KEY_RE = re.compile(
    r"(?:^|[_-])(?:token|password|pass|secret|api[_-]?key|access[_-]?token|auth|signature|sig|credential|"
    r"client[_-]?secret|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_HTTP_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# A report may describe a roster only when the configuration itself carries
# the evidence.  Keep these checks deliberately narrow: a prose mention of a
# phase or an arbitrary URL must not promote the current registry to an
# expanded roster.  The source path is local evidence, not a network target;
# reporting never opens or fetches it.
_PHASE0_ROSTER_PATH_RE = re.compile(
    r"(?:^|[/\\])data[/\\]zoofan_phase0_zoo_list_v0_1\.md$",
    re.IGNORECASE,
)
_PHASE0_ROSTER_VERSION_RE = re.compile(r"\bphase\s*[-_ ]?0\s*[-_/ ]?v?0[._]1\b", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _clean_text(value: Any, default: str = "") -> str:
    return _WHITESPACE_RE.sub(" ", _text(value, default)).strip()


def _int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return _text(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}


def _iso(value: Any) -> Optional[str]:
    """Return a stable ISO string for report fields without raising."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        raw = _text(value).strip()
        if not raw:
            return None
        parsed_value = _parse_datetime(raw)
        if parsed_value is None:
            # Keep legacy/non-ISO evidence visible, but trim only whitespace.
            return raw
        parsed = parsed_value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    parsed: Optional[datetime] = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        raw = _text(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            # Some legacy rows contain a date without a time or a common UTC
            # rendering.  Do not attempt broad locale parsing.
            for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S"):
                try:
                    parsed = datetime.strptime(raw, pattern)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _discovery_bounds(discoveries: Sequence[Mapping[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Return first/last discovery times as UTC ISO strings."""

    first_values = [
        _first(row, "discovered_at")
        for row in discoveries
        if _first(row, "discovered_at") not in (None, "")
    ]
    last_values = [
        value
        for row in discoveries
        for value in (_first(row, "discovered_at"), _first(row, "last_discovered_at", "discovered_at"))
        if value not in (None, "")
    ]

    def _bound(values: Sequence[Any], *, latest: bool) -> Optional[str]:
        if not values:
            return None
        parsed = [(value, _parse_datetime(value)) for value in values]
        valid = [(value, stamp) for value, stamp in parsed if stamp is not None]
        if valid:
            selected = (max if latest else min)(valid, key=lambda item: item[1])
            return _iso(selected[1])
        # Preserve opaque legacy evidence when no honest chronology exists.
        return (max if latest else min)(_clean_text(value) for value in values)

    return _bound(first_values, latest=False), _bound(last_values, latest=True)


def _normalized_url(value: Any) -> str:
    try:
        return normalize_url(_text(value))
    except Exception:
        return _clean_text(value)


def _url_parts(value: Any) -> Optional[Any]:
    raw = _clean_text(value)
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not parsed.hostname:
            return None
        # Accessing port catches malformed values such as ``example:bad``.
        _ = parsed.port
        return parsed
    except (TypeError, ValueError):
        return None


def _safe_http_url(value: Any) -> Optional[str]:
    """Return an absolute HTTP(S) URL suitable for an HTML attribute.

    Report projections retain canonical URL evidence as text, but links and
    media attributes must never receive protocol-relative, javascript:, data:,
    or whitespace/control-character values.  Keep this check independent from
    ``normalize_url``: normalization is an identity operation and is not an
    HTML-safety policy.
    """

    raw = _redact_url_sensitive(value)
    if not raw or not re.match(r"^https?://", raw, re.IGNORECASE):
        return None
    if any(ord(character) <= 0x20 or character in '<>\"\'' for character in raw):
        return None
    parsed = _url_parts(raw)
    if parsed is None or parsed.scheme.lower() not in {"http", "https"}:
        return None
    return parsed.geturl()


def _redact_url_userinfo(value: Any) -> str:
    """Remove URL userinfo before any value can reach a report/UI field."""

    raw = _text(value).strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return raw
    if not parsed.netloc or "@" not in parsed.netloc:
        return raw
    # Keep the host/port and all non-credential evidence while dropping both
    # username and password.  ``rsplit`` also handles an @ in a password.
    host_port = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, host_port, parsed.path, parsed.query, parsed.fragment))


def _redact_url_sensitive(value: Any) -> str:
    """Redact common credential query parameters while retaining URL evidence.

    Raw discovery/source URL fields are useful audit evidence, so ordinary
    campaign parameters such as ``utm_source`` remain intact.  Credential
    names are replaced in-place with a stable marker; userinfo is removed
    first so credentials cannot leak through either URL representation.
    """

    raw = _redact_url_userinfo(value)
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return raw
    if not parsed.query:
        return raw
    redacted_parts: List[str] = []
    for component in parsed.query.split("&"):
        if not component:
            redacted_parts.append(component)
            continue
        key, separator, _value = component.partition("=")
        try:
            key_for_match = unquote_plus(key)
        except (TypeError, ValueError):
            key_for_match = key
        if _SENSITIVE_QUERY_KEY_RE.search(key_for_match):
            redacted_parts.append(key + (separator or "=") + "REDACTED")
        else:
            redacted_parts.append(component)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(redacted_parts), parsed.fragment))


def _redact_text_urls(value: Any) -> str:
    """Redact URL credentials/query secrets embedded in error text."""

    text = _clean_text(value)
    if not text:
        return ""
    return _HTTP_URL_IN_TEXT_RE.sub(lambda match: _redact_url_sensitive(match.group(0)), text)


def _plain_body_summary(value: Any, limit: int = 320) -> Optional[str]:
    """Create a compact text-only body summary without retaining HTML."""

    text = _text(value)
    if not text.strip():
        return None
    # Parsed article content is normally plain text, but legacy databases may
    # contain markup.  Remove executable/style blocks and tags before exposing
    # a short preview in the report.  The original raw response is never read
    # for this field.
    text = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(_clean_text(text))
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _host(value: Any) -> str:
    parsed = _url_parts(value)
    if parsed is None:
        raw = _clean_text(value).lower().rstrip(".")
        if "://" in raw:
            raw = raw.split("://", 1)[1].split("/", 1)[0]
        return raw
    # Keep ``www.example`` distinct from ``example``.  Runtime URLPolicy uses
    # exact host equality and only explicit aliases authorize another host.
    return _text(parsed.hostname).lower().rstrip(".")


def _safe_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list, str, int, float, bool)):
        candidate = value
    else:
        candidate = _text(value)
    try:
        parsed = json.loads(candidate) if isinstance(candidate, str) else candidate
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _json_object(value: Any) -> Dict[str, Any]:
    parsed = _safe_json(value, {})
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _json_list(value: Any) -> List[Any]:
    parsed = _safe_json(value, [])
    return list(parsed) if isinstance(parsed, list) else []


def _first(row: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _row_key(row: Mapping[str, Any], index: int, prefix: str) -> str:
    value = _first(row, "id", f"{prefix}_id", "slug", "url", "canonical_url")
    return _clean_text(value) or f"{prefix}-{index + 1}"


def _groups(value: Any) -> List[str]:
    """Extract only the explicitly supported group fields.

    Arbitrary zoo metadata is deliberately never copied into a projection.
    """

    raw: Any = value
    if isinstance(value, Mapping):
        raw = value.get("groups", value.get("group", []))
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple, set)):
        raw = [] if raw in (None, "") else [raw]
    result: List[str] = []
    for item in raw:
        name = _clean_text(item)
        if name and name not in result:
            result.append(name[:80])
    return sorted(result, key=lambda item: item.casefold()) or ["Ungrouped"]


def _provenance_value(value: Any) -> Any:
    """Keep explicit roster provenance while dropping arbitrary metadata."""

    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            name = _clean_text(key)
            if not name:
                continue
            if isinstance(item, (str, int, float, bool)) or item is None:
                result[name[:80]] = _redact_text_urls(item) if isinstance(item, str) else item
            elif isinstance(item, (list, tuple)):
                result[name[:80]] = [_provenance_value(part) for part in item[:20]]
        return result
    if isinstance(value, (list, tuple, set)):
        return [_provenance_value(item) for item in list(value)[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _redact_text_urls(value) if isinstance(value, str) else value
    return _clean_text(value)


def _provenance_entries(value: Any) -> List[Any]:
    """Return the simple provenance entries accepted by the config model.

    ``list_provenance`` predates the expanded-roster contract and is allowed
    to be a string, mapping, or list of either.  Keep this adapter permissive
    for report generation, while the authority check below remains strict.
    """

    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _provenance_field(value: Any, *names: str) -> Any:
    """Read a named provenance field case-insensitively and safely."""

    if not isinstance(value, Mapping):
        return None
    wanted = {name.casefold().replace("-", "_") for name in names}
    for key, candidate in value.items():
        normalized = _clean_text(key).casefold().replace("-", "_")
        if normalized in wanted:
            return candidate
    return None


def _provenance_text(value: Any) -> str:
    """Flatten scalar provenance text for version/path evidence checks."""

    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, Mapping):
        return " ".join(
            _provenance_text(item)
            for item in value.values()
            if _provenance_text(item)
        )
    if isinstance(value, (list, tuple, set)):
        return " ".join(
            _provenance_text(item)
            for item in value
            if _provenance_text(item)
        )
    return _clean_text(value)


def _provenance_source(value: Any) -> Optional[str]:
    """Extract a local source path from a provenance entry."""

    if isinstance(value, str):
        text = _clean_text(value)
        return text or None
    if not isinstance(value, Mapping):
        return None
    source = _provenance_field(
        value,
        "source",
        "source_path",
        "path",
        "file",
        "filename",
        "artifact",
        "document",
        "roster_path",
        "url",
    )
    if isinstance(source, Mapping):
        return _provenance_source(source)
    if isinstance(source, (list, tuple)):
        for item in source:
            candidate = _provenance_source(item)
            if candidate:
                return candidate
    text = _provenance_text(source)
    return text or None


def _provenance_version(value: Any) -> Optional[str]:
    """Extract an explicit roster version, if one was supplied."""

    candidate = _provenance_field(
        value,
        "version",
        "roster_version",
        "list_version",
        "release",
        "revision",
    )
    text = _provenance_text(candidate)
    if text:
        return text
    # A string provenance entry can contain the version beside its path.
    if isinstance(value, str):
        match = _PHASE0_ROSTER_VERSION_RE.search(value)
        return _clean_text(match.group(0)) if match else None
    return None


def _provenance_sha256(value: Any) -> Optional[str]:
    """Return a canonical SHA-256 evidence value, rejecting weak hashes."""

    candidate = _provenance_field(
        value,
        "sha256",
        "sha_256",
        "sha",
        "hash",
        "digest",
        "checksum",
        "roster_sha256",
    )
    text = _provenance_text(candidate).lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :].strip()
    if text.startswith("sha256="):
        text = text[len("sha256=") :].strip()
    return text if _SHA256_RE.fullmatch(text) else None


def _provenance_count(value: Any) -> Optional[int]:
    """Read an optional declared expanded-roster count from provenance."""

    candidate = _provenance_field(
        value,
        "expanded_count",
        "expanded_zoo_count",
        "expanded_total",
        "roster_count",
        "zoo_count",
        "total_zoos",
        "member_count",
        "membership_count",
        "total_members",
        "total",
        "count",
    )
    if isinstance(candidate, bool):
        return None
    if isinstance(candidate, int):
        return candidate if candidate > 0 else None
    if isinstance(candidate, float) and candidate.is_integer():
        return int(candidate) if candidate > 0 else None
    text = _clean_text(candidate)
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed > 0 else None
    return None


def _zoo_memberships(zoo: Zoo) -> List[str]:
    """Return explicit configured group memberships for authority checks."""

    raw = getattr(zoo, "groups", ())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return []
    result: List[str] = []
    for item in raw:
        membership = _clean_text(item)
        if membership and membership.casefold() not in {"ungrouped", "unknown"} and membership not in result:
            result.append(membership)
    return result


def _membership_evidence(zoo: Zoo) -> Tuple[List[str], bool]:
    """Return memberships and whether the raw group evidence is complete."""

    raw = getattr(zoo, "groups", ())
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set)):
        return [], False
    values = [_clean_text(item) for item in raw]
    values = [item for item in values if item]
    memberships = _zoo_memberships(zoo)
    if not values or not memberships:
        return memberships, False
    normalized = [item.casefold() for item in values]
    complete = (
        len(normalized) == len(set(normalized))
        and all(item not in {"ungrouped", "unknown"} for item in normalized)
    )
    return memberships, complete


def _phase0_provenance_record(
    value: Any,
) -> Optional[Tuple[str, str, str, Optional[int]]]:
    """Return one valid Phase0 provenance record, without reading its file."""

    entries = _provenance_entries(value)
    if len(entries) != 1:
        return None
    entry = entries[0]
    source = _provenance_source(entry)
    version = _provenance_version(entry)
    digest = _provenance_sha256(entry)
    entry_text = _provenance_text(entry)
    source_match = bool(
        source
        and "://" not in source
        and _PHASE0_ROSTER_PATH_RE.search(source.replace("\\", "/"))
    )
    version_match = bool(version and _PHASE0_ROSTER_VERSION_RE.search(version))
    if source_match and not version:
        # The version is encoded by the v0_1 filename, but retain a stable
        # display value instead of inventing a roster total.
        version = "Phase0 v0.1"
        version_match = True
    elif source_match and not version_match:
        # Some config producers keep the phase marker in ``scope`` and only
        # put ``v0.1`` in ``version``.  The source filename still anchors the
        # identity, so normalize that equivalent spelling.
        if (
            re.search(r"phase\s*[-_ ]?0", (source or "") + " " + entry_text, re.IGNORECASE)
            and re.search(r"\bv?0[._]1\b", entry_text, re.IGNORECASE)
        ):
            version = "Phase0 v0.1"
            version_match = True
    if not source_match or not version_match or not digest:
        return None
    return source or "", version or "", digest, _provenance_count(entry)


def _config_only_source_not_found(
    zoo: Zoo,
    source_records: Sequence[Mapping[str, Any]],
) -> bool:
    """Recognize an explicit Phase0 no-site/no-source configuration gap."""

    if source_records or _clean_text(getattr(zoo, "website_url", None)):
        return False
    status = _clean_text(getattr(zoo, "source_status", None)).casefold()
    if status not in {"not_found", "source_not_found"}:
        return False
    record = _phase0_provenance_record(getattr(zoo, "list_provenance", []))
    if record is None or record[3] is None:
        return False
    memberships, complete = _membership_evidence(zoo)
    return bool(
        complete
        and memberships
        and _clean_text(getattr(zoo, "id", None))
        and _clean_text(getattr(zoo, "slug", None))
        and _clean_text(getattr(zoo, "name", None))
    )


def _derive_roster_scope(bundle: "_ConfigBundle", enabled_zoos: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Derive the report scope from explicit, local configuration evidence.

    The expanded roster is authoritative only when every configured zoo has a
    unique identity, complete explicit group membership, the same Phase0 v0.1
    provenance record, and a declared count equal to the configured member
    count.  This rejects partial, duplicate, and mixed registries.  No network
    or source-file access occurs.
    """

    configured_count = len(bundle.zoos)
    enabled_count = len(enabled_zoos)
    roster_provenance: Dict[str, Any] = {}
    records: List[Tuple[str, str, str, str, Optional[int]]] = []
    invalid_evidence = False
    structural_invalid = False
    phase0_hint = False
    membership_count = 0
    group_membership_count = 0
    seen_keys: Set[str] = set()
    seen_names: Set[str] = set()

    for zoo in bundle.zoos:
        zoo_key = _config_zoo_key(zoo)
        provenance = getattr(zoo, "list_provenance", [])
        roster_provenance[zoo_key] = _provenance_value(provenance)
        memberships, groups_complete = _membership_evidence(zoo)
        group_membership_count += len(memberships)
        name_key = _clean_text(getattr(zoo, "name", "")).casefold()
        if (
            not _clean_text(getattr(zoo, "id", ""))
            or not _clean_text(getattr(zoo, "slug", ""))
            or not name_key
            or zoo_key in seen_keys
            or name_key in seen_names
        ):
            structural_invalid = True
        else:
            seen_keys.add(zoo_key)
            seen_names.add(name_key)
        if not groups_complete:
            structural_invalid = True
        elif zoo_key not in seen_keys:
            structural_invalid = True
        else:
            membership_count += 1
        entries = _provenance_entries(provenance)
        provenance_text = _provenance_text(provenance)
        phase0_hint = phase0_hint or bool(
            _PHASE0_ROSTER_PATH_RE.search(provenance_text.replace("\\", "/"))
            or re.search(r"phase\s*[-_ ]?0", provenance_text, re.IGNORECASE)
        )
        # A single, unambiguous provenance record is required.  In
        # particular, do not silently select a Phase0 item from a mixed list
        # containing an older registry or an untrusted citation.
        if len(entries) != 1 or not memberships:
            if phase0_hint:
                invalid_evidence = True
            continue
        record = _phase0_provenance_record(provenance)
        if record is None:
            invalid_evidence = True
            continue
        source, version, digest, declared_count = record
        records.append((source, version, digest, zoo_key, declared_count))

    if phase0_hint and structural_invalid:
        invalid_evidence = True
    identity_values = {(source, version, digest) for source, version, digest, _, _ in records}
    declared_counts = {count for _, _, _, _, count in records if count is not None}
    all_records_have_count = bool(records) and all(
        count is not None for _, _, _, _, count in records
    )
    if (records or phase0_hint) and (len(identity_values) != 1 or len(records) != configured_count):
        invalid_evidence = True
    if records and (not all_records_have_count or len(declared_counts) != 1):
        invalid_evidence = True
    declared_count = next(iter(declared_counts), None) if all_records_have_count and len(declared_counts) == 1 else None
    expanded_count = declared_count
    if (records or phase0_hint) and (
        expanded_count is None
        or expanded_count != configured_count
        or expanded_count != membership_count
    ):
        invalid_evidence = True
    available = bool(
        configured_count
        and records
        and not invalid_evidence
        and len(seen_keys) == configured_count
        and membership_count == configured_count
    )
    scope_source: Optional[str] = None
    scope_version: Optional[str] = None
    scope_digest: Optional[str] = None
    if available:
        scope_source, scope_version, scope_digest = next(iter(identity_values))
        scope_kind = "authoritative_expanded_roster"
        scope_label = f"Authoritative {scope_version} roster"
        scope_status = "available"
        limitation = None
        provenance_evidence: Dict[str, Any] = {
            "source": scope_source,
            "version": scope_version,
            "sha256": scope_digest,
        }
    else:
        scope_kind = "configured_registry"
        scope_label = "Configured registry only"
        scope_status = "invalid" if invalid_evidence or phase0_hint else "not_provided"
        limitation = (
            "Acceptance scope is limited to the explicitly configured registry; no authoritative "
            "expanded zoo roster or group taxonomy is included."
        )
        provenance_evidence = {}

    return {
        "kind": scope_kind,
        "label": scope_label,
        "configured_zoos": configured_count,
        "configured_zoo_count": configured_count,
        "configured_count": configured_count,
        "enabled_zoos": enabled_count,
        "enabled_zoo_count": enabled_count,
        "expanded_roster_available": available,
        "expanded_roster_status": scope_status,
        "authoritative_roster_available": available,
        "expanded_zoos": expanded_count if available else None,
        "expanded_zoo_count": expanded_count if available else None,
        "expanded_roster_count": expanded_count if available else None,
        "expanded_count": expanded_count if available else None,
        "configured_membership_count": membership_count,
        "configured_group_membership_count": group_membership_count,
        "expanded_membership_count": membership_count if available else None,
        "roster_source": scope_source,
        "roster_path": scope_source,
        "roster_version": scope_version,
        "roster_sha256": scope_digest,
        "roster_sha": scope_digest,
        "roster_provenance": roster_provenance,
        "authoritative_roster": provenance_evidence,
        "authoritative_roster_provenance": provenance_evidence,
        "limitation": limitation,
    }


@dataclass
class _ConfigBundle:
    zoos: List[Zoo]
    sources: List[Source]
    groups: Dict[str, List[str]]
    path: str
    error: Optional[str] = None


def _config_from_mapping(value: Mapping[str, Any], path: str) -> _ConfigBundle:
    """Small in-memory config adapter for callers/tests that already have YAML."""

    zoos: List[Zoo] = []
    sources: List[Source] = []
    groups: Dict[str, List[str]] = {}
    raw_zoos = value.get("zoos", [])
    if isinstance(raw_zoos, Mapping):
        raw_zoos = [dict(item, id=key) if isinstance(item, Mapping) else {"id": key} for key, item in raw_zoos.items()]
    if not isinstance(raw_zoos, list):
        raw_zoos = []
    for index, raw in enumerate(raw_zoos):
        item = dict(raw) if isinstance(raw, Mapping) else {}
        zoo_id = _clean_text(item.get("id") or item.get("slug") or item.get("name") or f"zoo-{index + 1}")
        zoo_kwargs: Dict[str, Any] = {
            "id": zoo_id,
            "slug": _clean_text(item.get("slug") or zoo_id),
            "name": _clean_text(item.get("name") or item.get("slug") or zoo_id),
            "website_url": item.get("website_url") or item.get("url") or item.get("base_url"),
            "country_code": _clean_text(item.get("country_code")) or None,
            "language": _clean_text(item.get("language")) or None,
            "enabled": _bool(item.get("enabled"), True),
            "metadata": dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
        }
        optional_zoo_fields = {
            "groups": tuple(item.get("groups", ())) if isinstance(item.get("groups", ()), (list, tuple)) else (),
            "region": _clean_text(item.get("region")) or None,
            "city": _clean_text(item.get("city")) or None,
            "source_status": _clean_text(item.get("source_status")) or None,
            "list_provenance": list(item.get("list_provenance", [])) if isinstance(item.get("list_provenance", []), list) else [],
        }
        supported_zoo_fields = {field.name for field in dataclass_fields(Zoo)}
        zoo_kwargs.update({key: value for key, value in optional_zoo_fields.items() if key in supported_zoo_fields})
        zoo = Zoo(
            **zoo_kwargs,
        )
        zoos.append(zoo)
        groups[zoo_id] = _groups(item.get("groups", item.get("group", zoo.metadata)))
        raw_sources = item.get("sources", [])
        if isinstance(raw_sources, Mapping):
            raw_sources = [dict(source, id=key) if isinstance(source, Mapping) else {"id": key, "url": source} for key, source in raw_sources.items()]
        if not isinstance(raw_sources, list):
            raw_sources = []
        for source_index, source_raw in enumerate(raw_sources):
            source_item = dict(source_raw) if isinstance(source_raw, Mapping) else {}
            source_id = _clean_text(source_item.get("id") or source_item.get("name") or source_item.get("url") or f"{zoo_id}-source-{source_index + 1}")
            source_config = dict(source_item.get("config", {})) if isinstance(source_item.get("config"), Mapping) else {}
            for key in (
                "official_host",
                "navigation_patterns",
                "allow_regex",
                "exclude_regex",
                "article_allow",
                "article_exclude",
                "adapter",
                "custom_adapter",
                "adapter_name",
                "source_status",
            ):
                if key in source_item:
                    source_config[key] = source_item[key]
            configured_adapter = source_item.get(
                "adapter",
                source_item.get("custom_adapter", source_item.get("adapter_name")),
            )
            if canonical_adapter_kind(configured_adapter) == "custom_adapter":
                configured_adapter = source_item.get(
                    "custom_adapter",
                    source_item.get("adapter_name", configured_adapter),
                )
            raw_kind = source_item.get("kind") or source_item.get("type") or source_item.get("source_type")
            if raw_kind is None:
                raw_kind = canonical_adapter_kind(configured_adapter) or ("custom_adapter" if configured_adapter else "rss")
            source_kind = canonical_adapter_kind(raw_kind) or _clean_text(raw_kind) or "rss"
            sources.append(
                Source(
                    id=source_id,
                    zoo_id=zoo_id,
                    url=_text(source_item.get("url")),
                    kind=source_kind,
                    name=_clean_text(source_item.get("name")) or None,
                    language=_clean_text(source_item.get("language")) or zoo.language,
                    config=source_config,
                    enabled=_bool(source_item.get("enabled"), True),
                    adapter=_clean_text(configured_adapter) or None,
                    source_status=_clean_text(source_item.get("source_status", source_item.get("status"))) or None,
                )
            )
    return _ConfigBundle(zoos=zoos, sources=sources, groups=groups, path=path)


def _load_config_bundle(config: Any) -> _ConfigBundle:
    if isinstance(config, _ConfigBundle):
        return config
    if isinstance(config, CrawlerConfig):
        groups = {
            _clean_text(zoo.id or zoo.slug or zoo.name): _groups(
                getattr(zoo, "groups", ()) or getattr(zoo, "metadata", {})
            )
            for zoo in config.zoos
        }
        return _ConfigBundle(config.zoos, config.sources, groups, "<config>")
    if isinstance(config, Mapping):
        return _config_from_mapping(config, "<mapping>")
    if config is None:
        config = Path(__file__).resolve().parents[1] / "config" / "zoos.yaml"
    path = Path(config)
    try:
        loaded = load_config(path)
        groups = {
            _clean_text(zoo.id or zoo.slug or zoo.name): _groups(
                getattr(zoo, "groups", ()) or getattr(zoo, "metadata", {})
            )
            for zoo in loaded.zoos
        }
        return _ConfigBundle(loaded.zoos, loaded.sources, groups, str(path))
    except Exception as exc:
        # Reporting should remain useful for a registry written against an
        # older validator.  Parse the raw YAML as a best-effort fallback and
        # retain the validation error as a visible known issue.
        try:
            # Import dynamically so reporting remains usable when the optional
            # YAML dependency has no installed type stubs.
            yaml_module = __import__("yaml")
            raw = yaml_module.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, Mapping):
                fallback = _config_from_mapping(raw, str(path))
                fallback.error = f"{type(exc).__name__}: {exc}"
                return fallback
        except Exception:
            pass
        return _ConfigBundle([], [], {}, str(path), f"{type(exc).__name__}: {exc}")


@dataclass
class _DBSnapshot:
    connection: Optional[sqlite3.Connection]
    owned: bool
    available: bool
    error: Optional[str]
    tables: Dict[str, List[Dict[str, Any]]]
    columns: Dict[str, Set[str]]
    user_version: int = 0


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_rows(connection: sqlite3.Connection, table: str) -> Tuple[List[Dict[str, Any]], Set[str]]:
    try:
        info = connection.execute("PRAGMA table_info(" + _quote_identifier(table) + ")").fetchall()
        columns = {_text(row[1]) for row in info}
        if not columns:
            return [], set()
        rows = connection.execute("SELECT * FROM " + _quote_identifier(table)).fetchall()
        result: List[Dict[str, Any]] = []
        for row in rows:
            if isinstance(row, sqlite3.Row):
                result.append({key: row[key] for key in row.keys()})
            else:
                result.append({column: row[index] for index, column in enumerate(columns)})
        return result, columns
    except (sqlite3.Error, TypeError, ValueError):
        return [], set()


def _open_snapshot(db: Any) -> _DBSnapshot:
    if isinstance(db, _DBSnapshot):
        return db
    connection: Optional[sqlite3.Connection] = None
    owned = False
    error: Optional[str] = None
    if isinstance(db, sqlite3.Connection):
        connection = db
    elif db is not None and hasattr(db, "connection"):
        try:
            candidate = getattr(db, "connection")
            if isinstance(candidate, sqlite3.Connection):
                connection = candidate
        except Exception:
            connection = None
    else:
        raw_path = ":memory:" if db is None else os.fspath(db)
        if raw_path == ":memory:":
            # A fresh memory database is useful as an explicitly empty view;
            # it must not be mistaken for a writable storage object.
            connection = sqlite3.connect(":memory:")
            owned = True
        else:
            path = Path(raw_path).expanduser()
            if path.is_file():
                try:
                    uri = "file:" + quote(str(path.resolve()), safe="/:") + "?mode=ro"
                    connection = sqlite3.connect(uri, uri=True)
                    owned = True
                except (sqlite3.Error, OSError) as exc:
                    error = f"{type(exc).__name__}: {exc}"
            else:
                error = "database file is missing"
    if connection is None:
        return _DBSnapshot(None, False, False, error, {name: [] for name in _TABLE_ALIASES}, {name: set() for name in _TABLE_ALIASES})
    try:
        connection.row_factory = sqlite3.Row
        tables: Dict[str, List[Dict[str, Any]]] = {}
        columns: Dict[str, Set[str]] = {}
        for logical, aliases in _TABLE_ALIASES.items():
            rows: List[Dict[str, Any]] = []
            found_columns: Set[str] = set()
            for table in aliases:
                candidate_rows, candidate_columns = _table_rows(connection, table)
                if candidate_columns:
                    rows, found_columns = candidate_rows, candidate_columns
                    break
            tables[logical] = rows
            columns[logical] = found_columns
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
            version = _int(row[0] if row else 0)
        except sqlite3.Error:
            version = 0
        return _DBSnapshot(connection, owned, True, None, tables, columns, version)
    except Exception as exc:
        if owned:
            try:
                connection.close()
            except Exception:
                pass
        return _DBSnapshot(None, False, False, f"{type(exc).__name__}: {exc}", {name: [] for name in _TABLE_ALIASES}, {name: set() for name in _TABLE_ALIASES})


def _close_snapshot(snapshot: _DBSnapshot) -> None:
    if snapshot.owned and snapshot.connection is not None:
        try:
            snapshot.connection.close()
        except Exception:
            pass


def _database_legacy(snapshot: _DBSnapshot) -> bool:
    expected: Dict[str, Set[str]] = {
        "zoos": {"id", "slug", "name"},
        "sources": {"id", "zoo_id", "url", "kind"},
        "articles": {"id", "title"},
        "discoveries": {"article_id", "source_id"},
        "runs": {"id", "status"},
        "stats": {"crawl_run_id", "source_id", "status"},
        "zoo_results": {"crawl_run_id", "zoo_id", "status"},
        "identities": {"article_id", "zoo_id"},
    }
    if not snapshot.available:
        return False
    # A readable SQLite file that lacks any core table is an incomplete/legacy
    # database, even when it has no rows.  This is intentionally stricter than
    # merely checking user_version: exported or hand-created databases often
    # omit schema_meta and PRAGMA user_version.
    core_tables = ("zoos", "sources", "articles", "runs", "stats", "zoo_results")
    if any(not snapshot.columns.get(logical) for logical in core_tables):
        return True
    for logical, required in expected.items():
        columns = snapshot.columns.get(logical, set())
        if columns and not required.issubset(columns):
            return True
    return snapshot.user_version < 4


def _git_identity(cwd: Optional[Path] = None) -> Dict[str, Any]:
    """Return commit plus a reproducible dirty-tree/code-state identity."""

    root = cwd or Path(__file__).resolve().parents[1]
    try:
        commit_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=4,
        )
        diff_result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=False,
            check=False,
            timeout=8,
        )
        status = status_result.stdout or ""
        digest = hashlib.sha256()
        digest.update(status.encode("utf-8", "replace"))
        digest.update(diff_result.stdout or b"")
        # ``git diff`` does not include untracked contents.  Include their
        # names and bytes so two dirty trees cannot share a claimed identity.
        for line in status.splitlines():
            if len(line) < 4 or not line.startswith("?? "):
                continue
            candidate = root / line[3:]
            try:
                digest.update(line[3:].encode("utf-8", "replace"))
                digest.update(candidate.read_bytes())
            except OSError:
                digest.update(b"<unreadable>")
        return {
            "commit": (commit_result.stdout or "").strip() or "unknown",
            "dirty": bool(status.strip()),
            "status": status.splitlines(),
            "tree_fingerprint": digest.hexdigest(),
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": "unknown", "dirty": None, "status": [], "tree_fingerprint": None}


def _git_commit(cwd: Optional[Path] = None) -> str:
    # Keep this small compatibility helper for integrations that imported it.
    return _text(_git_identity(cwd).get("commit"), "unknown")


def _now_value(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    return parsed or datetime.now(timezone.utc)


def _config_zoo_key(zoo: Zoo) -> str:
    return _clean_text(zoo.id or zoo.slug or zoo.name)


def _config_source_key(source: Source) -> str:
    return _clean_text(source.id or source.normalized_url or source.url or f"source-{id(source)}")


def _db_match_zoo(zoo: Zoo, db_rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    identifiers = {_clean_text(zoo.id), _clean_text(zoo.slug), _clean_text(zoo.name)} - {""}
    for row in db_rows:
        values = {_clean_text(_first(row, "id")), _clean_text(_first(row, "slug")), _clean_text(_first(row, "name"))} - {""}
        if identifiers & values:
            return row
    return None


def _db_source_match(source: Source, zoo: Zoo, db_rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    source_id = _clean_text(source.id)
    normalized = _normalized_url(source.url)
    zoo_ids = {_clean_text(zoo.id), _clean_text(zoo.slug)} - {""}
    candidates: List[Mapping[str, Any]] = []
    for row in db_rows:
        if source_id and _clean_text(_first(row, "id")) == source_id:
            return row
        row_zoo = _clean_text(_first(row, "zoo_id"))
        if zoo_ids and row_zoo and row_zoo not in zoo_ids:
            continue
        row_url = _normalized_url(_first(row, "normalized_url", "url", "source_url"))
        if normalized and row_url == normalized:
            candidates.append(row)
    if not candidates:
        return None
    return sorted(candidates, key=lambda row: (_parse_datetime(_first(row, "updated_at", "last_checked")) or datetime.min.replace(tzinfo=timezone.utc), _clean_text(_first(row, "id"))))[-1]


def _official_hosts(zoo: Zoo, sources: Sequence[Source]) -> Set[str]:
    hosts: Set[str] = set()
    website = _host(getattr(zoo, "official_url", None) or zoo.website_url)
    if website:
        hosts.add(website)
    metadata = getattr(zoo, "metadata", {})
    if isinstance(metadata, Mapping):
        explicit_values: List[Any] = []
        for key in ("official_host", "official_hosts", "official_host_aliases"):
            value = metadata.get(key)
            if isinstance(value, (list, tuple, set)):
                explicit_values.extend(value)
            elif value:
                explicit_values.append(value)
        for explicit in explicit_values:
            value = _text(explicit)
            hosts.add(_host("https://" + value.lstrip("/") if "://" not in value else value))
    for source in sources:
        config = getattr(source, "config", {})
        if isinstance(config, Mapping):
            source_explicit_values: List[Any] = []
            for key in ("official_host", "official_hosts", "official_host_aliases"):
                value = config.get(key)
                if isinstance(value, (list, tuple, set)):
                    source_explicit_values.extend(value)
                elif value:
                    source_explicit_values.append(value)
            for explicit_value in source_explicit_values:
                value = _text(explicit_value)
                hosts.add(_host("https://" + value if "://" not in value else value))
    return {host for host in hosts if host}


def _host_is_official(value: Any, hosts: Set[str]) -> bool:
    article_host = _host(value)
    if not article_host or not hosts:
        return True
    # Host ownership is intentionally exact.  A lookalike subdomain such as
    # ``evil.official.example`` is not covered by ``official.example``;
    # legitimate alternate hosts must be listed explicitly in configuration.
    return article_host in hosts


def _source_config_match(source: Source, key: str) -> Any:
    config = getattr(source, "config", {})
    return config.get(key) if isinstance(config, Mapping) else None


def _canonical_source_adapter(source: Source) -> str:
    """Return the registry taxonomy for a configured source.

    ``Source.adapter`` intentionally retains a custom registry name (for
    example ``fixture_adapter``), while ``Source.kind`` carries the
    canonical ``custom_adapter`` marker.  Reports need both values: the
    canonical kind drives support/completion checks and the implementation
    name remains useful operator evidence.  Unknown values stay visible as
    unknown instead of being silently relabelled ``unsupported``; only an
    explicit unsupported kind is classified as such.
    """

    kind = canonical_adapter_kind(getattr(source, "kind", None))
    if kind:
        return kind
    configured = getattr(source, "adapter", None) or _source_config_match(source, "adapter")
    configured_kind = canonical_adapter_kind(configured)
    if configured_kind:
        return configured_kind
    # A source with a non-taxonomy adapter token and no explicit kind is the
    # data-only custom-adapter form accepted by config.py.
    if configured and _clean_text(getattr(source, "kind", None)) in {"", "unknown"}:
        return "custom_adapter"
    return normalize_adapter_token(getattr(source, "kind", None)) or "unknown"


def _source_adapter(source: Source) -> str:
    """Return the display adapter name, preserving injected custom names."""

    configured = getattr(source, "adapter", None) or _source_config_match(source, "adapter")
    kind = _canonical_source_adapter(source)
    if kind == "custom_adapter":
        custom_name = configured
        if not custom_name or canonical_adapter_kind(custom_name) == "custom_adapter":
            custom_name = (
                _source_config_match(source, "custom_adapter")
                or _source_config_match(source, "adapter_name")
            )
        return _clean_text(custom_name) or kind
    canonical = canonical_adapter_kind(configured)
    return _clean_text(canonical or configured or kind) or "unknown"


def _source_navigation_match(source: Optional[Source], article_url: str, title: str, content: str) -> bool:
    # URL identity and navigation rejection belong to URLPolicy.  Do not feed
    # the canonical URL into this content-quality classifier: legitimate news
    # paths frequently contain ``about``, ``news``, ``contact`` or similar
    # navigation words (for example ``/en/about-zoo/news/<article>``).
    del article_url
    title_value = _clean_text(title)
    if title_value and _NAVIGATION_TITLE_RE.fullmatch(title_value):
        return True

    # Body text is used only to classify; it is never retained in the
    # projection.  A navigation contamination normally appears before the
    # article prose as a breadcrumb/site-chrome prefix.  Requiring the first
    # visible token to be a navigation marker avoids treating ordinary prose
    # such as "The route ..." or "News about ..." as contaminated while still
    # catching the known Homepage/Go back and ticket/opening-hours/share/
    # related/contact/route prefixes.
    body_prefix = _plain_body_summary(content, limit=500) or ""
    if body_prefix and _NAVIGATION_BODY_PREFIX_RE.match(body_prefix):
        return True

    # Source-specific patterns are retained as an explicit content/title
    # override, but deliberately do not inspect ``article_url``.  URLPolicy
    # already applies path/navigation identities before an article is stored;
    # repeating them here was the source of the Prague false positive.
    if source is not None:
        patterns = _source_config_match(source, "navigation_patterns")
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, (list, tuple)):
            patterns = []
        values = (title_value, body_prefix)
        for pattern in patterns:
            try:
                expression = _text(pattern).strip()
                if expression and any(re.search(expression, value, re.IGNORECASE) for value in values if value):
                    return True
            except (re.error, TypeError):
                continue
    return False


def _warning(code: str, detail: Optional[str] = None) -> Dict[str, str]:
    result = {
        "code": code,
        "label": QUALITY_WARNING_LABELS.get(code, code.replace("_", " ").title()),
        "severity": "warning",
    }
    if detail:
        result["detail"] = _redact_text_urls(detail)[:240]
    return result


def _warning_sort_key(item: Mapping[str, Any]) -> Tuple[str, str]:
    return (_text(item.get("code")), _text(item.get("detail")))


def _dedupe_warnings(items: Iterable[Mapping[str, Any]]) -> List[Dict[str, str]]:
    unique: Dict[Tuple[str, str], Dict[str, str]] = {}
    for item in items:
        code = _text(item.get("code"))
        detail = _text(item.get("detail"))
        if not code:
            continue
        unique[(code, detail)] = {key: _text(value) for key, value in item.items() if value is not None}
    return [unique[key] for key in sorted(unique)]


def _article_sort_key(article: Mapping[str, Any]) -> Tuple[int, str, str]:
    parsed = _parse_datetime(article.get("published_at")) or _parse_datetime(article.get("last_fetched_at"))
    # Negating a timestamp keeps newest first without relying on mixed string
    # representations from legacy rows.
    stamp = int(parsed.timestamp()) if parsed else -1
    return (-stamp, _text(article.get("title")).casefold(), _text(article.get("id")))


def _failure_add(
    failures: List[str],
    value: Any,
    category_counts: Optional[Counter[str]] = None,
    category: Optional[str] = None,
    *,
    http_status: Any = None,
) -> None:
    message = _redact_text_urls(value)
    if message and message not in failures:
        # An explicit classification always wins.  When no classification was
        # persisted, an HTTP response status is still structured evidence and
        # can safely identify an external request boundary.  Unknown text is
        # intentionally left to ``_failure_class`` and therefore remains
        # blocking by default.
        failure_class = _failure_class(category or message)
        if category is None:
            failure_class = _http_failure_class(http_status, default=failure_class)
        failures.append(message[:400])
        if category_counts is not None:
            category_counts[failure_class] += 1


def _failure_class(value: Any, default: str = "failed") -> str:
    """Return a stable failure class without exposing arbitrary exception text."""

    normalized = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        return default
    for raw, stable in _FAILURE_CLASS_ALIASES.items():
        # Match aliases on token boundaries.  A raw substring such as
        # ``config`` must not classify ordinary evidence like
        # ``no enabled sources configured`` as a configuration failure.
        if re.search(rf"(?<![a-z0-9]){re.escape(raw)}(?![a-z0-9])", normalized):
            return stable
    for stable in (
        "source_not_found",
        "robots_disallowed",
        "javascript_required",
        "configuration_error",
        "unsupported",
        "parse_error",
        "blocked",
        "timeout",
        "dns_error",
        "tls_error",
        "http_error",
        "connection_error",
        "source_unavailable",
        "official_source_unavailable",
        "official_site_unavailable",
        "partial",
    ):
        if stable in normalized:
            return stable
    if normalized in {"error", "failed", "failure", "partial", "warning"}:
        return "failed" if normalized != "partial" else "partial"
    if "timed_out" in normalized or "timeout" in normalized:
        return "timeout"
    if "status_4" in normalized or "status_5" in normalized or "http" in normalized:
        return "http_error"
    return default


def _http_status(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _http_failure_class(value: Any, *, default: str = "failed") -> str:
    """Classify an HTTP status only when one was explicitly persisted."""

    status = _http_status(value)
    if status is None or status < 400 or status > 599:
        return default
    if status == 404:
        return "source_not_found"
    if status in {401, 403, 407, 451}:
        return "blocked"
    return "http_error"


def _specific_failure_category(value: Any) -> Optional[str]:
    """Return a non-summary category, keeping generic statuses as fallback."""

    normalized = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
    if not normalized or normalized in {
        "success",
        "completed",
        "ok",
        "healthy",
        "success_no_items",
        "partial",
        "warning",
        "completed_with_errors",
        "failed",
        "failure",
        "error",
        "running",
        "pending",
        "discovered",
    }:
        return None
    return normalized


def _evidence_flags(
    categories: Sequence[Any],
    *,
    status: Any = None,
    http_status: Any = None,
    messages: Sequence[Any] = (),
) -> Tuple[bool, bool]:
    """Return ``(external_limited, blocking)`` from structured evidence.

    Explicit parser/config/storage/runtime categories take precedence over a
    coincident HTTP status.  A generic failed/error status is only external
    when the persisted HTTP status identifies the request boundary.  Unknown
    explicit categories are conservative and remain blocking.
    """

    observed: List[str] = []
    explicit: List[str] = []
    for value in categories:
        normalized = _clean_text(value).lower().replace("-", "_").replace(" ", "_")
        if not normalized:
            continue
        classification = _failure_class(normalized)
        if normalized not in {
            "success",
            "completed",
            "ok",
            "healthy",
            "success_no_items",
            "partial",
            "warning",
            "completed_with_errors",
            "failed",
            "failure",
            "error",
            "running",
            "pending",
            "discovered",
        }:
            explicit.append(classification)
        observed.append(classification)
    for value in messages:
        if not _clean_text(value):
            continue
        observed.append(_failure_class(value))
    status_category = _specific_failure_category(status)
    if status_category:
        observed.append(_failure_class(status_category))

    generic_status = _clean_text(status).lower().replace("-", "_").replace(" ", "_") in {
        "failed",
        "failure",
        "error",
        "partial",
        "warning",
        "completed_with_errors",
    }
    status_code_class = _http_failure_class(http_status, default="")
    http_external = bool(status_code_class and status_code_class in EXTERNAL_FAILURE_CLASSES)
    blocking = any(
        classification not in NONBLOCKING_FAILURE_CLASSES
        for classification in observed
        if classification not in {"success", "success_no_items"}
    )
    if http_external and not explicit and generic_status:
        # Generic ``failed``/``error`` is the summary emitted for many fetch
        # boundaries; the persisted response status supplies the reason.
        blocking = False
    external = any(classification in EXTERNAL_FAILURE_CLASSES for classification in observed)
    external = external or (http_external and not blocking)
    return external, blocking


def _status_for_zoo(
    zoo: Zoo,
    source_records: Sequence[Mapping[str, Any]],
    articles: Sequence[Mapping[str, Any]],
    run_status: str,
) -> str:
    if not zoo.enabled:
        return "disabled"
    if not source_records:
        return "no_data" if not articles else "ok"
    errors = any(_text(source.get("status")) in {"error", "failed", "failure", "partial", "warning", "completed_with_errors", "unsupported"} or _int(source.get("error_count")) > 0 for source in source_records)
    successes = any(source.get("success") is True or _text(source.get("status")) in {"completed", "ok", "success", "healthy"} for source in source_records)
    if errors and successes:
        return "partial"
    if errors:
        return "error"
    if successes:
        return "ok"
    if run_status in {"error", "failed", "failure", "partial", "completed_with_errors"}:
        return "error"
    return "unknown"


def _status_label(status: str) -> str:
    return {
        "ok": "OK",
        "partial": "Partial",
        "error": "Error",
        "disabled": "Disabled",
        "no_data": "No data",
        "missing_result": "Missing result",
        "duplicate_result": "Duplicate result",
        "unknown": "Unknown",
    }.get(status, status.replace("_", " ").title())


def _zoo_priority(record: Mapping[str, Any]) -> Tuple[int, int, str]:
    status = _clean_text(record.get("status"))
    if status in {"error", "failed", "missing_result", "duplicate_result", "unsupported"}:
        severity = 0
    elif status in {"partial", "unknown", "no_data"}:
        severity = 1
    else:
        severity = 2
    warning_count = _int(record.get("quality_warning_count", record.get("counts", {}).get("warnings")))
    # Warnings/errors remain visible at the top by default while retaining a
    # deterministic name tie-break for stable artifacts.
    return severity, -warning_count, _clean_text(record.get("name")).casefold()


def _completion_category(row: Mapping[str, Any]) -> str:
    """Map a persisted zoo result into one stable completion category."""

    values = [
        _clean_text(_first(row, "error_category")),
        _clean_text(_first(row, "source_status")),
        _clean_text(_first(row, "status")),
    ]
    normalized = [value.lower().replace("-", "_").replace(" ", "_") for value in values if value]
    if any("source_not_found" in value or "not_found" in value or "404" in value for value in normalized):
        return "source_not_found"
    if any("robots_disallowed" in value or value in {"robots", "robots_error", "robots_unavailable"} for value in normalized):
        return "robots_disallowed"
    if any("javascript_required" in value or value in {"javascript", "js_required"} for value in normalized):
        return "javascript_required"
    for category in ("unsupported", "parse_error", "blocked", "configuration_error"):
        if any(category in value or (category == "parse_error" and "parse" in value) for value in normalized):
            return category
    if any(value in {"success_no_items", "no_items"} for value in normalized):
        return "success_no_items"
    if any(value in {"partial", "warning", "completed_with_errors"} for value in normalized):
        return "partial"
    if any(value in {"failed", "failure", "error"} for value in normalized):
        return "failed"
    if any(value in {"success", "completed", "ok", "healthy"} for value in normalized):
        return "success"
    return "failed"


def _completion_classification(row_or_category: Any) -> str:
    """Map raw/internal result state to the required public label."""

    raw = (
        _completion_category(row_or_category)
        if isinstance(row_or_category, Mapping)
        else _clean_text(row_or_category).lower().replace("-", "_").replace(" ", "_")
    )
    return _PUBLIC_CLASSIFICATION_BY_CATEGORY.get(raw, "FAILED")


def _result_error_category(row: Mapping[str, Any]) -> Optional[str]:
    """Return only a real error category from a persisted zoo result.

    Older crawler versions copied ``source_status`` into ``error_category``
    for every result, including successful rows.  Keep the two concepts
    separate in reports and suppress success-like values even when they are
    still present in legacy rows.
    """

    raw = _clean_text(_first(row, "error_category"))
    if not raw:
        return None
    normalized = raw.lower().replace("-", "_").replace(" ", "_")
    if normalized in {
        "success",
        "success_no_items",
        "completed",
        "ok",
        "healthy",
        "running",
    }:
        return None
    return raw


def _source_record(
    source: Source,
    zoo: Zoo,
    db_row: Optional[Mapping[str, Any]],
    stat: Optional[Mapping[str, Any]],
    article_count: int,
    article_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    source_id = _config_source_key(source)
    db_row = db_row or {}
    stat = stat or {}
    errors = _json_list(_first(stat, "errors_json", "errors", default=[]))
    errors = [_redact_text_urls(item) for item in errors if _redact_text_urls(item)]
    error_categories = [
        _clean_text(item).lower().replace("-", "_").replace(" ", "_")
        for item in _json_list(_first(stat, "error_classifications", "error_categories", default=[]))
        if _clean_text(item)
    ]
    explicit_error_category = _clean_text(_first(stat, "error_category", "error_classification"))
    normalized_explicit_error_category = explicit_error_category.lower().replace("-", "_").replace(" ", "_")
    if normalized_explicit_error_category and normalized_explicit_error_category not in error_categories:
        error_categories.insert(0, normalized_explicit_error_category)
    last_error = _redact_text_urls(_first(db_row, "last_error", "error")) or _redact_text_urls(_first(stat, "error"))
    if last_error and last_error not in errors:
        errors.insert(0, last_error)
    adapter_kind = _canonical_source_adapter(source)
    configured_status = _clean_text(
        getattr(source, "source_status", None) or _source_config_match(source, "source_status")
    )
    fallback_status = "disabled" if not source.enabled else ("unsupported" if adapter_kind == "unsupported" else "not_registered")
    status = _clean_text(_first(stat, "status")) or _clean_text(_first(db_row, "status")) or configured_status or fallback_status
    http_status = _first(stat, "http_status", "last_http_status", default=_first(db_row, "last_http_status"))
    source_external_limited, _source_blocking = _evidence_flags(
        error_categories,
        status=status,
        http_status=http_status,
        messages=errors,
    )
    success_value = _first(db_row, "success")
    success: Optional[bool]
    if success_value is None:
        success = status in {"completed", "ok", "success", "healthy"}
        if not db_row and not stat:
            success = None
    else:
        success = _bool(success_value)
    adapter = _source_adapter(source)
    source_url = _redact_url_sensitive(source.url)
    return {
        "id": source_id,
        "name": _clean_text(source.name) or source_id,
        "url": source_url,
        "kind": adapter,
        "adapter": adapter,
        "adapter_kind": adapter_kind,
        "enabled": bool(source.enabled),
        "registered": bool(db_row),
        "status": status,
        "status_label": _status_label(status),
        "success": success,
        "http_status": http_status,
        "last_checked": _iso(_first(db_row, "last_checked")),
        "last_success": _iso(_first(db_row, "last_success")),
        "last_error": last_error or None,
        "error": last_error or None,
        "errors": errors,
        "error_categories": list(dict.fromkeys(error_categories)),
        "external_limited": source_external_limited,
        "error_count": _int(_first(stat, "error_count"), len(errors)),
        "discovered_count": _int(_first(stat, "discovered_count")),
        "fetched_count": _int(_first(stat, "fetched_count")),
        "stored_count": _int(_first(stat, "stored_count")),
        "already_known_count": _int(_first(stat, "already_known_count")),
        "duplicate_candidate_count": _int(_first(stat, "duplicate_candidate_count")),
        "parsed_count": _first(stat, "parsed_count", "parsed"),
        "inserted_count": _first(stat, "inserted_count", "inserted"),
        "failed_count": _first(stat, "failed_count", "failed"),
        "duration_ms": _int(_first(stat, "duration_ms")),
        "article_count": article_count,
        # ``article_count`` remains a compatibility alias for the number of
        # source/article associations.  The explicit names prevent readers
        # from mistaking cross-source associations for unique articles.
        "article_association_count": article_count,
        "unique_article_count": len(set(article_ids or ())),
        "evidence_path": "sqlite://sources/" + quote(source_id, safe=""),
    }


def _article_record(
    row: Mapping[str, Any],
    index: int,
    discoveries: Sequence[Mapping[str, Any]],
    identities: Sequence[Mapping[str, Any]],
    source_by_id: Mapping[str, Mapping[str, Any]],
    config_sources_by_id: Mapping[str, Source],
    zoo_by_key: Mapping[str, Zoo],
    zoo_aliases: Mapping[str, str],
    now: datetime,
    same_date_counts: Mapping[str, int],
    duplicate_urls: Set[str],
    duplicate_titles: Set[str],
) -> Dict[str, Any]:
    article_id = _row_key(row, index, "article")
    canonical_url = _redact_url_sensitive(
        _clean_text(_first(row, "canonical_url", "url", "source_url", "normalized_url"))
    )
    normalized = _redact_url_sensitive(
        _normalized_url(_first(row, "canonical_url", "normalized_url", "url", "source_url"))
    )
    title = _clean_text(_first(row, "title", "headline"))
    content = _text(_first(row, "content", "body", "text"))
    published = _iso(_first(row, "published_at", "published", "date", "publication_date"))
    last_fetched = _iso(_first(row, "last_fetched_at", "fetched_at", "updated_at"))
    first_discovered, last_discovered = _discovery_bounds(discoveries)
    if first_discovered is None:
        first_discovered = _iso(_first(row, "first_discovered_at"))
    if last_discovered is None:
        last_discovered = _iso(_first(row, "last_discovered_at"))
    raw_source_url = _first(row, "source_url_raw")
    if raw_source_url in (None, ""):
        raw_source_url = next(
            (
                _first(discovery, "discovered_url_raw", "discovered_url")
                for discovery in discoveries
                if _first(discovery, "discovered_url_raw", "discovered_url") not in (None, "")
            ),
            _first(row, "source_url"),
        )
    raw_source_url = _redact_url_sensitive(raw_source_url)
    source_ids: List[str] = []
    zoo_ids: List[str] = []
    for discovery in discoveries:
        source_id = _clean_text(_first(discovery, "source_id"))
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
        source = source_by_id.get(source_id)
        source_zoo = _clean_text(_first(source or {}, "zoo_id"))
        source_zoo = zoo_aliases.get(source_zoo, source_zoo)
        if source_zoo and source_zoo not in zoo_ids:
            zoo_ids.append(source_zoo)
    for identity in identities:
        identity_zoo = zoo_aliases.get(_clean_text(_first(identity, "zoo_id")), _clean_text(_first(identity, "zoo_id")))
        if identity_zoo and identity_zoo not in zoo_ids:
            zoo_ids.append(identity_zoo)
    direct_zoo = zoo_aliases.get(_clean_text(_first(row, "zoo_id")), _clean_text(_first(row, "zoo_id")))
    if direct_zoo and direct_zoo not in zoo_ids:
        zoo_ids.insert(0, direct_zoo)
    config_zoo_keys: List[str] = []
    for zoo_id in zoo_ids:
        if zoo_id in zoo_by_key:
            config_zoo_keys.append(zoo_id)
            continue
        for key, zoo in zoo_by_key.items():
            if zoo_id in {_clean_text(zoo.id), _clean_text(zoo.slug), _clean_text(zoo.name)}:
                config_zoo_keys.append(key)
                break
    config_zoo_keys = list(dict.fromkeys(config_zoo_keys))
    config_source: Optional[Source] = None
    for source_id in source_ids:
        if source_id in config_sources_by_id:
            config_source = config_sources_by_id[source_id]
            break
    warnings: List[Mapping[str, Any]] = []
    if not title:
        warnings.append(_warning("empty_title"))
    if len(title) > 100:
        warnings.append(_warning("long_title", f"title has {len(title)} characters"))
    if _url_parts(canonical_url) is None:
        warnings.append(_warning("bad_url"))
    parsed_published = _parse_datetime(published)
    if parsed_published is not None:
        if parsed_published > now + FUTURE_GRACE:
            warnings.append(_warning("future_date"))
        if parsed_published < now - timedelta(days=DEFAULT_OLD_DAYS):
            warnings.append(_warning("over_old"))
    if not content.strip():
        warnings.append(_warning("empty_content"))
    if normalized and normalized in duplicate_urls:
        warnings.append(_warning("duplicate_url"))
    title_key = _WHITESPACE_RE.sub(" ", title).strip().casefold()
    if title_key and title_key in duplicate_titles:
        warnings.append(_warning("duplicate_title"))
    # A repeated date is ordinary for a feed/archive and is not an article
    # quality defect by itself.  ``same_date`` is emitted once at zoo/run
    # scope when essentially all dated articles collapse to one date.
    for zoo_key in config_zoo_keys:
        zoo = zoo_by_key[zoo_key]
        linked_config_sources = [
            config_sources_by_id[source_id]
            for source_id in source_ids
            if source_id in config_sources_by_id
        ]
        if not _host_is_official(canonical_url, _official_hosts(zoo, linked_config_sources)):
            warnings.append(_warning("non_official_host"))
        if _source_navigation_match(config_source, canonical_url, title, content):
            warnings.append(_warning("navigation_content_suspect"))
    path = _url_parts(canonical_url)
    if path is not None and (_LIST_PATH_RE.search(path.path or "/") or any(key.lower() in {"page", "paged", "offset", "start"} for key in (path.query or "").split("&") for key in [key.split("=", 1)[0]])):
        warnings.append(_warning("list_page_suspect"))
    parsed_fetched = _parse_datetime(last_fetched)
    if parsed_fetched is None or parsed_fetched < now - timedelta(days=DEFAULT_STALE_DAYS):
        warnings.append(_warning("stale"))
    warning_records = _dedupe_warnings(warnings)
    body_summary = _plain_body_summary(_first(row, "summary", "description"))
    if body_summary is None:
        body_summary = _plain_body_summary(content)
    metadata = _json_object(_first(row, "metadata_json", "metadata"))
    image_value = _first(
        row,
        "image_url",
        "image",
        "thumbnail_url",
        default=_first(metadata, "image_url", "image", "thumbnail_url"),
    )
    image_url = _safe_http_url(image_value)
    evidence_path = "sqlite://articles/" + quote(article_id, safe="")
    discovery_records = [
        {
            "id": _clean_text(_first(discovery, "id")) or None,
            "source_id": _clean_text(_first(discovery, "source_id")) or None,
            "discovered_url": _redact_url_sensitive(_first(discovery, "discovered_url")) or None,
            "discovered_url_raw": _redact_url_sensitive(
                _first(discovery, "discovered_url_raw", "discovered_url")
            ) or None,
            "discovered_at": _iso(_first(discovery, "discovered_at")),
            "last_discovered_at": _iso(
                _first(discovery, "last_discovered_at", "discovered_at")
            ),
        }
        for discovery in discoveries
    ]
    # A report must not expose raw_html, metadata_json, or any unknown column.
    # These are the deliberately selected evidence fields only.
    return {
        "id": article_id,
        "zoo_id": config_zoo_keys[0] if config_zoo_keys else (zoo_ids[0] if zoo_ids else None),
        "zoo_ids": config_zoo_keys,
        "source_ids": source_ids,
        "source_names": [_clean_text(_first(source_by_id.get(source_id, {}), "name")) for source_id in source_ids if _clean_text(_first(source_by_id.get(source_id, {}), "name"))],
        "title": title,
        "url": canonical_url,
        "canonical_url": canonical_url,
        "safe_url": _safe_http_url(canonical_url),
        "normalized_url": normalized or None,
        "source_url_raw": raw_source_url or None,
        "published_at": published,
        "last_fetched_at": last_fetched,
        "first_discovered_at": first_discovered,
        "last_discovered_at": last_discovered,
        "created_at": _iso(_first(row, "created_at")),
        "storage_updated_at": _iso(_first(row, "updated_at", "storage_updated_at")),
        "discoveries": discovery_records,
        "author": _clean_text(_first(row, "author")) or None,
        "summary": body_summary,
        "body_summary": body_summary,
        "image_url": image_url,
        "language": _clean_text(_first(row, "language")) or None,
        "http_status": _first(row, "http_status", "status_code"),
        "crawl_status": _clean_text(_first(row, "crawl_status", "status")) or None,
        "parse_status": _clean_text(_first(row, "parse_status")) or None,
        "content_length": len(content),
        "warnings": warning_records,
        "quality_warnings": warning_records,
        "quality_warning_count": len(warning_records),
        "warning_codes": [item["code"] for item in warning_records],
        "evidence_path": evidence_path,
        "evidence": {
            "path": evidence_path,
            "canonical_url": canonical_url or None,
            "http_status": _first(row, "http_status", "status_code"),
            "last_fetched_at": last_fetched,
            "parse_status": _clean_text(_first(row, "parse_status")) or None,
        },
    }


def _same_date_anomaly(articles: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """Flag a zoo only when its dated article set collapses to one date.

    Two articles sharing a publication date is routine.  Requiring at least
    three parseable dates and one unique calendar date keeps the finding
    useful for feed/parser regressions without annotating every article.
    """

    dated = [
        _parse_datetime(article.get("published_at"))
        for article in articles
    ]
    dated_values = [value for value in dated if value is not None]
    if len(dated_values) < 3:
        return None
    unique_dates = {value.date().isoformat() for value in dated_values}
    if len(unique_dates) != 1:
        return None
    date_value = next(iter(unique_dates))
    return _warning(
        "same_date",
        f"{len(dated_values)} dated articles all use publication date {date_value}",
    )


def _as_utc_iso(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).isoformat()


def build_projection(
    config: Any = None,
    db: Any = None,
    *,
    config_path: Any = None,
    db_path: Any = None,
    now: Any = None,
    git_commit: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the read-only, JSON-serialisable report projection.

    ``config`` accepts a YAML path, :class:`CrawlerConfig`, or a mapping.
    ``db`` accepts a path, a sqlite connection, or a storage-like object with
    a ``connection`` property.  ``db_path`` is an explicit alias convenient
    for keyword callers.  Missing/invalid databases produce an empty view and
    a known issue instead of raising.
    """

    if config_path is not None:
        config = config_path
    if db_path is not None:
        db = db_path
    bundle = _load_config_bundle(config)
    snapshot = _open_snapshot(db)
    clock = _now_value(now)
    config_zoo_by_key: Dict[str, Zoo] = {}
    for zoo in bundle.zoos:
        config_zoo_by_key[_config_zoo_key(zoo)] = zoo
    config_sources_by_id: Dict[str, Source] = {}
    sources_by_zoo_key: Dict[str, List[Source]] = defaultdict(list)
    for source in bundle.sources:
        source_id = _config_source_key(source)
        config_sources_by_id[source_id] = source
        sources_by_zoo_key[_clean_text(source.zoo_id)].append(source)
    db_zoos = snapshot.tables.get("zoos", [])
    db_sources = snapshot.tables.get("sources", [])
    db_discoveries = snapshot.tables.get("discoveries", [])
    db_identities = snapshot.tables.get("identities", [])
    db_articles = snapshot.tables.get("articles", [])
    db_runs = snapshot.tables.get("runs", [])
    db_stats = snapshot.tables.get("stats", [])
    db_zoo_results = snapshot.tables.get("zoo_results", [])
    source_by_db_id: Dict[str, Mapping[str, Any]] = {
        _clean_text(_first(row, "id")): row for row in db_sources if _clean_text(_first(row, "id"))
    }
    # Source stats only persist source IDs.  Keep an explicit configured-ID
    # to adapter map so relative adapter changes can be compared across runs;
    # relying on a zoo-result row (which has no source_id) produces an
    # ``unknown`` bucket and misleading negative deltas.
    source_adapter_by_id: Dict[str, str] = {
        _config_source_key(source): _canonical_source_adapter(source)
        for source in bundle.sources
    }
    for source_id, registered_source in source_by_db_id.items():
        if source_id in source_adapter_by_id:
            continue
        registered_adapter = _clean_text(_first(registered_source, "adapter", "kind", "source_type"))
        if registered_adapter:
            source_adapter_by_id[source_id] = canonical_adapter_kind(registered_adapter) or normalize_adapter_token(registered_adapter)
    zoo_aliases: Dict[str, str] = {}
    for config_key, config_zoo in config_zoo_by_key.items():
        identifiers = {_clean_text(config_zoo.id), _clean_text(config_zoo.slug), _clean_text(config_zoo.name)} - {""}
        for db_zoo in db_zoos:
            db_identifiers = {_clean_text(_first(db_zoo, "id")), _clean_text(_first(db_zoo, "slug")), _clean_text(_first(db_zoo, "name"))} - {""}
            if identifiers & db_identifiers:
                zoo_aliases.update({identifier: config_key for identifier in db_identifiers})
    article_discoveries: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for discovery in db_discoveries:
        article_id = _clean_text(_first(discovery, "article_id"))
        if article_id:
            article_discoveries[article_id].append(discovery)
    for values in article_discoveries.values():
        values.sort(key=lambda item: (_text(_first(item, "discovered_at")), _text(_first(item, "source_id"))))
    article_identities: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for identity in db_identities:
        article_id = _clean_text(_first(identity, "article_id"))
        if article_id:
            article_identities[article_id].append(identity)

    def _run_sort_key(row: Mapping[str, Any]) -> Tuple[datetime, datetime, str]:
        return (
            _parse_datetime(_first(row, "started_at", "created_at", "finished_at")) or datetime.min.replace(tzinfo=timezone.utc),
            _parse_datetime(_first(row, "finished_at")) or datetime.min.replace(tzinfo=timezone.utc),
            _clean_text(_first(row, "id")),
        )

    ordered_runs = sorted(db_runs, key=_run_sort_key, reverse=True)
    run_row: Optional[Mapping[str, Any]] = ordered_runs[0] if ordered_runs else None
    previous_run_row: Optional[Mapping[str, Any]] = ordered_runs[1] if len(ordered_runs) > 1 else None
    run_id = _clean_text(_first(run_row or {}, "id"))
    run_stats_rows: List[Mapping[str, Any]] = [
        row for row in db_stats
        if not run_id or _clean_text(_first(row, "crawl_run_id", "run_id")) == run_id
    ]
    # On an old database without a run id, retain the newest stat for each
    # source rather than dropping all status evidence.
    if not run_id and db_stats:
        newest: Dict[str, Mapping[str, Any]] = {}
        for row in db_stats:
            key = _clean_text(_first(row, "source_id"))
            current = newest.get(key)
            if current is None or _text(_first(row, "finished_at", "started_at")) > _text(_first(current, "finished_at", "started_at")):
                newest[key] = row
        run_stats_rows = list(newest.values())
    stats_by_source: Dict[str, Mapping[str, Any]] = {}
    for stats_row in run_stats_rows:
        source_id = _clean_text(_first(stats_row, "source_id"))
        if source_id:
            stats_by_source[source_id] = stats_row
    zoo_results_rows: List[Mapping[str, Any]] = [
        row
        for row in db_zoo_results
        if not run_id or _clean_text(_first(row, "crawl_run_id", "run_id")) == run_id
    ]
    zoo_result_rows_by_config: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for result_row in zoo_results_rows:
        zoo_id = _clean_text(_first(result_row, "zoo_id"))
        if zoo_id:
            config_key = zoo_aliases.get(zoo_id, zoo_id)
            if config_key in config_zoo_by_key:
                zoo_result_rows_by_config[config_key].append(result_row)
            else:
                # A result may carry a slug/name instead of the configured id.
                for candidate_key, candidate_zoo in config_zoo_by_key.items():
                    if zoo_id in {_clean_text(candidate_zoo.id), _clean_text(candidate_zoo.slug), _clean_text(candidate_zoo.name)}:
                        zoo_result_rows_by_config[candidate_key].append(result_row)
                        break
    # Keep every raw row for duplicate-result diagnostics, but choose one
    # deterministic canonical row per configured zoo for metrics.  This
    # prevents duplicate rows from inflating totals while ensuring they are
    # still visible as an explicit result issue below.
    canonical_zoo_result_by_config: Dict[str, Mapping[str, Any]] = {
        key: rows[0]
        for key, rows in zoo_result_rows_by_config.items()
        if rows
    }

    # First establish duplicate keys and date counts over all rows, including
    # legacy rows that would not satisfy today's unique indexes.
    url_counts: Counter[str] = Counter()
    title_counts: Counter[str] = Counter()
    identity_counts: Counter[str] = Counter()
    date_counts: Counter[str] = Counter()
    for row in db_articles:
        normalized = _normalized_url(_first(row, "canonical_url", "normalized_url", "url", "source_url"))
        if normalized:
            url_counts[normalized] += 1
        title = _clean_text(_first(row, "title", "headline"))
        title_key = _WHITESPACE_RE.sub(" ", title).strip().casefold()
        if title_key:
            title_counts[title_key] += 1
        identity_key = _clean_text(_first(row, "content_identity_key"))
        if not identity_key:
            content_hash = _clean_text(_first(row, "content_hash"))
            if content_hash and title_key:
                identity_key = content_hash + "\n" + title_key
        if identity_key:
            identity_counts[identity_key] += 1
        published = _iso(_first(row, "published_at", "published", "date", "publication_date"))
        if published:
            date_counts[published[:10]] += 1
    duplicate_urls = {key for key, count in url_counts.items() if count > 1}
    duplicate_titles = {key for key, count in title_counts.items() if count > 1}

    article_records: List[Dict[str, Any]] = []
    for index, row in enumerate(db_articles):
        raw_id = _clean_text(_first(row, "id")) or f"article-{index + 1}"
        linked_discoveries = article_discoveries.get(raw_id, [])
        article_records.append(
            _article_record(
                row,
                index,
                linked_discoveries,
                article_identities.get(raw_id, []),
                source_by_db_id,
                config_sources_by_id,
                config_zoo_by_key,
                zoo_aliases,
                clock,
                date_counts,
                duplicate_urls,
                duplicate_titles,
            )
        )
    article_records.sort(key=_article_sort_key)

    failures: List[str] = []
    failure_class_counts: Counter[str] = Counter()
    known_issues: List[str] = []
    if bundle.error:
        _failure_add(failures, bundle.error, failure_class_counts, "configuration_error")
        known_issues.append("Configuration could not be loaded; the report contains no configured zoos.")
    if not snapshot.available:
        known_issues.append("Database is missing or unreadable; database-backed evidence is empty.")
    elif _database_legacy(snapshot):
        known_issues.append("Database uses a legacy or partial schema; unavailable columns are shown as unknown.")
    if snapshot.available and not db_articles:
        known_issues.append("Database contains no articles.")
    if not run_row:
        known_issues.append("No crawl run is recorded in the database.")
    run_status = _clean_text(_first(run_row or {}, "status")) or "no_run"
    run_error = _redact_text_urls(_first(run_row or {}, "error"))
    if run_error:
        _failure_add(failures, run_error, failure_class_counts)

    zoo_records: List[Dict[str, Any]] = []
    all_source_records: List[Dict[str, Any]] = []
    configured_source_counts: Counter[str] = Counter()
    adapter_stats: Dict[str, Dict[str, int]] = {
        adapter: {
            "configured_count": 0,
            "enabled_count": 0,
            "registered_count": 0,
            "success_count": 0,
            "error_count": 0,
            "article_count": 0,
            "article_association_count": 0,
            "unique_article_count": 0,
            "discovered_count": 0,
            "fetched_count": 0,
            "stored_count": 0,
        }
        for adapter in KNOWN_ADAPTERS
    }
    # Preserve unknown configured adapter kinds in the category report while
    # still marking them unsupported and never attempting to execute them.
    for source in bundle.sources:
        adapter = _canonical_source_adapter(source)
        if adapter not in adapter_stats:
            adapter_stats[adapter] = {
                "configured_count": 0,
                "enabled_count": 0,
                "registered_count": 0,
                "success_count": 0,
                "error_count": 0,
                "article_count": 0,
                "article_association_count": 0,
                "unique_article_count": 0,
                "discovered_count": 0,
                "fetched_count": 0,
                "stored_count": 0,
            }
        adapter_stats[adapter]["configured_count"] += 1
        adapter_stats[adapter]["enabled_count"] += int(bool(source.enabled))
        configured_source_counts[adapter] += 1

    source_article_counts: Counter[str] = Counter()
    source_article_ids: Dict[str, Set[str]] = defaultdict(set)
    adapter_article_ids: Dict[str, Set[str]] = defaultdict(set)
    for article in article_records:
        for source_id in article.get("source_ids", []):
            source_article_counts[source_id] += 1
            article_id = _clean_text(article.get("id"))
            if article_id:
                source_article_ids[source_id].add(article_id)
                article_adapter: Optional[str] = source_adapter_by_id.get(source_id)
                if article_adapter is not None:
                    adapter_article_ids[article_adapter].add(article_id)
    for zoo in bundle.zoos:
        zoo_key = _config_zoo_key(zoo)
        configured_sources = sources_by_zoo_key.get(_clean_text(zoo.id), []) + sources_by_zoo_key.get(_clean_text(zoo.slug), [])
        # A source's zoo_id normally uses the ID.  Deduplicate if callers use
        # both id and slug in an in-memory config.
        unique_sources: List[Source] = []
        seen_source_keys: Set[str] = set()
        for source in configured_sources:
            key = _config_source_key(source)
            if key not in seen_source_keys:
                seen_source_keys.add(key)
                unique_sources.append(source)
        configured_sources = unique_sources
        source_records: List[Dict[str, Any]] = []
        for source in configured_sources:
            matched_db_source: Optional[Mapping[str, Any]] = _db_source_match(source, zoo, db_sources)
            source_id = _config_source_key(source)
            stat = stats_by_source.get(source_id)
            if stat is None and matched_db_source is not None:
                stat = stats_by_source.get(_clean_text(_first(matched_db_source, "id")))
            count = source_article_counts.get(source_id, 0)
            record = _source_record(
                source,
                zoo,
                matched_db_source,
                stat,
                count,
                sorted(source_article_ids.get(source_id, set())),
            )
            source_records.append(record)
            all_source_records.append(record)
            adapter = record["adapter_kind"]
            if adapter not in adapter_stats:
                adapter_stats[adapter] = {key: 0 for key in next(iter(adapter_stats.values()))}
            stats = adapter_stats[adapter]
            stats["registered_count"] += int(record["registered"])
            stats["success_count"] += int(record["success"] is True)
            stats["error_count"] += _int(record["error_count"])
            stats["article_count"] += _int(record["article_count"])
            stats["article_association_count"] += _int(record["article_association_count"])
            stats["unique_article_count"] += _int(record["unique_article_count"])
            stats["discovered_count"] += _int(record["discovered_count"])
            stats["fetched_count"] += _int(record["fetched_count"])
            stats["stored_count"] += _int(record["stored_count"])
            source_error_category = (record.get("error_categories") or [None])[0]
            for error in record.get("errors", []):
                _failure_add(
                    failures,
                    error,
                    failure_class_counts,
                    source_error_category,
                    http_status=record.get("http_status"),
                )
            if record["enabled"] and record.get("status") in {"error", "failed", "failure", "partial", "completed_with_errors"} and not record.get("errors"):
                _failure_add(
                    failures,
                    f"{_clean_text(source.name) or source_id}: status {record.get('status')}",
                    failure_class_counts,
                    source_error_category or _specific_failure_category(record.get("status")),
                    http_status=record.get("http_status"),
                )
            if record["enabled"] and not record["registered"]:
                known_issues.append("Configured source is not registered in the database: " + source_id)
        zoo_articles = [article for article in article_records if zoo_key in article.get("zoo_ids", []) or article.get("zoo_id") == zoo_key]
        zoo_articles.sort(key=_article_sort_key)
        warning_items_list: List[Mapping[str, Any]] = []
        for article in zoo_articles:
            warning_items_list.extend(article.get("warnings", []))
        same_date_warning = _same_date_anomaly(zoo_articles)
        if same_date_warning is not None:
            warning_items_list.append(same_date_warning)
        for source_record in source_records:
            for error in source_record.get("errors", []):
                warning_items_list.append(_warning("source_error", error))
        warning_items: Sequence[Mapping[str, Any]] = _dedupe_warnings(warning_items_list)
        zoo_result_rows = zoo_result_rows_by_config.get(zoo_key, [])
        zoo_result = canonical_zoo_result_by_config.get(zoo_key)
        zoo_result_count = len(zoo_result_rows)
        # An explicit unsupported source is itself a stable outcome even when
        # an older database has no per-zoo result row.  Do not infer this from
        # arbitrary/unknown adapter names: only the canonical unsupported
        # marker (or an explicit source status) receives this classification.
        enabled_source_records = [source for source in source_records if source.get("enabled")]
        only_unsupported_sources = bool(enabled_source_records) and all(
            source.get("adapter_kind") == "unsupported"
            or _clean_text(source.get("status")).lower() == "unsupported"
            for source in enabled_source_records
        )
        config_only_source_not_found = _config_only_source_not_found(zoo, source_records)
        completion_category = (
            _completion_category(zoo_result)
            if zoo_result is not None
            else (
                "source_not_found"
                if config_only_source_not_found and (run_row is None or not zoo.enabled)
                else ("unsupported" if only_unsupported_sources else "failed")
            )
        )
        completion_classification = _completion_classification(completion_category)
        result_issue: Optional[str] = None
        result_external_limited = False
        result_blocking = False
        source_parsed = sum(
            _int(source.get("parsed_count"), _int(source.get("fetched_count")))
            for source in source_records
        )
        source_inserted = sum(
            _int(source.get("inserted_count"), _int(source.get("stored_count")))
            for source in source_records
        )
        source_failed = sum(
            _int(source.get("failed_count"), _int(source.get("error_count")))
            for source in source_records
        )
        counts = {
            "configured_sources": len(configured_sources),
            "registered_sources": sum(1 for source in source_records if source["registered"]),
            "articles": len(zoo_articles),
            "unique_articles": len(zoo_articles),
            "source_article_associations": sum(
                _int(source.get("article_association_count")) for source in source_records
            ),
            "discovered": sum(_int(source.get("discovered_count")) for source in source_records),
            "fetched": sum(_int(source.get("fetched_count")) for source in source_records),
            "parsed": source_parsed,
            "stored": sum(_int(source.get("stored_count")) for source in source_records),
            "inserted": source_inserted,
            "updated": 0,
            "failed": source_failed,
            "duplicate_filtered": sum(_int(source.get("duplicate_candidate_count")) for source in source_records),
            "already_known": sum(_int(source.get("already_known_count")) for source in source_records),
            "duplicate_candidates": sum(_int(source.get("duplicate_candidate_count")) for source in source_records),
            "errors": sum(_int(source.get("error_count")) for source in source_records),
            "warnings": len(warning_items),
            "duration_ms": sum(_int(source.get("duration_ms")) for source in source_records),
        }
        if zoo_result is not None:
            # Newer crawler runs persist one auditable result per zoo.  Keep
            # source-stat counts as a fallback, but prefer this canonical
            # result when it exists (especially when no source stat table is
            # available in a legacy/exported database).
            counts.update(
                {
                    "discovered": _int(_first(zoo_result, "discovered", "discovered_count"), counts["discovered"]),
                    "fetched": _int(_first(zoo_result, "parsed", "parsed_count"), counts["fetched"]),
                    "parsed": _int(_first(zoo_result, "parsed", "parsed_count"), counts["parsed"]),
                    "stored": _int(_first(zoo_result, "inserted", "inserted_count"), counts["stored"]),
                    "inserted": _int(_first(zoo_result, "inserted", "inserted_count"), counts["inserted"]),
                    "updated": _int(_first(zoo_result, "updated", "updated_count")),
                    "failed": _int(_first(zoo_result, "failed", "failed_count")),
                    "duration_ms": _int(
                        _first(zoo_result, "duration_ms"),
                        sum(_int(source.get("duration_ms")) for source in source_records),
                    ),
                    "duplicate_candidates": _int(_first(zoo_result, "duplicate_filtered", "duplicate_filtered_count"), counts["duplicate_candidates"]),
                    "duplicate_filtered": _int(_first(zoo_result, "duplicate_filtered", "duplicate_filtered_count"), counts["duplicate_filtered"]),
                    "errors": _int(_first(zoo_result, "failed", "failed_count"), counts["errors"]),
                }
            )
            result_status = _clean_text(_first(zoo_result, "status"))
            result_error = _redact_text_urls(_first(zoo_result, "error_summary", "error"))
            result_source_status = _clean_text(_first(zoo_result, "source_status"))
            result_error_category = _result_error_category(zoo_result)
            result_http_status = _first(zoo_result, "http_status", "status_code")
            result_failure_category = result_error_category or _specific_failure_category(result_source_status)
            result_external_limited, result_blocking = _evidence_flags(
                [result_error_category, result_source_status],
                status=result_status,
                http_status=result_http_status,
                messages=[result_error],
            )
            if result_error:
                warning_items = _dedupe_warnings(list(warning_items) + [_warning("source_error", result_error)])
                _failure_add(
                    failures,
                    f"{_clean_text(zoo.name)}: {result_error}",
                    failure_class_counts,
                    result_failure_category,
                    http_status=result_http_status,
                )
            counts["warnings"] = len(warning_items)
            if result_status in {"failed", "error", "failure"}:
                status = "error"
                _failure_add(
                    failures,
                    f"{_clean_text(zoo.name)}: status {result_status}",
                    failure_class_counts,
                    result_failure_category,
                    http_status=result_http_status,
                )
            elif result_status in {"partial", "completed_with_errors", "warning"}:
                status = "partial"
                _failure_add(
                    failures,
                    f"{_clean_text(zoo.name)}: status {result_status}",
                    failure_class_counts,
                    result_failure_category,
                    http_status=result_http_status,
                )
            elif result_status in {"success", "completed", "success_no_items", "ok"}:
                status = "ok" if result_status != "success_no_items" or zoo_articles else "no_data"
            elif completion_category in LIMITATION_COMPLETION_CATEGORIES:
                # A source can persist its limitation directly in ``status``
                # instead of using ``failed`` plus ``error_category``.
                status = "error"
            else:
                # Unknown result statuses are not external limitations: keep
                # them blocking so an accidental new vocabulary cannot pass
                # acceptance silently.
                status = "error"
                _failure_add(
                    failures,
                    f"{_clean_text(zoo.name)}: status {result_status or 'unknown'}",
                    failure_class_counts,
                    "failed",
                    http_status=result_http_status,
                )
        else:
            result_status = ""
            result_source_status = ""
            result_error_category = ""
            result_error = ""
            result_http_status = None
            status = _status_for_zoo(zoo, source_records, zoo_articles, run_status)
        if zoo.enabled and run_row is not None and zoo_result_count != 1:
            status = "missing_result" if zoo_result_count == 0 else "duplicate_result"
            result_issue = status
            completion_category = "failed"
            completion_classification = "FAILED"
            warning_items = _dedupe_warnings(
                list(warning_items)
                + [_warning("missing_result", f"latest run has {zoo_result_count} zoo results")]
            )
            _failure_add(
                failures,
                f"{_clean_text(zoo.name)}: latest run has {zoo_result_count} zoo results (expected 1)",
                failure_class_counts,
                "failed",
            )
        counts["warnings"] = len(warning_items)
        if status in {"error", "partial"}:
            for source_record in source_records:
                for error in source_record.get("errors", []):
                    _failure_add(
                        failures,
                        f"{_clean_text(zoo.name)} / {_clean_text(source_record.get('name'))}: {error}",
                        failure_class_counts,
                        (source_record.get("error_categories") or [None])[0],
                    )
        if status in {"error", "partial", "unknown", "no_data", "missing_result", "duplicate_result"} and zoo.enabled:
            known_issues.append(f"{_clean_text(zoo.name)} has status {_status_label(status)}.")
        zoo_failure_classes: Set[str] = set()
        for source_record in source_records:
            zoo_failure_classes.update(
                _failure_class(item)
                for item in source_record.get("error_categories", [])
                if _clean_text(item)
            )
            zoo_failure_classes.update(
                _failure_class(error)
                for error in source_record.get("errors", [])
                if _clean_text(error)
            )
        if result_error_category:
            zoo_failure_classes.add(_failure_class(result_error_category))
        if result_external_limited:
            zoo_failure_classes.add(
                _http_failure_class(
                    result_http_status,
                    default=_failure_class(result_error_category or result_source_status or result_error),
                )
            )
        if result_blocking and not result_error_category:
            zoo_failure_classes.add("failed")
        if completion_classification not in {"SUPPORTED", "SUPPORTED_NO_CURRENT_NEWS"}:
            zoo_failure_classes.add(_failure_class(completion_category))
        latest_error_values: List[str] = []
        if result_error:
            latest_error_values.append(result_error)
        for source_record in source_records:
            for error in source_record.get("errors", []):
                if error and error not in latest_error_values:
                    latest_error_values.append(error)
        source_url_values = [
            _redact_url_sensitive(source.get("url"))
            for source in source_records
            if _redact_url_sensitive(source.get("url"))
        ]
        adapter_values = sorted(
            {
                _clean_text(source.get("adapter"))
                for source in source_records
                if _clean_text(source.get("adapter"))
            },
            key=str.casefold,
        )
        adapter_kind_values = sorted(
            {
                _clean_text(source.get("adapter_kind"))
                for source in source_records
                if _clean_text(source.get("adapter_kind"))
            },
            key=str.casefold,
        )
        dated_articles = [
            (article.get("published_at"), _parse_datetime(article.get("published_at")))
            for article in zoo_articles
            if _clean_text(article.get("published_at"))
        ]
        valid_dates = [(value, stamp) for value, stamp in dated_articles if stamp is not None]
        if valid_dates:
            latest_news_date = _iso(max(valid_dates, key=lambda item: item[1] or datetime.min.replace(tzinfo=timezone.utc))[1])
        else:
            latest_news_date = max((_clean_text(value) for value, _ in dated_articles), default=None) or None
        effective_http_status = result_http_status
        if effective_http_status is None:
            effective_http_status = next(
                (source.get("http_status") for source in source_records if source.get("http_status") is not None),
                None,
            )
        effective_run_status = result_status or (
            "missing_result" if run_row is not None and zoo_result_count != 1 else None
        )
        error_summary = "; ".join(latest_error_values)[:800] if latest_error_values else None
        list_provenance = _provenance_value(getattr(zoo, "list_provenance", []))
        flat_counts = {
            key: counts.get(key, 0)
            for key in ("discovered", "parsed", "inserted", "failed", "errors")
        }
        zoo_records.append(
            {
                "id": zoo_key,
                "slug": _clean_text(zoo.slug) or zoo_key,
                "name": _clean_text(zoo.name) or zoo_key,
                "country_code": _clean_text(zoo.country_code) or None,
                "country": _clean_text(zoo.country_code) or None,
                "language": _clean_text(zoo.language) or None,
                "region": _clean_text(getattr(zoo, "region", None)) or None,
                "city": _clean_text(getattr(zoo, "city", None)) or None,
                "website_url": _redact_url_sensitive(zoo.website_url) or None,
                "enabled": bool(zoo.enabled),
                "source_status": _clean_text(getattr(zoo, "source_status", None)) or None,
                "groups": bundle.groups.get(
                    zoo_key,
                    _groups(getattr(zoo, "groups", ()) or getattr(zoo, "metadata", {})),
                ),
                "list_provenance": list_provenance,
                "status": status,
                "status_label": _status_label(status),
                "run_status": effective_run_status,
                "status_raw": result_status or status,
                "zoo_result_count": zoo_result_count,
                "result_issue": result_issue,
                "run_source_status": result_source_status or None,
                "completion_category": completion_category,
                "completion_classification": completion_classification,
                "failure_classes": sorted(zoo_failure_classes),
                "external_limited": bool(
                    result_external_limited
                    or any(source.get("external_limited") for source in source_records)
                    or completion_category in LIMITATION_COMPLETION_CATEGORIES
                ),
                "http_status": effective_http_status,
                "run_source_url": _redact_url_sensitive(_first(zoo_result or {}, "source_url")) or None,
                "run_error": result_error or None,
                "run_error_category": result_error_category or None,
                "official_site_url": _redact_url_sensitive(zoo.website_url) or None,
                "official_site": _redact_url_sensitive(zoo.website_url) or None,
                "news_source_urls": source_url_values,
                "news_source_url": source_url_values[0] if source_url_values else None,
                "adapter": ";".join(adapter_values) or None,
                "adapter_kind": ";".join(adapter_kind_values) or None,
                "latest_errors": latest_error_values,
                "latest_error": latest_error_values[0] if latest_error_values else None,
                "latest_news_date": latest_news_date,
                "duration_ms": counts.get("duration_ms", 0),
                "error_summary": error_summary,
                **flat_counts,
                "counts": counts,
                "sources": source_records,
                "recent_articles": zoo_articles[:3],
                "warnings": warning_items,
                "quality_warnings": warning_items,
                "quality_warning_count": len(warning_items),
                "warning_codes": sorted({item["code"] for item in warning_items}),
                "evidence_path": "sqlite://zoos/" + quote(zoo_key, safe=""),
            }
        )

    for adapter, article_ids in adapter_article_ids.items():
        if adapter in adapter_stats:
            adapter_stats[adapter]["unique_article_count"] = len(article_ids)

    zoo_records.sort(key=_zoo_priority)

    # Count exactly one completion state per configured enabled zoo.  Missing
    # and duplicate latest-run results were already converted to FAILED above,
    # so these totals cannot silently disappear or double-count raw rows.
    completion_categories: Dict[str, int] = {category: 0 for category in COMPLETION_CATEGORIES}
    completion_classifications: Dict[str, int] = {
        category: 0 for category in PUBLIC_COMPLETION_CLASSIFICATIONS
    }
    for zoo_record in zoo_records:
        if not zoo_record.get("enabled"):
            continue
        raw_category = _clean_text(zoo_record.get("completion_category")) or "failed"
        public_category = _clean_text(zoo_record.get("completion_classification")) or "FAILED"
        completion_categories[raw_category] = completion_categories.get(raw_category, 0) + 1
        completion_classifications[public_category] = completion_classifications.get(public_category, 0) + 1

    limitation_categories = {
        category: count
        for category, count in completion_categories.items()
        if category in LIMITATION_COMPLETION_CATEGORIES and count
    }
    limitation_class_counts = {
        category: count
        for category, count in limitation_categories.items()
    }
    limitations = [
        f"{_clean_text(zoo.get('name'))}: {_completion_classification(zoo.get('completion_category'))}"
        for zoo in zoo_records
        if zoo.get("enabled") and _clean_text(zoo.get("completion_category")) in LIMITATION_COMPLETION_CATEGORIES
    ]

    # Duplicate config IDs are not silently hidden from the output; the
    # explicit list remains one record per loaded config entry in normal use.
    db_zoo_keys = {_clean_text(_first(row, "id", "slug")) for row in db_zoos}
    configured_keys = set(config_zoo_by_key)
    db_only_zoos = sorted(key for key in db_zoo_keys if key and key not in configured_keys)
    if db_only_zoos:
        known_issues.append("Database contains zoo records absent from configuration: " + ", ".join(db_only_zoos))
    enabled_zoos = [zoo for zoo in zoo_records if zoo["enabled"]]
    enabled_sources = [source for source in all_source_records if source["enabled"]]
    article_capable_enabled_sources = [
        source for source in enabled_sources if source.get("adapter_kind") in ARTICLE_CAPABLE_ADAPTERS
    ]
    health_only_enabled_sources = [
        source for source in enabled_sources if source.get("adapter_kind") in HEALTH_ONLY_ADAPTERS
    ]
    # Keep this compatibility name, but make its semantics truthful: a
    # ``registry_only`` source is a health check, not an article executable.
    executable_enabled_sources = article_capable_enabled_sources
    enabled_zoo_keys = {zoo["id"] for zoo in zoo_records if zoo["enabled"]}
    latest_result_counts = {
        key: len(zoo_result_rows_by_config.get(key, []))
        for key in enabled_zoo_keys
    }
    result_match_keys = {key for key, count in latest_result_counts.items() if count == 1}
    result_missing_keys = sorted(key for key, count in latest_result_counts.items() if count == 0)
    result_duplicate_keys = sorted(key for key, count in latest_result_counts.items() if count > 1)
    result_coverage_count = len(result_match_keys)
    source_failures = sum(_int(source.get("error_count")) for source in all_source_records)
    warning_count = sum(len(article.get("warnings", [])) for article in article_records)
    quality_warning_categories: Counter[str] = Counter()
    for article in article_records:
        for warning in article.get("warnings", []):
            code = _clean_text(warning.get("code"))
            if code:
                quality_warning_categories[code] += 1
    for zoo_record in zoo_records:
        # Zoo warnings intentionally aggregate article findings for the
        # detail view, but top-level quality totals represent underlying
        # findings rather than every projection location.  Suppress warning
        # records that are exact copies of an article warning linked to this
        # zoo; retain genuinely zoo-level findings such as same_date,
        # source_error, and missing_result.
        article_warning_keys: Set[Tuple[str, str]] = set()
        zoo_id = _clean_text(zoo_record.get("id"))
        for article in article_records:
            article_zoo_ids = {
                _clean_text(article.get("zoo_id")),
                *(_clean_text(item) for item in (article.get("zoo_ids", []) or [])),
            }
            if zoo_id not in article_zoo_ids:
                continue
            article_warning_keys.update(
                _warning_sort_key(warning)
                for warning in article.get("warnings", [])
            )
        for warning in zoo_record.get("warnings", []):
            code = _clean_text(warning.get("code"))
            if code and _warning_sort_key(warning) not in article_warning_keys:
                quality_warning_categories[code] += 1
    warning_count = sum(quality_warning_categories.values())
    categories = {
        "supported": list(SUPPORTED_ADAPTERS),
        "known": list(KNOWN_ADAPTERS),
        "configured": {key: configured_source_counts[key] for key in sorted(configured_source_counts)},
        "counts": {key: adapter_stats[key]["configured_count"] for key in sorted(adapter_stats)},
        "error_categories": sorted(
            {
                _result_error_category(row) or ""
                for row in canonical_zoo_result_by_config.values()
                if _result_error_category(row)
            }
        ),
        "completion": completion_categories,
        "completion_classifications": completion_classifications,
        "public_completion_classifications": list(PUBLIC_COMPLETION_CLASSIFICATIONS),
    }
    coverage = {
        "configured_zoos": len(zoo_records),
        "enabled_zoos": len(enabled_zoos),
        "database_zoos": len(db_zoos),
        "configured_sources": len(bundle.sources),
        "enabled_sources": len(enabled_sources),
        "enabled_source_checks": len(enabled_sources),
        "executable_enabled_sources": len(executable_enabled_sources),
        "article_capable_enabled_sources": len(article_capable_enabled_sources),
        "article_capable_executable_enabled_sources": len(article_capable_enabled_sources),
        "health_only_enabled_sources": len(health_only_enabled_sources),
        "health_only_registry_only_enabled_sources": len(health_only_enabled_sources),
        "registry_only_enabled_sources": len(health_only_enabled_sources),
        "registered_sources": sum(1 for source in all_source_records if source["registered"]),
        "registered_source_checks": sum(1 for source in enabled_sources if source["registered"]),
        "article_capable_registered_sources": sum(1 for source in article_capable_enabled_sources if source["registered"]),
        "health_only_registered_sources": sum(1 for source in health_only_enabled_sources if source["registered"]),
        "sources_registered_percent": round((sum(1 for source in enabled_sources if source["registered"]) / len(enabled_sources)) * 100, 2) if enabled_sources else 100.0,
        "articles": len(article_records),
        "unique_articles": len(article_records),
        "source_article_associations": sum(source_article_counts.values()),
        "unique_source_article_associations": sum(
            len(article.get("source_ids", []) or []) for article in article_records
        ),
        "duplicate_url_count": sum(max(0, count - 1) for count in url_counts.values()),
        "duplicate_identity_count": sum(max(0, count - 1) for count in identity_counts.values()),
        "quality_warnings": warning_count,
        "quality_warning_categories": {key: quality_warning_categories[key] for key in sorted(quality_warning_categories)},
        "zoo_results": len(canonical_zoo_result_by_config),
        "raw_zoo_results": len(zoo_results_rows),
        "duplicate_zoo_results": sum(max(0, len(rows) - 1) for rows in zoo_result_rows_by_config.values()),
        "zoo_results_percent": min(100.0, round((result_coverage_count / len(enabled_zoos)) * 100, 2)) if enabled_zoos else 100.0,
        "latest_run_zoo_results": result_coverage_count,
        "latest_run_enabled_zoo_count": len(enabled_zoos),
        "latest_run_missing_zoo_results": result_missing_keys,
        "latest_run_duplicate_zoo_results": result_duplicate_keys,
        "latest_run_zoo_result_coverage_percent": min(100.0, round((result_coverage_count / len(enabled_zoos)) * 100, 2)) if enabled_zoos else 100.0,
    }
    run_counts = {
        "discovered": sum(_int(source.get("discovered_count")) for source in all_source_records),
        "fetched": sum(_int(source.get("fetched_count")) for source in all_source_records),
        "parsed": sum(_int(source.get("fetched_count")) for source in all_source_records),
        "stored": sum(_int(source.get("stored_count")) for source in all_source_records),
        "inserted": sum(_int(source.get("stored_count")) for source in all_source_records),
        "updated": 0,
        "failed": source_failures,
        "duplicate_filtered": sum(_int(source.get("duplicate_candidate_count")) for source in all_source_records),
        "already_known": sum(_int(source.get("already_known_count")) for source in all_source_records),
        "duplicate_candidates": sum(_int(source.get("duplicate_candidate_count")) for source in all_source_records),
        "errors": source_failures,
        "source_article_associations": sum(source_article_counts.values()),
        "unique_articles": len(article_records),
    }
    if canonical_zoo_result_by_config:
        run_counts.update(
            {
                "zoo_discovered": sum(_int(_first(row, "discovered", "discovered_count")) for row in canonical_zoo_result_by_config.values()),
                "zoo_parsed": sum(_int(_first(row, "parsed", "parsed_count")) for row in canonical_zoo_result_by_config.values()),
                "zoo_inserted": sum(_int(_first(row, "inserted", "inserted_count")) for row in canonical_zoo_result_by_config.values()),
                "parsed": sum(_int(_first(row, "parsed", "parsed_count")) for row in canonical_zoo_result_by_config.values()),
                "inserted": sum(_int(_first(row, "inserted", "inserted_count")) for row in canonical_zoo_result_by_config.values()),
                "updated": sum(_int(_first(row, "updated", "updated_count")) for row in canonical_zoo_result_by_config.values()),
                "failed": sum(_int(_first(row, "failed", "failed_count")) for row in canonical_zoo_result_by_config.values()),
                "duplicate_filtered": sum(_int(_first(row, "duplicate_filtered", "duplicate_filtered_count")) for row in canonical_zoo_result_by_config.values()),
                "zoo_failed": sum(_int(_first(row, "failed", "failed_count")) for row in canonical_zoo_result_by_config.values()),
                "zoo_duplicate_filtered": sum(_int(_first(row, "duplicate_filtered", "duplicate_filtered_count")) for row in canonical_zoo_result_by_config.values()),
            }
        )
    result_durations = sum(_int(_first(row, "duration_ms")) for row in canonical_zoo_result_by_config.values())
    wall_duration_ms = _int(_first(run_row or {}, "duration_ms"))
    run_counts["wall_duration_ms"] = wall_duration_ms
    run_counts["zoo_duration_ms"] = result_durations
    # ``total_duration_ms`` is the wall-clock run duration when available;
    # summed zoo duration remains available separately for diagnostics.
    run_counts["total_duration_ms"] = wall_duration_ms or result_durations
    run_counts["result_issues"] = sum(1 for zoo in zoo_records if zoo.get("result_issue"))
    run_counts["errors"] = max(
        source_failures,
        _int(run_counts.get("failed")),
        _int(run_counts.get("result_issues")),
    )
    run_counts["error_count"] = run_counts["errors"]
    run_counts["parse_failures"] = sum(
        count for category, count in completion_categories.items()
        if "parse" in category
    )
    run_counts["unique"] = len(article_records)
    run_counts["total_enabled"] = len(enabled_zoos)
    for category in COMPLETION_CATEGORIES:
        run_counts[category] = completion_categories.get(category, 0)
    run_evidence: Dict[str, Any] = {
        "id": run_id or None,
        "batch_id": _clean_text(_first(run_row or {}, "batch_id")) or None,
        "status": run_status,
        "error": run_error or None,
        "started_at": _iso(_first(run_row or {}, "started_at")),
        "finished_at": _iso(_first(run_row or {}, "finished_at")),
        # This is the crawl's wall-clock duration.  The sum of per-zoo
        # durations is retained below as a diagnostic, not as the run time.
        "duration_ms": run_counts.get("wall_duration_ms", 0),
        "counts": run_counts,
        "totals": {
            "configured_zoos": len(zoo_records),
            "enabled_zoos": len(enabled_zoos),
            "configured_sources": len(bundle.sources),
            "enabled_sources": len(enabled_sources),
            "unique_articles": len(article_records),
            "source_article_associations": sum(source_article_counts.values()),
        },
        "actual": {
            "zoo_results": len(zoo_results_rows),
            "canonical_zoo_results": len(canonical_zoo_result_by_config),
            "duplicate_zoo_results": sum(max(0, len(rows) - 1) for rows in zoo_result_rows_by_config.values()),
            "enabled_zoo_results": result_coverage_count,
            "articles": len(article_records),
            "source_article_associations": sum(source_article_counts.values()),
            "unique_articles": len(article_records),
        },
        "completion_categories": completion_categories,
        "classification_counts": completion_categories,
        "completion_classifications": completion_classifications,
        "public_classification_counts": completion_classifications,
        "limitation_categories": limitation_categories,
        "limitation_class_counts": limitation_class_counts,
        "limitations": limitations,
        "wall_duration_ms": run_counts.get("wall_duration_ms", 0),
        "zoo_duration_ms": run_counts.get("zoo_duration_ms", 0),
        "total_duration_ms": run_counts.get("total_duration_ms", 0),
        "source_stats": sorted(all_source_records, key=lambda item: (item["id"], item["adapter"])),
        "zoo_results": sorted(
            [
                {
                    "id": _clean_text(_first(row, "id")) or None,
                    "zoo_id": _clean_text(_first(row, "zoo_id")) or None,
                    "zoo_slug": _clean_text(_first(row, "zoo_slug")) or None,
                    "zoo_name": _clean_text(_first(row, "zoo_name")) or None,
                    "status": _clean_text(_first(row, "status")) or None,
                    "source_status": _clean_text(_first(row, "source_status")) or None,
                    "completion_category": _completion_category(row),
                    "completion_classification": _completion_classification(row),
                    "discovered": _int(_first(row, "discovered", "discovered_count")),
                    "parsed": _int(_first(row, "parsed", "parsed_count")),
                    "inserted": _int(_first(row, "inserted", "inserted_count")),
                    "updated": _int(_first(row, "updated", "updated_count")),
                    "failed": _int(_first(row, "failed", "failed_count")),
                    "duplicate_filtered": _int(_first(row, "duplicate_filtered", "duplicate_filtered_count")),
                    "duration_ms": _first(row, "duration_ms"),
                    "source_url": _redact_url_sensitive(_first(row, "source_url")) or None,
                    "http_status": _first(row, "http_status", "status_code"),
                    "error_category": _result_error_category(row),
                    "error_summary": _clean_text(_first(row, "error_summary", "error")) or None,
                    "started_at": _iso(_first(row, "started_at")),
                    "finished_at": _iso(_first(row, "finished_at")),
                    "evidence_path": "sqlite://crawl_zoo_results/" + quote(_clean_text(_first(row, "id")), safe=""),
                }
                for row in canonical_zoo_result_by_config.values()
            ],
            key=lambda item: (_text(item.get("zoo_id")), _text(item.get("id"))),
        ),
        "evidence_path": "sqlite://crawl_runs/" + quote(run_id, safe="") if run_id else None,
    }
    run_quality_warnings = [
        {
            "zoo_id": zoo.get("id"),
            "zoo_name": zoo.get("name"),
            "warning": warning,
        }
        for zoo in zoo_records
        for warning in zoo.get("warnings", [])
        if _clean_text(warning.get("code")) == "same_date"
    ]
    run_evidence["quality_warnings"] = run_quality_warnings
    run_evidence["anomalies"] = run_quality_warnings
    run_evidence["warnings"] = run_quality_warnings
    if previous_run_row is None:
        run_evidence["relative_change"] = {
            "available": False,
            "reason": "no previous run in database",
            "previous_run_id": None,
            "counts": {},
        }
        adapter_changes: Dict[str, Any] = {
            "available": False,
            "reason": "no previous run in database",
            "evidence_complete": False,
            "changes": {},
            "added_sources": [],
            "removed_sources": [],
            "kind_changed_sources": [],
            "unknown_previous_sources": [],
            "unknown_current_sources": [],
            "source_changes": {},
        }
    else:
        previous_id = _clean_text(_first(previous_run_row, "id"))
        previous_rows_raw = [
            row for row in db_zoo_results
            if _clean_text(_first(row, "crawl_run_id", "run_id")) == previous_id
        ]
        previous_rows_by_config: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
        for row in previous_rows_raw:
            previous_zoo_id = _clean_text(_first(row, "zoo_id"))
            previous_key = zoo_aliases.get(previous_zoo_id, previous_zoo_id)
            if previous_key in config_zoo_by_key:
                previous_rows_by_config[previous_key].append(row)
        previous_canonical_rows = [
            rows[0]
            for rows in previous_rows_by_config.values()
            if rows
        ]
        previous_counts = {
            key: sum(_int(_first(row, field)) for row in previous_canonical_rows)
            for key, field in (
                ("discovered", "discovered"),
                ("parsed", "parsed"),
                ("inserted", "inserted"),
                ("updated", "updated"),
                ("failed", "failed"),
                ("duplicate_filtered", "duplicate_filtered"),
            )
        }
        run_evidence["relative_change"] = {
            "available": True,
            "reason": None,
            "previous_run_id": previous_id or None,
            "counts": {
                key: _int(run_counts.get(key)) - value
                for key, value in previous_counts.items()
            },
        }

        def _adapter_for_stat(row: Mapping[str, Any]) -> Optional[str]:
            """Read the adapter as it existed for the prior run when present."""

            source_id = _clean_text(_first(row, "source_id"))
            source_row = source_by_db_id.get(source_id, {})
            persisted_kind = canonical_adapter_kind(_first(source_row, "kind", "source_type"))
            explicit = _clean_text(_first(row, "adapter", "kind", "source_type"))
            if explicit:
                if persisted_kind == "custom_adapter" and canonical_adapter_kind(explicit) is None:
                    return "custom_adapter"
                return canonical_adapter_kind(explicit) or normalize_adapter_token(explicit)
            metadata = _json_object(_first(row, "metadata_json", "metadata"))
            explicit = _clean_text(_first(metadata, "adapter", "kind", "source_type"))
            if explicit:
                if persisted_kind == "custom_adapter" and canonical_adapter_kind(explicit) is None:
                    return "custom_adapter"
                return canonical_adapter_kind(explicit) or normalize_adapter_token(explicit)
            persisted = _clean_text(_first(source_row, "adapter", "kind", "source_type"))
            if persisted_kind == "custom_adapter":
                return "custom_adapter"
            return canonical_adapter_kind(persisted) or normalize_adapter_token(persisted) or None

        previous_stat_rows = [
            row for row in db_stats
            if _clean_text(_first(row, "crawl_run_id", "run_id")) == previous_id
        ]
        # The current side is configuration state, not merely the subset of
        # sources that happened to emit a stat row.  A failed/missing current
        # stat must not look like an adapter was removed.  Previous-run rows
        # are still source-stat evidence, with their source IDs mapped back
        # to configured kinds above.
        current_source_adapters = dict(source_adapter_by_id)
        current_adapter_counts: Counter[str] = Counter(current_source_adapters.values())
        previous_adapter_counts: Counter[str] = Counter()
        previous_source_adapters: Dict[str, Optional[str]] = {}
        for stat_row in previous_stat_rows:
            source_id = _clean_text(_first(stat_row, "source_id"))
            stat_adapter = _adapter_for_stat(stat_row)
            if source_id:
                previous_source_adapters[source_id] = stat_adapter
            if stat_adapter:
                previous_adapter_counts[stat_adapter] += 1
        adapter_keys = set(adapter_stats) | set(current_adapter_counts) | set(previous_adapter_counts)
        # A source without a stat row in the previous run is not proof that it
        # was added: a failed/interrupted source can simply have no row.  Keep
        # that distinction explicit so adapter deltas never claim more history
        # than the database proves.  When a prior stat exists, an ID absent
        # from it is useful added-source evidence; the overall evidence flag
        # still records any missing prior stat coverage.
        unknown_current_sources = sorted(
            set(current_source_adapters) - set(previous_source_adapters)
        )
        added_sources = list(unknown_current_sources)
        removed_sources = sorted(set(previous_source_adapters) - set(current_source_adapters))
        kind_changed = sorted(
            source_id
            for source_id in set(current_source_adapters) & set(previous_source_adapters)
            if previous_source_adapters[source_id]
            and current_source_adapters[source_id] != previous_source_adapters[source_id]
        )
        unknown_previous_sources = sorted(
            source_id for source_id, adapter in previous_source_adapters.items() if not adapter
        )
        adapter_changes = {
            "available": True,
            "reason": "previous source adapter evidence is incomplete" if (unknown_previous_sources or unknown_current_sources) else None,
            "evidence_complete": not (unknown_previous_sources or unknown_current_sources),
            "changes": {
                adapter: current_adapter_counts.get(adapter, 0) - previous_adapter_counts.get(adapter, 0)
                for adapter in sorted(adapter_keys)
            },
            "added_sources": added_sources,
            "removed_sources": removed_sources,
            "kind_changed_sources": kind_changed,
            "unknown_previous_sources": unknown_previous_sources,
            "unknown_current_sources": unknown_current_sources,
            "source_changes": {
                source_id: {
                    "previous": previous_source_adapters.get(source_id),
                    "current": current_source_adapters.get(source_id),
                }
                for source_id in sorted(
                    set(added_sources) | set(removed_sources) | set(kind_changed)
                )
            },
        }
    run_evidence["adapter_changes"] = adapter_changes
    if run_status in {"error", "failed", "failure", "partial", "completed_with_errors"}:
        known_issues.append("Latest crawl run is not fully successful: " + run_status)

    scope = _derive_roster_scope(bundle, enabled_zoos)
    scope_limitation = _clean_text(scope.get("limitation"))
    if scope_limitation:
        known_issues.append(scope_limitation)

    # Normalize known issue ordering so JSON, Markdown and HTML are stable.
    known_issues = sorted(set(_clean_text(item) for item in known_issues if _clean_text(item)), key=str.casefold)
    failures = sorted(set(failures), key=str.casefold)
    # ``failure_reason_counts`` remains the compatibility field; its values
    # are now stable classes rather than zoo names extracted from messages.
    failure_reason_counts: Dict[str, int] = {
        key: failure_class_counts[key] for key in sorted(failure_class_counts)
    }
    if failures and not failure_reason_counts:
        failure_reason_counts = dict(Counter(_failure_class(item) for item in failures))

    # Keep all failure reasons in the report for auditability, while deriving
    # acceptance from the stable class vocabulary.  External request limits
    # are evidence and warnings, not blockers; unknown/internal classes remain
    # conservative blockers.
    external_failure_class_counts = {
        key: count
        for key, count in failure_reason_counts.items()
        if key in EXTERNAL_FAILURE_CLASSES
    }
    blocking_failure_class_counts = {
        key: count
        for key, count in failure_reason_counts.items()
        if key not in NONBLOCKING_FAILURE_CLASSES
    }
    external_limited_zoo_ids = sorted(
        _clean_text(zoo.get("id"))
        for zoo in zoo_records
        if zoo.get("enabled") and zoo.get("external_limited")
    )
    external_limited_source_ids = sorted(
        _clean_text(source.get("id"))
        for source in all_source_records
        if source.get("enabled") and source.get("external_limited")
    )
    external_limited = bool(
        external_failure_class_counts
        or external_limited_zoo_ids
        or external_limited_source_ids
    )
    nonblocking_completion = any(
        zoo.get("enabled") and _clean_text(zoo.get("completion_category")) == "partial"
        for zoo in zoo_records
    )
    run_status_is_failure = run_status in {"error", "failed", "failure", "partial", "completed_with_errors"}
    run_status_blocking = bool(
        run_status_is_failure
        and not external_limited
        and not nonblocking_completion
    )

    blocking_quality_codes = sorted(
        {
            _clean_text(warning.get("code"))
            for article in article_records
            for warning in article.get("warnings", [])
            if _clean_text(warning.get("code")) in _QUALITY_BLOCKING_CODES
        }
    )
    duplicate_identity_count = sum(max(0, count - 1) for count in identity_counts.values())
    duplicate_url_count = sum(max(0, count - 1) for count in url_counts.values())
    projection_safe = all(
        "raw_html" not in article and "metadata_json" not in article
        for article in article_records
    )
    criteria = [
        {
            "id": "configuration_loaded",
            "label": "Configuration loaded",
            "passed": bundle.error is None,
            "detail": "The configured zoo registry is readable." if bundle.error is None else bundle.error,
        },
        {
            "id": "configured_zoos_projected",
            "label": "Configured zoos projected",
            "passed": len(zoo_records) == len(bundle.zoos),
            "detail": f"{len(zoo_records)} of {len(bundle.zoos)} configured zoos are present.",
        },
        {
            "id": "supported_adapters",
            "label": "Source adapters supported",
            "passed": all(_canonical_source_adapter(source) in KNOWN_ADAPTERS for source in bundle.sources),
            "detail": (
                "All configured source kinds are known; explicit unsupported entries are external limitations."
                if all(_canonical_source_adapter(source) in KNOWN_ADAPTERS for source in bundle.sources)
                else "At least one configured source kind is unknown."
            ),
        },
        {
            "id": "run_evidence",
            "label": "Crawl run evidence",
            "passed": run_row is not None,
            "detail": "A latest crawl run is available." if run_row is not None else "No crawl run is available.",
        },
        {
            "id": "source_coverage",
            "label": "Enabled source coverage",
            "passed": all(source["registered"] for source in enabled_sources),
            "detail": (
                f"{sum(1 for source in enabled_sources if source['registered'])} of {len(enabled_sources)} enabled source checks are registered; "
                f"{sum(1 for source in article_capable_enabled_sources if source['registered'])} of {len(article_capable_enabled_sources)} "
                f"article-capable executable sources and {sum(1 for source in health_only_enabled_sources if source['registered'])} of "
                f"{len(health_only_enabled_sources)} health-only registry_only sources are covered."
            ),
        },
        {
            "id": "latest_run_zoo_result_coverage",
            "label": "Latest run has exactly one result per enabled zoo",
            "passed": bool(run_row is not None and result_coverage_count == len(enabled_zoos) and not result_missing_keys and not result_duplicate_keys),
            "detail": (
                f"{result_coverage_count} of {len(enabled_zoos)} enabled zoos have exactly one latest-run result."
                if run_row is not None
                else "No latest crawl run is available."
            ),
        },
        {
            "id": "configured_registry_scope",
            "label": "Acceptance scope is the configured registry",
            "passed": bool(
                scope.get("configured_zoos") == len(bundle.zoos)
                and (
                    scope.get("kind") == "configured_registry"
                    or scope.get("expanded_roster_available") is True
                )
            ),
            "detail": (
                "The configured entries carry a matching authoritative expanded-roster provenance."
                if scope.get("expanded_roster_available") is True
                else scope_limitation
            ),
        },
        {
            "id": "no_run_failures",
            "label": "No run failures",
            "passed": not blocking_failure_class_counts and not run_status_blocking,
            "detail": (
                "No blocking source/run failures were recorded."
                if not blocking_failure_class_counts and not run_status_blocking
                else (
                    f"{sum(blocking_failure_class_counts.values())} blocking failure reason(s) are recorded."
                    if blocking_failure_class_counts
                    else f"Latest crawl run has blocking status {run_status}."
                )
            )
            + (
                f" {sum(external_failure_class_counts.values())} external limitation failure reason(s) remain visible as non-blocking evidence."
                if external_failure_class_counts
                else ""
            ),
        },
        {
            "id": "article_quality_rules",
            "label": "Article quality rules",
            "passed": not blocking_quality_codes,
            "detail": (
                "No blocking article-quality findings were recorded; long_title remains a warning."
                if not blocking_quality_codes
                else "Blocking article-quality findings: " + ", ".join(blocking_quality_codes)
            ),
        },
        {
            "id": "deduplication_integrity",
            "label": "Deduplication integrity",
            "passed": duplicate_identity_count == 0 and duplicate_url_count == 0,
            "detail": (
                f"{duplicate_identity_count} duplicate content identities and {duplicate_url_count} duplicate normalized URLs."
            ),
        },
        {
            "id": "article_url_safety",
            "label": "Article URL policy evidence",
            "passed": not any(code in blocking_quality_codes for code in {"bad_url", "non_official_host"}),
            "detail": (
                "All projected article URLs are valid and official for their configured zoo."
                if not any(code in blocking_quality_codes for code in {"bad_url", "non_official_host"})
                else "Invalid or non-official article URLs were found."
            ),
        },
        {
            "id": "safe_projection",
            "label": "Raw HTML and unselected metadata excluded",
            "passed": projection_safe,
            "detail": "Only selected, escaped evidence fields are projected." if projection_safe else "A forbidden raw field was found in the projection.",
        },
        {
            "id": "cross_format_contract",
            "label": "Cross-format report contract",
            "passed": bool(
                scope
                and set(completion_classifications) == set(PUBLIC_COMPLETION_CLASSIFICATIONS)
                and all("completion_classification" in zoo for zoo in zoo_records)
                and all("completion_category" in zoo for zoo in zoo_records)
            ),
            "detail": "JSON, Markdown, CSV and HTML are generated from one projection contract.",
        },
    ]
    # Empty/missing databases are intentionally represented as incomplete,
    # not as a false PASS.  This lets automation distinguish a healthy crawl
    # from a valid but not-yet-populated report.
    end_criteria_met = bool(criteria) and all(bool(item["passed"]) for item in criteria)
    acceptance_status = "PASS" if end_criteria_met else ("INCOMPLETE" if not snapshot.available or not run_row else "FAIL")
    git_root = (
        Path(bundle.path).resolve().parent
        if bundle.path not in {"<config>", "<mapping>"}
        else None
    )
    git_identity = _git_identity(git_root)
    reported_commit = _clean_text(git_commit) or _clean_text(git_identity.get("commit")) or "unknown"
    run_evidence["scope"] = scope
    run_evidence["failure_class_counts"] = dict(failure_reason_counts)
    run_evidence["external_limited"] = external_limited
    run_evidence["external_failure_class_counts"] = dict(external_failure_class_counts)
    run_evidence["blocking_failure_class_counts"] = dict(blocking_failure_class_counts)
    run_evidence["external_limited_zoo_ids"] = external_limited_zoo_ids
    run_evidence["external_limited_source_ids"] = external_limited_source_ids
    run_evidence["run_status_blocking"] = run_status_blocking
    overview = {
        "unique": len(article_records),
        "total_enabled": len(enabled_zoos),
        **{category: completion_categories.get(category, 0) for category in COMPLETION_CATEGORIES},
        "completion_classifications": completion_classifications,
        "discovered": _int(run_counts.get("discovered")),
        "parsed": _int(run_counts.get("parsed")),
        "inserted": _int(run_counts.get("inserted")),
        "updated": _int(run_counts.get("updated")),
        "failed": _int(run_counts.get("failed")),
        "duplicate_filtered": _int(run_counts.get("duplicate_filtered")),
        "parse_failures": _int(run_counts.get("parse_failures")),
        "total_duration_ms": _int(run_counts.get("total_duration_ms")),
        "wall_duration_ms": _int(run_counts.get("wall_duration_ms")),
        "zoo_duration_ms": _int(run_counts.get("zoo_duration_ms")),
        "source_article_associations": sum(source_article_counts.values()),
    }
    projection: Dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _as_utc_iso(clock),
        "git_commit": reported_commit,
        "git_dirty": git_identity.get("dirty"),
        "code_state_fingerprint": git_identity.get("tree_fingerprint"),
        "git_identity": {
            "commit": reported_commit,
            "head_commit": git_identity.get("commit"),
            "dirty": git_identity.get("dirty"),
            "tree_fingerprint": git_identity.get("tree_fingerprint"),
            "status": git_identity.get("status", []),
        },
        "scope": scope,
        "config": {
            "path": bundle.path,
            "zoo_count": len(bundle.zoos),
            "enabled_zoo_count": len(enabled_zoos),
            "source_count": len(bundle.sources),
            "enabled_source_count": len(enabled_sources),
            "sha256": _config_digest(bundle.path),
        },
        "database": {
            "available": snapshot.available,
            "path": _database_label(db),
            "schema_version": snapshot.user_version,
            "legacy_schema": _database_legacy(snapshot),
            "tables": sorted(key for key, columns in snapshot.columns.items() if columns),
            "error": snapshot.error,
        },
        "run": run_evidence,
        "run_summary": run_evidence,
        "coverage": coverage,
        "counts": {
            "zoos": len(zoo_records),
            "sources": len(all_source_records),
            "articles": len(article_records),
            "unique_articles": len(article_records),
            "source_article_associations": sum(source_article_counts.values()),
            "duplicate_url_count": duplicate_url_count,
            "duplicate_identity_count": duplicate_identity_count,
            "total_enabled": len(enabled_zoos),
            "warnings": warning_count,
            "failures": len(failures),
        },
        "run_metrics": run_counts,
        "overview": overview,
        "categories": categories,
        "completion_categories": completion_categories,
        "completion_classifications": completion_classifications,
        "public_completion_classifications": list(PUBLIC_COMPLETION_CLASSIFICATIONS),
        "limitation_categories": limitation_categories,
        "limitation_class_counts": limitation_class_counts,
        "limitations": limitations,
        "external_limited": external_limited,
        "external_failure_class_counts": {
            key: external_failure_class_counts[key]
            for key in sorted(external_failure_class_counts)
        },
        "blocking_failure_class_counts": {
            key: blocking_failure_class_counts[key]
            for key in sorted(blocking_failure_class_counts)
        },
        "external_limited_zoo_ids": external_limited_zoo_ids,
        "external_limited_source_ids": external_limited_source_ids,
        "quality_warning_categories": {key: quality_warning_categories[key] for key in sorted(quality_warning_categories)},
        "supported_categories": list(SUPPORTED_ADAPTERS),
        "known_adapter_categories": list(KNOWN_ADAPTERS),
        "adapter_stats": {key: adapter_stats[key] for key in sorted(adapter_stats)},
        "failure_reasons": failures,
        "failure_reason_counts": {key: failure_reason_counts[key] for key in sorted(failure_reason_counts)},
        "failure_class_counts": {key: failure_reason_counts[key] for key in sorted(failure_reason_counts)},
        "quality": {
            "warning_categories": {key: quality_warning_categories[key] for key in sorted(quality_warning_categories)},
            "blocking_codes": blocking_quality_codes,
            "long_title_is_warning": True,
            "deduplication": {
                "duplicate_url_count": duplicate_url_count,
                "duplicate_identity_count": duplicate_identity_count,
            },
            "projection_safe": projection_safe,
        },
        "known_issues": known_issues,
        "acceptance": {
            "status": acceptance_status,
            "passed": end_criteria_met,
            "end_criteria_met": end_criteria_met,
            "external_limited": external_limited,
            "criteria": criteria,
        },
        "end_criteria": {
            "status": acceptance_status,
            "passed": end_criteria_met,
            "checks": criteria,
        },
        "zoos": zoo_records,
        "articles": article_records,
    }
    _close_snapshot(snapshot)
    return projection


def _database_label(db: Any) -> str:
    if isinstance(db, sqlite3.Connection):
        return "<connection>"
    if db is not None and hasattr(db, "path"):
        try:
            return os.fspath(getattr(db, "path"))
        except (TypeError, OSError):
            return "<database>"
    if db is None:
        return ":memory:"
    try:
        return os.fspath(db)
    except (TypeError, OSError):
        return "<database>"


def _config_digest(path: str) -> Optional[str]:
    if path.startswith("<"):
        return None
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def _json_projection(projection: Mapping[str, Any]) -> str:
    return json.dumps(projection, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n"


def _md_escape(value: Any) -> str:
    """Escape dynamic Markdown/HTML punctuation while preserving readability."""

    text = _clean_text(value)
    return (
        text.replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("#", "\\#")
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def _md_code(value: Any) -> str:
    # Code spans do not interpret underscores or brackets, so preserve stable
    # machine-facing labels such as ``SUPPORTED_NO_CURRENT_NEWS`` literally.
    text = _clean_text(value).replace("\\", "\\\\").replace("`", "\\`")
    return "`" + text + "`"


def _articles_payload(projection: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema_version": projection.get("schema_version", 1),
        "generation_id": projection.get("generation_id"),
        "generated_at": projection.get("generated_at"),
        "git_commit": projection.get("git_commit"),
        "git_dirty": projection.get("git_dirty"),
        "code_state_fingerprint": projection.get("code_state_fingerprint"),
        "scope": projection.get("scope", {}),
        "acceptance": projection.get("acceptance", {}),
        "counts": projection.get("counts", {}),
        "completion_classifications": projection.get("completion_classifications", {}),
        "articles": projection.get("articles", []),
    }


def _markdown(projection: Mapping[str, Any]) -> str:
    config = projection.get("config", {})
    database = projection.get("database", {})
    run = projection.get("run", {})
    coverage = projection.get("coverage", {})
    acceptance = projection.get("acceptance", {})
    metrics = projection.get("run_metrics", {}) or {}
    scope = projection.get("scope", {}) or {}
    if scope.get("expanded_roster_available") is True:
        expanded_scope_line = (
            f"- Expanded roster: **available** ({_md_escape(scope.get('roster_version') or 'version unknown')}); "
            f"configured zoos={_int(scope.get('configured_zoos'))}, expanded zoos={_int(scope.get('expanded_zoos'))}; "
            f"SHA-256={_md_code(scope.get('roster_sha256') or 'unknown')}."
        )
    else:
        expanded_scope_line = (
            f"- Expanded roster: {_md_escape(scope.get('expanded_roster_status') or 'not_provided')}; "
            "no expanded roster is implied by this report."
        )
    lines = [
        "# ZooFanCrawler run summary",
        "",
        f"- Acceptance: **{_md_escape(acceptance.get('status') or 'INCOMPLETE')}**",
        f"- Generation: {_md_code(projection.get('generation_id'))}",
        f"- Generated: {_md_code(projection.get('generated_at'))}",
        f"- Git commit: {_md_code(projection.get('git_commit') or 'unknown')}",
        f"- Git tree state: {_md_code('dirty' if projection.get('git_dirty') else 'clean/unknown')}; code-state fingerprint: {_md_code(projection.get('code_state_fingerprint') or 'unknown')}",
        f"- Configuration: {_md_code(config.get('path'))} ({_int(config.get('zoo_count'))} zoos, {_int(config.get('source_count'))} sources)",
        f"- Database: {_md_code(database.get('path'))} (available={bool(database.get('available'))}, schema={_int(database.get('schema_version'))})",
        "",
        "## Acceptance scope",
        "",
        f"- Scope: **{_md_escape(scope.get('label') or 'Configured registry only')}**; configured zoos={_int(scope.get('configured_zoos'))}, enabled zoos={_int(scope.get('enabled_zoos'))}.",
        expanded_scope_line,
        f"- Limitation: {_md_escape(scope.get('limitation') or ('No expanded-roster limitation recorded.' if scope.get('expanded_roster_available') else 'No expanded roster provenance was supplied.'))}",
        "- Roster provenance: " + (_md_code(json.dumps(scope.get("roster_provenance", {}), ensure_ascii=False, sort_keys=True)) if scope.get("roster_provenance") else "not supplied."),
        "",
        "## Coverage and run counts",
        "",
        f"Configured zoos: {_int(coverage.get('configured_zoos'))}; enabled zoos: {_int(coverage.get('enabled_zoos'))}; unique articles (cumulative DB): {_int(coverage.get('unique_articles', coverage.get('articles')))}; source/article associations: {_int(coverage.get('source_article_associations'))}",
        f"Registered sources: {_int(coverage.get('registered_sources'))}/{_int(coverage.get('enabled_sources'))} ({coverage.get('sources_registered_percent', 0)}%); latest-run zoo-result coverage: {_md_escape(str(coverage.get('latest_run_zoo_result_coverage_percent', 0)))}%; quality warnings: {_int(coverage.get('quality_warnings'))}",
        f"Source checks: {_int(coverage.get('enabled_source_checks', coverage.get('enabled_sources')))} enabled; article-capable executable: {_int(coverage.get('article_capable_executable_enabled_sources', coverage.get('executable_enabled_sources')))}; health-only registry_only: {_int(coverage.get('health_only_registry_only_enabled_sources', coverage.get('health_only_enabled_sources')))}",
        f"External-limited evidence: **{bool(projection.get('external_limited'))}**; external failure reasons retained: {_int(sum((projection.get('external_failure_class_counts', {}) or {}).values()))}",
        f"Latest run: {_md_code(run.get('status') or 'no_run')}; start={_md_code(run.get('started_at'))}, end={_md_code(run.get('finished_at'))}, wall duration={_int(run.get('wall_duration_ms', run.get('duration_ms')))} ms; summed per-zoo duration={_int(run.get('zoo_duration_ms'))} ms",
        f"Latest-run totals: discovered={_int(metrics.get('discovered'))}, fetched={_int(metrics.get('fetched'))}, parsed={_int(metrics.get('parsed'))}, stored={_int(metrics.get('stored'))}, inserted={_int(metrics.get('inserted'))}, updated={_int(metrics.get('updated'))}, already_known={_int(metrics.get('already_known'))}, failed={_int(metrics.get('failed'))}, duplicate_filtered={_int(metrics.get('duplicate_filtered'))}, errors={_int(metrics.get('errors'))}, wall_duration_ms={_int(metrics.get('wall_duration_ms'))}",
        f"Latest-run result rows: raw={_int(coverage.get('raw_zoo_results'))}; canonical unique={_int(coverage.get('zoo_results'))}; duplicate rows excluded={_int(coverage.get('duplicate_zoo_results'))}",
        "",
        "## Supported source categories",
        "",
    ]
    categories = projection.get("categories", {})
    supported = categories.get("supported", []) if isinstance(categories, Mapping) else []
    lines.append(", ".join(_md_code(item) for item in supported) or "None")
    if isinstance(categories, Mapping) and categories.get("error_categories"):
        lines.append("Error categories observed: " + ", ".join(_md_code(item) for item in categories.get("error_categories", [])))
    lines.extend(["", "## Completion classifications (configured enabled zoos)", ""])
    public_categories = projection.get("completion_classifications", {}) or {}
    for key in PUBLIC_COMPLETION_CLASSIFICATIONS:
        lines.append(f"- {_md_code(key)}: {_int(public_categories.get(key))}")
    lines.extend(["", "Raw/internal completion categories (compatibility evidence):", ""])
    lines.extend(f"- {_md_code(key)}: {_int(value)}" for key, value in (projection.get("completion_categories", {}) or {}).items())
    lines.extend(["", "## External limitations (non-blocking)", ""])
    limitations = projection.get("limitation_class_counts", {}) or {}
    if limitations:
        lines.extend(f"- {_md_code(key)}: {_int(value)}" for key, value in limitations.items())
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Quality warning categories", ""])
    warning_categories = projection.get("quality_warning_categories", {}) or {}
    if warning_categories:
        lines.extend(f"- {_md_code(key)}: {_int(value)}" for key, value in warning_categories.items())
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Adapter statistics", "", "| Adapter | Configured | Enabled | Registered | Success | Errors | Associations | Unique articles |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
    for adapter, stats in (projection.get("adapter_stats", {}) or {}).items():
        lines.append(f"| {_md_escape(adapter)} | {_int(stats.get('configured_count'))} | {_int(stats.get('enabled_count'))} | {_int(stats.get('registered_count'))} | {_int(stats.get('success_count'))} | {_int(stats.get('error_count'))} | {_int(stats.get('article_association_count', stats.get('article_count')))} | {_int(stats.get('unique_article_count'))} |")
    adapter_changes = run.get("adapter_changes", {}) or {}
    lines.extend(["", "## Adapter changes", ""])
    if adapter_changes.get("available"):
        lines.append(f"Evidence complete: **{bool(adapter_changes.get('evidence_complete'))}**; {_md_escape(adapter_changes.get('reason') or 'all compared source adapter evidence is present.')}")
        lines.append("- Kind/count deltas: " + _md_code(json.dumps(adapter_changes.get("changes", {}), ensure_ascii=False, sort_keys=True)))
        lines.append("- Added sources: " + (_md_code(", ".join(adapter_changes.get("added_sources", []) or [])) if adapter_changes.get("added_sources") else "none"))
        lines.append("- Removed sources: " + (_md_code(", ".join(adapter_changes.get("removed_sources", []) or [])) if adapter_changes.get("removed_sources") else "none"))
        lines.append("- Kind-changed sources: " + (_md_code(", ".join(adapter_changes.get("kind_changed_sources", []) or [])) if adapter_changes.get("kind_changed_sources") else "none"))
        if adapter_changes.get("unknown_current_sources") or adapter_changes.get("unknown_previous_sources"):
            lines.append("- Unknown adapter evidence: " + _md_code(json.dumps({"current": adapter_changes.get("unknown_current_sources", []), "previous": adapter_changes.get("unknown_previous_sources", [])}, ensure_ascii=False, sort_keys=True)))
    else:
        lines.append("Unknown: no previous run history was available.")
    quality = projection.get("quality", {}) or {}
    dedup = quality.get("deduplication", {}) or {}
    lines.extend(["", "## Quality, URL and security criteria", ""])
    lines.append(f"- Blocking quality codes: {_md_code(', '.join(quality.get('blocking_codes', []) or []) or 'none')}; long_title is warning-only: **{bool(quality.get('long_title_is_warning', True))}**")
    lines.append(f"- Duplicate normalized URLs: {_int(dedup.get('duplicate_url_count'))}; duplicate content identities: {_int(dedup.get('duplicate_identity_count'))}")
    lines.append(f"- Projection security: raw_html/metadata_json excluded: **{bool(quality.get('projection_safe'))}**")
    lines.extend(["", "## Failure classes", ""])
    failure_counts = projection.get("failure_class_counts", projection.get("failure_reason_counts", {})) or {}
    lines.extend(f"- {_md_code(key)}: {_int(value)}" for key, value in failure_counts.items()) if failure_counts else lines.append("None recorded.")
    lines.extend(["", "## Failure reasons", ""])
    failures = projection.get("failure_reasons", []) or []
    lines.extend(f"- {_md_escape(item)}" for item in failures) if failures else lines.append("None recorded.")
    lines.extend(["", "## Known issues", ""])
    issues = projection.get("known_issues", []) or []
    lines.extend(f"- {_md_escape(item)}" for item in issues) if issues else lines.append("None recorded.")
    lines.extend(["", "## Relative change", ""])
    relative = run.get("relative_change", {}) or {}
    if relative.get("available"):
        lines.append("Previous run: " + _md_code(relative.get("previous_run_id")))
        lines.extend(f"- {_md_code(key)}: {_int(value):+d}" for key, value in (relative.get("counts", {}) or {}).items())
    else:
        lines.append("Unknown: no previous run history was available.")
    lines.extend(["", "## End criteria", "", f"Overall: **{_md_escape(acceptance.get('status') or 'INCOMPLETE')}**", ""])
    lines.extend(f"- [{'x' if item.get('passed') else ' '}] {_md_escape(item.get('label'))}: {_md_escape(item.get('detail'))}" for item in (acceptance.get("criteria", []) or []))
    lines.extend(
        [
            "",
            "## Zoo status",
            "",
            "| Name | Country | Region | Groups | Official site | News source URL | Adapter | Completion | Run status | HTTP | Discovered | Parsed | Inserted | Failed | Latest errors | Latest news date | Duration (ms) | Error summary |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | --- |",
        ]
    )
    for zoo in projection.get("zoos", []) or []:
        counts = zoo.get("counts", {})
        source_urls = "; ".join(_text(item) for item in (zoo.get("news_source_urls", []) or []))
        latest_errors = "; ".join(_text(item) for item in (zoo.get("latest_errors", []) or []))
        lines.append(
            f"| {_md_escape(zoo.get('name'))} | {_md_escape(zoo.get('country_code'))} | {_md_escape(zoo.get('region'))} | {_md_escape('; '.join(zoo.get('groups', []) or []))} | {_md_code(zoo.get('website_url') or '')} | {_md_code(source_urls)} | {_md_code(zoo.get('adapter') or zoo.get('adapter_kind') or '')} | {_md_escape(zoo.get('completion_classification'))} | {_md_escape(zoo.get('run_status') or zoo.get('status_raw') or zoo.get('status'))} | {_md_escape(zoo.get('http_status'))} | {_int(counts.get('discovered'))} | {_int(counts.get('parsed'))} | {_int(counts.get('inserted'))} | {_int(counts.get('failed'))} | {_md_escape(latest_errors)} | {_md_escape(zoo.get('latest_news_date'))} | {_int(zoo.get('duration_ms', counts.get('duration_ms')))} | {_md_escape(zoo.get('error_summary'))} |"
        )
    lines.extend(["", "## Recent articles", ""])
    recent_count = 0
    for zoo in projection.get("zoos", []) or []:
        recent = (zoo.get("recent_articles", []) or [])[:3]
        if recent:
            lines.append(f"### {_md_escape(zoo.get('name') or zoo.get('id'))}")
        for article in recent:
            recent_count += 1
            title = _text(article.get("title")) or "(untitled)"
            lines.append(f"- {_md_escape(title)} — {_md_escape(article.get('published_at') or 'undated')} — {_md_code(article.get('url') or '')}")
    if not recent_count:
        lines.append("None recorded.")
    return "\n".join(lines) + "\n"


def _csv_status(projection: Mapping[str, Any]) -> str:
    output = io.StringIO(newline="")
    fields = [
        "generation_id", "scope", "acceptance_status", "git_commit", "git_dirty", "code_state_fingerprint",
        "expanded_roster_available", "expanded_roster_status", "configured_zoo_count", "enabled_zoo_count", "expanded_zoo_count",
        "roster_source", "roster_version", "roster_sha256",
        "zoo_id", "slug", "name", "website", "country_code", "language", "region", "city", "groups", "enabled",
        "completion_classification", "completion_category", "source_status", "status", "status_raw", "result_issue", "zoo_result_count",
        "official_site", "news_source_url", "source", "adapter", "adapter_kind", "http_status", "configured_sources", "registered_sources", "source_article_associations", "unique_article_count", "article_count",
        "discovered", "fetched", "parsed", "stored", "inserted", "updated", "failed", "errors", "duplicate_filtered", "already_known",
        "warning_count", "quality_codes", "failure_classes", "external_limited", "latest_date", "latest_news_date", "duration_ms", "wall_duration_ms", "error_category", "summary", "error_summary", "latest_errors", "run_status",
        "adapter_changes", "relative_change", "evidence_path",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    generation_id = _text(projection.get("generation_id"))
    scope = projection.get("scope", {}) or {}
    run = projection.get("run", {}) or {}
    acceptance = projection.get("acceptance", {}) or {}
    adapter_changes = json.dumps(run.get("adapter_changes", {}) or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    relative_change = json.dumps(run.get("relative_change", {}) or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for zoo in projection.get("zoos", []) or []:
        counts = zoo.get("counts", {}) or {}
        sources = zoo.get("sources", []) or []
        source_values = [
            _clean_text(source.get("url") or source.get("name"))
            for source in sources
            if _clean_text(source.get("url") or source.get("name"))
        ]
        adapter_values = sorted({_clean_text(source.get("adapter") or source.get("kind")) for source in sources if _clean_text(source.get("adapter") or source.get("kind"))})
        adapter_kind_values = sorted({_clean_text(source.get("adapter_kind")) for source in sources if _clean_text(source.get("adapter_kind"))})
        dates = sorted(
            _clean_text(article.get("published_at"))
            for article in (zoo.get("recent_articles", []) or [])
            if _clean_text(article.get("published_at"))
        )
        # ``status`` is the normalized per-zoo status from the projection;
        # raw result/run status remains available in dedicated columns.
        row_status = _text(zoo.get("status")) or "unknown"
        writer.writerow(
            {
                "generation_id": generation_id,
                "scope": _text(scope.get("label") or "Configured registry only"),
                "acceptance_status": _text(acceptance.get("status") or "INCOMPLETE"),
                "git_commit": _text(projection.get("git_commit")),
                "git_dirty": "true" if projection.get("git_dirty") is True else ("false" if projection.get("git_dirty") is False else "unknown"),
                "code_state_fingerprint": _text(projection.get("code_state_fingerprint")),
                "expanded_roster_available": "true" if scope.get("expanded_roster_available") is True else "false",
                "expanded_roster_status": _text(scope.get("expanded_roster_status")),
                "configured_zoo_count": _int(scope.get("configured_zoos")),
                "enabled_zoo_count": _int(scope.get("enabled_zoos")),
                "expanded_zoo_count": _int(scope.get("expanded_zoos")),
                "roster_source": _text(scope.get("roster_source")),
                "roster_version": _text(scope.get("roster_version")),
                "roster_sha256": _text(scope.get("roster_sha256")),
                "zoo_id": _text(zoo.get("id")),
                "slug": _text(zoo.get("slug")),
                "name": _text(zoo.get("name")),
                "website": _text(zoo.get("website_url")),
                "official_site": _text(zoo.get("official_site_url") or zoo.get("website_url")),
                "news_source_url": ";".join(_text(item) for item in (zoo.get("news_source_urls", []) or [])),
                "country_code": _text(zoo.get("country_code")),
                "language": _text(zoo.get("language")),
                "region": _text(zoo.get("region")),
                "city": _text(zoo.get("city")),
                "groups": ";".join(_text(item) for item in (zoo.get("groups", []) or [])),
                "enabled": "true" if zoo.get("enabled") else "false",
                # Keep operational run source status distinct from a true
                # error category; configured registry status is the fallback
                # only when no latest-run result exists.
                "source_status": _text(zoo.get("run_source_status") or zoo.get("source_status")),
                "completion_classification": _text(zoo.get("completion_classification") or "FAILED"),
                "completion_category": _text(zoo.get("completion_category") or "failed"),
                "status": row_status,
                "status_raw": _text(zoo.get("status_raw") or row_status),
                "result_issue": _text(zoo.get("result_issue")),
                "zoo_result_count": _int(zoo.get("zoo_result_count")),
                "source": ";".join(source_values),
                "adapter": ";".join(adapter_values),
                "adapter_kind": ";".join(adapter_kind_values) or _text(zoo.get("adapter_kind")),
                "http_status": _first(zoo, "http_status"),
                "configured_sources": _int(counts.get("configured_sources")),
                "registered_sources": _int(counts.get("registered_sources")),
                "source_article_associations": _int(counts.get("source_article_associations")),
                "unique_article_count": _int(counts.get("unique_articles", counts.get("articles"))),
                "article_count": _int(counts.get("unique_articles", counts.get("articles"))),
                "discovered": _int(counts.get("discovered")),
                "fetched": _int(counts.get("fetched")),
                "parsed": _int(counts.get("parsed")),
                "stored": _int(counts.get("stored")),
                "inserted": _int(counts.get("inserted")),
                "updated": _int(counts.get("updated")),
                "failed": _int(counts.get("failed")),
                "errors": _int(counts.get("errors")),
                "duplicate_filtered": _int(counts.get("duplicate_filtered")),
                "already_known": _int(counts.get("already_known")),
                "warning_count": _int(counts.get("warnings")),
                "quality_codes": ";".join(_text(item) for item in (zoo.get("warning_codes", []) or [])),
                "failure_classes": ";".join(_text(item) for item in (zoo.get("failure_classes", []) or [])),
                "external_limited": "true" if zoo.get("external_limited") else "false",
                "latest_date": dates[-1] if dates else "",
                "latest_news_date": _text(zoo.get("latest_news_date")) or (dates[-1] if dates else ""),
                "duration_ms": _int(zoo.get("duration_ms", counts.get("duration_ms"))),
                "wall_duration_ms": _int(run.get("wall_duration_ms", run.get("duration_ms"))),
                "error_category": _text(zoo.get("run_error_category")),
                "summary": _text(zoo.get("run_error") or zoo.get("error_summary")),
                "error_summary": _text(zoo.get("error_summary")),
                "latest_errors": ";".join(_text(item) for item in (zoo.get("latest_errors", []) or [])),
                "run_status": _text(zoo.get("run_status") or run.get("status"), "no_run"),
                "adapter_changes": adapter_changes,
                "relative_change": relative_change,
                "evidence_path": _text(zoo.get("evidence_path")),
            }
        )
    return output.getvalue()


def _html_projection_json(projection: Mapping[str, Any]) -> str:
    # A script element terminates on a literal '<', even when it appears in a
    # JSON string.  Unicode escapes preserve the value while preventing an
    # untrusted title/URL from becoming markup or executable script.
    return (
        json.dumps(projection, ensure_ascii=False, separators=(",", ":"), default=str)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _html(projection: Mapping[str, Any]) -> str:
    template_path = Path(__file__).with_name("templates") / "static_acceptance.html"
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError:
        template = _FALLBACK_HTML
    rendered = template.replace("__PROJECTION_JSON__", _html_projection_json(projection)).replace(
        "__GENERATION_ID__", html.escape(_text(projection.get("generation_id")), quote=True)
    )
    # Keep the legacy banner text byte-for-byte for configured-registry
    # reports (the endgoal smoke contract checks it), while making an
    # authoritative Phase0 projection truthful in the browser as well as in
    # the embedded JSON.
    scope = projection.get("scope", {})
    if isinstance(scope, Mapping) and scope.get("expanded_roster_available") is True:
        rendered = rendered.replace(
            ' + " · no expanded roster is implied."));',
            ' + " · " + (projection.scope.expanded_roster_available ? ("expanded roster available (" + String(projection.scope.expanded_zoos || 0) + " zoos).") : "no expanded roster is implied.")));',
        )
    return rendered


def _atomic_write(path: Path, content: Union[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, mode, encoding=None if mode == "wb" else "utf-8", newline="" if mode == "w" else None) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _content_bytes(content: Union[str, bytes]) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _restore_file(path: Path, content: Optional[bytes]) -> None:
    """Restore one known target; used only for an interrupted generation."""

    if content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    # Use the same atomic primitive in normal operation, but retain a direct
    # fallback so a fault injected into os.replace cannot leave a half report.
    try:
        _atomic_write(path, content)
    except BaseException:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _manifest_content(projection: Mapping[str, Any], files: Mapping[str, Union[str, bytes]]) -> str:
    manifest = {
        "schema_version": projection.get("schema_version", 1),
        "generation_id": projection.get("generation_id"),
        "generated_at": projection.get("generated_at"),
        "git_commit": projection.get("git_commit"),
        "git_dirty": projection.get("git_dirty"),
        "code_state_fingerprint": projection.get("code_state_fingerprint"),
        "scope": projection.get("scope", {}),
        "files": {
            name: {
                "sha256": hashlib.sha256(_content_bytes(content)).hexdigest(),
                "bytes": len(_content_bytes(content)),
            }
            for name, content in files.items()
        },
    }
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


@dataclass
class ReportBuild:
    """Result of :func:`build_reports`, with mapping-like path access."""

    projection: Dict[str, Any]
    paths: Dict[str, Path]
    output_directory: Optional[Path] = None

    @property
    def output_dir(self) -> Path:
        return self.output_directory or next(iter(self.paths.values())).parent

    @property
    def files(self) -> Dict[str, Path]:
        return self.paths

    def __getitem__(self, key: str) -> Any:
        if key == "projection":
            return self.projection
        if key == "output_dir":
            return self.output_dir
        if key == "files":
            return self.paths
        return self.paths[key]

    def get(self, key: str, default: Any = None) -> Any:
        if key == "projection":
            return self.projection
        if key == "output_dir":
            return self.output_dir
        if key == "files":
            return self.paths
        return self.paths.get(key, default)

    def keys(self) -> Iterable[str]:
        return self.paths.keys()

    def items(self) -> Iterable[Tuple[str, Path]]:
        return self.paths.items()

    def __iter__(self) -> Iterable[str]:
        return iter(self.paths)

    def __len__(self) -> int:
        return len(self.paths)

    def __contains__(self, key: object) -> bool:
        return key in self.paths or key in {"projection", "output_dir", "files"}


def _resolve_output_dir(output_dir: Any) -> Path:
    root = Path(output_dir or "reports")
    return root if root.name == "latest" else root / "latest"


def build_reports(
    config: Any = None,
    db: Any = None,
    output_dir: Any = "reports",
    *,
    config_path: Any = None,
    reports_dir: Any = None,
    report_dir: Any = None,
    output_root: Any = None,
    reports_root: Any = None,
    db_path: Any = None,
    now: Any = None,
    git_commit: Optional[str] = None,
) -> ReportBuild:
    """Generate all five explicit report files from one projection.

    ``reports_dir`` is an alias for ``output_dir``.  The target is normally
    ``<reports_dir>/latest``; passing a path named ``latest`` writes directly
    there.  Existing unrelated files are preserved.
    """

    if reports_dir is not None:
        output_dir = reports_dir
    if report_dir is not None:
        output_dir = report_dir
    if output_root is not None:
        output_dir = output_root
    if reports_root is not None:
        output_dir = reports_root
    projection = build_projection(config, db, config_path=config_path, db_path=db_path, now=now, git_commit=git_commit)
    # All artifacts in one report generation carry the same immutable token.
    projection = dict(projection)
    projection["generation_id"] = uuid.uuid4().hex
    target = _resolve_output_dir(output_dir)
    files: Dict[str, Union[str, bytes]] = {
        "run-summary.json": _json_projection(projection),
        "run-summary.md": _markdown(projection),
        "zoo-status.csv": _csv_status(projection),
        "articles.json": json.dumps(_articles_payload(projection), ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        "acceptance/index.html": _html(projection),
    }
    manifest_name = "manifest.json"
    manifest = _manifest_content(projection, files)
    all_files: Dict[str, Union[str, bytes]] = dict(files)
    all_files[manifest_name] = manifest
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".latest-stage-", dir=str(target.parent)))
    destination_paths = {relative: target / relative for relative in all_files}
    backups: Dict[Path, Optional[bytes]] = {}
    replaced: List[Path] = []
    try:
        # Stage the complete generation first.  A write fault here leaves the
        # previous latest directory untouched and, importantly, no manifest
        # can advertise a partially written generation.
        for relative, content in all_files.items():
            _atomic_write(stage / relative, content)
        _fsync_directory(stage)
        for path in destination_paths.values():
            try:
                backups[path] = path.read_bytes() if path.is_file() else None
            except OSError:
                backups[path] = None
        # Replace the five payloads before the manifest.  The manifest is the
        # commit marker; readers can reject a mixed generation by checking its
        # hashes and generation_id.
        for relative in (*files.keys(), manifest_name):
            source = stage / relative
            destination = destination_paths[relative]
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
            replaced.append(destination)
            _fsync_directory(destination.parent)
        _fsync_directory(target)
    except BaseException:
        # Restore only our explicit targets.  Unrelated files under reports/
        # and latest/ are never enumerated or removed.
        for path in reversed(replaced):
            _restore_file(path, backups.get(path))
        _fsync_directory(target)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    paths = {relative: destination_paths[relative] for relative in all_files}
    return ReportBuild(projection=projection, paths=paths, output_directory=target)


# Friendly aliases used by small integrations and earlier milestone callers.
generate_reports = build_reports
write_reports = build_reports
generate_projection = build_projection


class ReportGenerator:
    """Small stateful facade for integrations that prefer an object API."""

    def __init__(self, config: Any = None, db: Any = None, output_dir: Any = "reports", **kwargs: Any) -> None:
        self.config = config
        self.db = db
        self.output_dir = output_dir
        self.options = dict(kwargs)

    def projection(self, **kwargs: Any) -> Dict[str, Any]:
        options = dict(self.options)
        options.update(kwargs)
        return build_projection(self.config, self.db, **options)

    def build(self, **kwargs: Any) -> ReportBuild:
        options = dict(self.options)
        options.update(kwargs)
        output_dir = options.pop("output_dir", self.output_dir)
        return build_reports(self.config, self.db, output_dir, **options)

    generate = build
    write = build

    __call__ = build


ReportingProjection = Dict[str, Any]
build_report = build_projection
create_reports = build_reports
write_report_artifacts = build_reports


_FALLBACK_HTML = """<!doctype html><meta charset=\"utf-8\"><title>ZooFanCrawler acceptance</title><script type=\"application/json\" id=\"projection\">__PROJECTION_JSON__</script><main><h1>ZooFanCrawler acceptance</h1><p id=\"status\"></p><pre id=\"output\"></pre></main><script>const p=JSON.parse(document.getElementById('projection').textContent);document.getElementById('status').textContent=p.acceptance.status;document.getElementById('output').textContent=JSON.stringify(p,null,2);</script>"""


__all__ = [
    "KNOWN_ADAPTERS",
    "SUPPORTED_ADAPTERS",
    "COMPLETION_CATEGORIES",
    "PUBLIC_COMPLETION_CLASSIFICATIONS",
    "QUALITY_WARNING_LABELS",
    "DEFAULT_OLD_DAYS",
    "DEFAULT_STALE_DAYS",
    "ReportBuild",
    "build_projection",
    "build_report",
    "generate_projection",
    "build_reports",
    "generate_reports",
    "write_reports",
    "ReportGenerator",
    "create_reports",
    "write_report_artifacts",
]
