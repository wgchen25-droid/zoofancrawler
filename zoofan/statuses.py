"""Stable status and error classification helpers for crawl orchestration.

The crawler deliberately keeps the original exception text in its source
statistics while exposing a small, stable vocabulary to callers.  This
module has no network or persistence dependencies; it is safe to use from
tests and from storage adapters as well.
"""

from __future__ import annotations

import re
import socket
import ssl
import urllib.error
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit


ERROR_STATUSES = (
    "success",
    "success_no_items",
    "source_not_found",
    "robots_disallowed",
    "http_error",
    "timeout",
    "dns_error",
    "tls_error",
    "parse_error",
    "date_parse_error",
    "content_parse_error",
    "javascript_required",
    "blocked",
    "unsupported",
    "configuration_error",
)

SUMMARY_STATUSES = ("partial", "failed")
ALL_STATUSES = ERROR_STATUSES + SUMMARY_STATUSES

# Configuration-facing source status values.  Runtime crawl statuses remain
# in ``ERROR_STATUSES`` above; these values describe a configured roster entry
# before (or independently of) a particular run.  Keeping the vocabulary in
# one dependency-free module lets config validation and reporting agree.
SOURCE_STATUS_VOCABULARY = frozenset(
    {
        "configured",
        "not_found",
        "source_not_found",
        "success",
        "success_no_items",
        "partial",
        "failed",
        "unsupported",
        "robots_disallowed",
        "http_error",
        "timeout",
        "dns_error",
        "tls_error",
        "parse_error",
        "date_parse_error",
        "content_parse_error",
        "javascript_required",
        "blocked",
        "configuration_error",
    }
)

# Short alias used by configuration integrations.
SOURCE_STATUSES = SOURCE_STATUS_VOCABULARY


class ClassifiedRuntimeError(ValueError):
    """An expected runtime failure carrying a stable public classification.

    The exception text remains the operator-facing detail.  ``classification``
    is deliberately a plain attribute so wrappers such as ``DiscoveryError``
    and storage adapters can preserve the category without importing parser
    implementation classes.
    """

    classification = "parse_error"

    def __init__(self, message: str, *, classification: Optional[str] = None) -> None:
        super().__init__(message)
        if classification:
            self.classification = str(classification).strip().lower().replace("-", "_")


class DateParseError(ClassifiedRuntimeError):
    """A publication date was present but could not be parsed."""

    classification = "date_parse_error"


class ContentParseError(ClassifiedRuntimeError):
    """The response did not contain usable article content."""

    classification = "content_parse_error"


class JavascriptRequiredError(ClassifiedRuntimeError):
    """A source is explicitly configured to require JavaScript rendering."""

    classification = "javascript_required"


# Both spellings are kept because ``JavaScript`` is conventional in Python
# APIs while the lower-case token mirrors the persisted status vocabulary.
JavaScriptRequiredError = JavascriptRequiredError


def _chain(error: BaseException) -> Iterable[BaseException]:
    """Yield an exception and its causes/contexts without looping forever."""

    seen: set[int] = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _text(error: BaseException) -> str:
    values: list[str] = []
    for item in _chain(error):
        values.extend((type(item).__name__, str(item)))
        for name in ("classification", "error_classification", "code", "reason"):
            value = getattr(item, name, None)
            if value:
                values.append(str(value))
    return " ".join(value for value in values if value).lower()


def classify_error(error: BaseException, *, phase: Optional[str] = None) -> str:
    """Map an exception to the stable crawl error vocabulary.

    Classification is intentionally conservative and checks exception causes
    as discovery wraps parser/fetch errors in :class:`DiscoveryError`.  The
    original exception is never returned or replaced by this helper.
    """

    values = list(_chain(error))
    text = _text(error)
    explicit = []
    for item in values:
        for name in ("classification", "error_classification", "code", "reason"):
            value = str(getattr(item, name, "") or "").lower().replace("-", "_")
            if value in ALL_STATUSES:
                explicit.append(value)
    if explicit:
        return explicit[0]

    # Keep imports local so this module remains a lightweight dependency and
    # can also be used while fetcher is importing.
    try:
        from .fetcher import (
            RequestBoundaryError,
            RobotsDisallowed,
            RobotsUnavailable,
            TransportError,
        )
    except Exception:  # pragma: no cover - defensive import cycle guard
        request_boundary_types: tuple[type[BaseException], ...] = ()
        robots_types: tuple[type[BaseException], ...] = ()
        transport_types: tuple[type[BaseException], ...] = ()
    else:
        request_boundary_types = (RequestBoundaryError,)
        robots_types = (RobotsDisallowed, RobotsUnavailable)
        transport_types = (TransportError,)

    for item in values:
        if isinstance(item, robots_types):
            return "robots_disallowed"
    # A boundary rejection is a blocked request when it came from a network
    # phase, but a malformed source declaration is configuration trouble.
    for item in values:
        if isinstance(item, request_boundary_types):
            boundary_text = str(item).lower()
            if phase == "configuration" or "target /" in boundary_text or "invalid-url" in boundary_text:
                return "configuration_error"
            return "blocked"

    status: Optional[int] = None
    for item in values:
        for name in ("status_code", "status", "http_status"):
            status_value = getattr(item, name, None)
            if status_value is not None:
                try:
                    status = int(status_value)
                    break
                except (TypeError, ValueError):
                    pass
        if status is not None:
            break
    if status is not None:
        if status in {401, 403, 407, 451} or "forbidden" in text or "blocked" in text:
            return "blocked"
        if status >= 400:
            return "http_error"
    status_match = re.search(r"\bhttps?\s*(?:status|error)?\s*[:#]?\s*([45]\d{2})\b", text)
    if status_match:
        status = int(status_match.group(1))
        return "blocked" if status in {401, 403, 407, 451} else "http_error"

    for item in values:
        if isinstance(item, (ssl.SSLError,)):
            return "tls_error"
        if isinstance(item, socket.gaierror):
            return "dns_error"
        if isinstance(item, (TimeoutError, socket.timeout)):
            return "timeout"
        if isinstance(item, urllib.error.URLError):
            reason = getattr(item, "reason", None)
            if isinstance(reason, socket.gaierror):
                return "dns_error"
            if isinstance(reason, (ssl.SSLError,)) or "certificate" in str(reason).lower() or "tls" in str(reason).lower():
                return "tls_error"
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return "timeout"
        if isinstance(item, transport_types):
            # Fetcher wraps low-level transport failures; inspect their cause
            # first, then fall back to text markers below.
            continue

    if any(marker in text for marker in ("timed out", "timeout", " read timed")):
        return "timeout"
    if "robot" in text and any(marker in text for marker in ("disallow", "unavailable", "robots.txt", "forbidden")):
        return "robots_disallowed"
    if any(marker in text for marker in (
        "gaierror", "name or service not known", "temporary failure in name resolution",
        "nodename nor servname", "getaddrinfo failed", "dns failure", "dns error",
    )):
        return "dns_error"
    if "dns" in text and any(marker in text for marker in ("lookup", "resolve", "failure", "error")):
        return "dns_error"
    if any(marker in text for marker in ("ssl", "tls", "certificate verify failed", "wrong version number")):
        return "tls_error"
    if any(marker in text for marker in (
        "connection refused", "connection reset", "connection aborted",
        "network is unreachable", "no route to host", "broken pipe",
    )):
        return "http_error"
    if any(marker in text for marker in (
        "javascript required", "javascript_required", "javascriptrequired",
        "enable javascript", "requires javascript", "requires_javascript",
        "javascript is disabled", "js required", "js_required",
    )):
        return "javascript_required"
    if any(marker in text for marker in ("unsupported", "not implemented", "unknown source kind")):
        return "unsupported"
    if any(marker in text for marker in ("configuration", "config error", "missing source", "source url is required")):
        return "configuration_error"
    if any(marker in text for marker in (
        "date parse", "date_parse", "dateparse", "invalid date", "unparseable date",
        "could not parse date", "unable to parse date",
    )):
        return "date_parse_error"
    if any(marker in text for marker in (
        "content parse", "content_parse", "contentparse", "content parsing",
        "article content", "missing article content", "unusable article content",
    )):
        return "content_parse_error"
    if any(marker in text for marker in ("parseerror", "parse error", "not well-formed", "xml", "beautifulsoup", "html parser")):
        return "parse_error"
    if any(marker in text for marker in (
        "captcha", "cloudflare", "access denied", "request blocked", "blocked",
        "outside official", "outside/allow policy", "policy rejected",
    )):
        return "blocked"
    if phase == "storage":
        return "configuration_error"
    # Unknown exceptions in the discovery/article parsing boundary are most
    # useful to operators as parse errors, while their original text remains
    # available in the source/stat error fields.
    return "parse_error" if phase in {"discovery", "article", "parse"} else "unsupported"


_SECRET_QUERY = re.compile(
    r"(?i)([?&](?:token|password|passwd|secret|api[_-]?key|access[_-]?token|auth|authorization|signature|sig|key)=)[^&#\s]*"
)
_URL = re.compile(r"(?i)https?://[^\s<>\"']+")


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return value
        # urlsplit exposes userinfo in ``netloc``; retaining only the hostname
        # and explicit non-default port prevents credentials from leaking.
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host
        if ":" in host and not host.startswith("["):
            netloc = f"[{host}]"
        if port is not None and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
            netloc += f":{port}"
        return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def sanitize_error(value: Any) -> str:
    """Remove credentials and query secrets while retaining useful detail."""

    message = str(value or "").strip()
    if not message:
        message = "unknown error"
    message = _URL.sub(lambda match: _safe_url(match.group(0)), message)
    message = _SECRET_QUERY.sub(r"\1<redacted>", message)
    # Also cover key/value snippets not attached to a URL.
    message = re.sub(
        r"(?i)(\bauthorization\s*:\s*)(?:bearer|basic)\s+[^\s,;]+",
        r"\1<redacted>",
        message,
    )
    message = re.sub(
        r"(?i)(\b(?:token|password|passwd|secret|api[_-]?key|access[_-]?token|auth|authorization|signature|sig|key)\s*[=:]\s*)[^\s,;]+",
        r"\1<redacted>",
        message,
    )
    return message


__all__ = [
    "ALL_STATUSES",
    "ClassifiedRuntimeError",
    "ContentParseError",
    "DateParseError",
    "ERROR_STATUSES",
    "JavascriptRequiredError",
    "JavaScriptRequiredError",
    "SUMMARY_STATUSES",
    "SOURCE_STATUS_VOCABULARY",
    "SOURCE_STATUSES",
    "classify_error",
    "sanitize_error",
]
