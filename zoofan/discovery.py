"""Source registry and URL discovery for configured zoos.

The discovery layer deliberately does not persist anything.  It turns one
configured source into a stream of candidates and leaves fetching article
pages, deduplication and change detection to :mod:`zoofan.crawler`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .models import ParsedFeedItem, ParsedSitemapEntry, Source, Zoo
from .fetcher import RequestPolicy, scoped_fetch
from .normalization import normalize_url
from .parsers import (
    parse_archive,
    parse_feed,
    parse_sitemap,
    parse_sitemap_index,
    parse_sitemap_urlset,
)


LOGGER = logging.getLogger(__name__)


@dataclass
class DiscoveryCandidate:
    """A candidate URL and optional metadata emitted by a discovery source."""

    url: str
    source: Optional[Source] = None
    title: Optional[str] = None
    published_at: Any = None
    updated_at_source: Any = None
    summary: Optional[str] = None
    author: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = normalize_url(self.url)


class DiscoveryResult(list):
    """List-compatible discovery output carrying source-request evidence.

    ``root_http_status`` always describes the configured source URL request,
    never a sitemap child or archive pagination request.  Duplicate count is
    the number of otherwise-valid candidates suppressed within this source.
    """

    def __init__(
        self,
        candidates: Iterable[DiscoveryCandidate] = (),
        *,
        root_http_status: Optional[int] = None,
        duplicate_candidate_count: int = 0,
    ) -> None:
        super().__init__(candidates)
        self.root_http_status = root_http_status
        self.duplicate_candidate_count = duplicate_candidate_count


class DiscoveryError(RuntimeError):
    """Discovery failure retaining the root source response status, if any."""

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass
class URLPolicy:
    """Official-domain and false-positive gate shared by every source."""

    official_hosts: tuple[str, ...] = ()
    allow_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    navigation_patterns: tuple[str, ...] = ()

    DEFAULT_NAVIGATION = (
        r"/(?:ticket|tickets|shop|restaurant|gastronomie|contact|kontakt|privacy|datenschutz|terms|cookies|jobs?|karriere)(?:/|$)",
        r"/(?:events?|veranstaltungen|kalender|calendar)(?:/|$)",
    )

    @classmethod
    def from_zoo_source(cls, zoo: Zoo | Mapping[str, Any], source: Source | Mapping[str, Any] | None = None) -> "URLPolicy":
        z = zoo if isinstance(zoo, Zoo) else Zoo(**dict(zoo))
        source_value = source if isinstance(source, Source) else Source(**dict(source or {}))
        config = dict(getattr(z, "metadata", {}) or {})
        config.update(dict(getattr(source_value, "config", {}) or {}))

        host = config.get("official_host") or config.get("host")
        hosts = config.get("official_hosts") or config.get("allowed_domains") or config.get("allowed_hosts")
        if isinstance(hosts, str):
            hosts = [hosts]
        if not hosts and host:
            hosts = [host]
        if not hosts and getattr(z, "website_url", None):
            hosts = [urlsplit(str(z.website_url)).hostname or ""]
        hosts = tuple(str(item).lower().lstrip(".") for item in (hosts or ()) if str(item).strip())

        def _patterns(key: str) -> tuple[str, ...]:
            value = config.get(key, ())
            if isinstance(value, str):
                value = [value]
            return tuple(str(item) for item in (value or ()) if str(item).strip())

        configured_navigation = _patterns("navigation_patterns") or _patterns("navigation_exclude")
        return cls(
            official_hosts=hosts,
            allow_patterns=_patterns("allow_patterns") or _patterns("article_allow") or _patterns("allow_regex"),
            exclude_patterns=_patterns("exclude_patterns") or _patterns("exclude_regex"),
            # Keep configured navigation exclusions separate from the built-in
            # false-positive list.  An explicit article allow rule may
            # override the built-in list, but never an explicit exclusion.
            navigation_patterns=configured_navigation,
        )

    def is_official(self, url: str) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        hostname = parsed.hostname.lower().rstrip(".")
        return any(hostname == host or hostname.endswith("." + host) for host in self.official_hosts)

    @staticmethod
    def _matches(patterns: Sequence[str], value: str) -> bool:
        for pattern in patterns:
            try:
                if re.search(pattern, value, flags=re.IGNORECASE):
                    return True
            except re.error:
                # A malformed optional config should reject rather than make
                # a source accidentally broad.
                if pattern.lower() in value.lower():
                    return True
        return False

    def accepts(self, url: str, *, source_url: Optional[str] = None) -> bool:
        normalized = normalize_url(url)
        if not normalized or not self.is_official(normalized):
            return False
        allowed = bool(self.allow_patterns and self._matches(self.allow_patterns, normalized))
        if self.exclude_patterns and self._matches(self.exclude_patterns, normalized):
            return False
        # Explicit allow rules are authoritative for the default navigation
        # gate (e.g. a publisher can intentionally use /events/ as an article
        # path), but a configured navigation exclusion remains authoritative.
        if self.allow_patterns and not allowed:
            return False
        path = urlsplit(normalized).path
        if self._matches(self.navigation_patterns, path):
            return False
        if not allowed and self._matches(self.DEFAULT_NAVIGATION, path):
            return False
        if allowed:
            return True
        # Homepage/root and a source archive are navigation, not article
        # candidates.
        if source_url:
            source_key = normalize_url(source_url)
            if normalized == source_key:
                return False
        path = path.rstrip("/").lower()
        if path in {"", "/"}:
            return False
        return True


class SourceRegistry:
    """In-memory index of configured zoos and sources.

    Registration is intentionally idempotent.  A caller may pass a loaded
    config or independently register records; persistence is handled by
    ``register_storage`` when a storage adapter is supplied.
    """

    def __init__(self, zoos: Iterable[Zoo] = (), sources: Iterable[Source] = ()) -> None:
        self.zoos: dict[str, Zoo] = {}
        self.sources: dict[str, Source] = {}
        self._source_by_zoo_url: dict[tuple[str, str], Source] = {}
        for zoo in zoos:
            self.add_zoo(zoo)
        for source in sources:
            self.add_source(source)

    @classmethod
    def from_config(cls, config: Any) -> "SourceRegistry":
        return cls(getattr(config, "zoos", ()) or (), getattr(config, "sources", ()) or ())

    def add_zoo(self, zoo: Zoo) -> Zoo:
        key = str(zoo.id or zoo.slug)
        self.zoos[key] = zoo
        self.zoos.setdefault(str(zoo.slug), zoo)
        return zoo

    def add_source(self, source: Source) -> Source:
        # Storage's source uniqueness is (zoo_id, normalized URL).  Normalize
        # before indexing so tracking parameters/trailing slashes cannot make
        # the registry emit two records that SQLite later treats as one.
        source.url = normalize_url(source.url)
        key = (str(source.zoo_id), source.url)
        existing = self._source_by_zoo_url.get(key)
        if existing is not None:
            # Keep the first configured source identity, while allowing a
            # later equivalent declaration to fill missing metadata.
            if not existing.id and source.id:
                existing.id = source.id
            if not existing.name and source.name:
                existing.name = source.name
            if not existing.config and source.config:
                existing.config = dict(source.config)
            existing.enabled = existing.enabled and source.enabled
            return existing
        self._source_by_zoo_url[key] = source
        self.sources[f"{key[0]}:{key[1]}"] = source
        return source

    def zoo_for_source(self, source: Source) -> Optional[Zoo]:
        zoo = self.zoos.get(str(source.zoo_id))
        if zoo is not None:
            return zoo
        return next((item for item in self.zoos.values() if item.slug == source.zoo_id), None)

    def sources_for_zoo(self, zoo: Zoo | str, *, enabled_only: bool = True) -> list[Source]:
        zoo_id = str(zoo.id or zoo.slug) if isinstance(zoo, Zoo) else str(zoo)
        result = [source for source in self.sources.values() if str(source.zoo_id) == zoo_id]
        if isinstance(zoo, Zoo) and not result:
            result = [source for source in self.sources.values() if str(source.zoo_id) == str(zoo.slug)]
        if enabled_only:
            result = [source for source in result if bool(source.enabled)]
        return result

    def register_storage(self, storage: Any) -> None:
        """Upsert all registry records, including health-check-only sources."""

        for zoo in self._unique_zoos():
            if hasattr(storage, "upsert_zoo"):
                persisted = storage.upsert_zoo(zoo)
                if persisted is not None:
                    zoo.id = getattr(persisted, "id", zoo.id)
        for source in self._unique_sources():
            if source.zoo_id in self.zoos:
                zoo = self.zoos[source.zoo_id]
                source.zoo_id = zoo.id or zoo.slug
            if hasattr(storage, "upsert_source"):
                persisted = storage.upsert_source(source)
                if persisted is not None:
                    source.id = getattr(persisted, "id", source.id)

    def _unique_zoos(self) -> list[Zoo]:
        return list({id(zoo): zoo for zoo in self.zoos.values()}.values())

    def _unique_sources(self) -> list[Source]:
        return list({id(source): source for source in self.sources.values()}.values())


def source_priority(source: Source) -> int:
    kind = str(source.kind or "").lower()
    return {"rss": 0, "atom": 0, "sitemap": 1, "archive": 2}.get(kind, 3)


def _response_text(response: Any) -> str:
    if isinstance(response, bytes):
        return response.decode("utf-8", errors="replace")
    if isinstance(response, str):
        return response
    if isinstance(response, tuple) and len(response) >= 2:
        return _response_text(response[1])
    value = getattr(response, "text", None)
    if callable(value):
        value = value()
    if value is not None:
        return str(value)
    content = getattr(response, "content", response)
    return content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content or "")


def _fetch(fetcher: Any, url: str, policy: RequestPolicy) -> Any:
    return scoped_fetch(fetcher, url, policy)


def _raise_for_status(response: Any, policy: Optional[RequestPolicy] = None, requested_url: Optional[str] = None) -> None:
    context = ""
    if policy is not None and requested_url:
        context = f" for source {policy.source_id} at {policy.safe_target(requested_url)}"
    if isinstance(response, tuple) and response:
        if int(response[0]) >= 400:
            raise DiscoveryError(f"HTTP {response[0]}{context}", status_code=int(response[0]))
        return
    if isinstance(response, (str, bytes)):
        return
    status = int(getattr(response, "status_code", getattr(response, "status", 200)))
    if status >= 400:
        raise DiscoveryError(f"HTTP {status}{context}", status_code=status)


def _response_status(response: Any) -> Optional[int]:
    if isinstance(response, tuple) and response:
        return int(response[0])
    if isinstance(response, (str, bytes)):
        return None
    value = getattr(response, "status_code", getattr(response, "status", None))
    return int(value) if value is not None else None


def _as_candidate(item: Any, source: Source) -> DiscoveryCandidate:
    if isinstance(item, DiscoveryCandidate):
        item.source = source
        return item
    if isinstance(item, ParsedFeedItem):
        return DiscoveryCandidate(item.url, source, item.title, item.published_at, item.updated_at_source, item.summary, item.author, dict(item.metadata))
    if isinstance(item, ParsedSitemapEntry):
        return DiscoveryCandidate(item.url, source, metadata=dict(item.metadata))
    if isinstance(item, str):
        return DiscoveryCandidate(item, source)
    if isinstance(item, Mapping):
        return DiscoveryCandidate(
            str(item.get("url") or item.get("link") or ""), source,
            item.get("title"), item.get("published_at"), item.get("updated_at_source"),
            item.get("summary"), item.get("author"), dict(item.get("metadata") or {}),
        )
    return DiscoveryCandidate(str(getattr(item, "url", "")), source)


class DiscoveryEngine:
    """Discover candidate article URLs from one configured source."""

    def __init__(self, fetcher: Any, *, logger: Optional[logging.Logger] = None) -> None:
        self.fetcher = fetcher
        self.logger = logger or LOGGER

    def discover(self, source: Source, *, zoo: Optional[Zoo] = None) -> DiscoveryResult:
        kind = str(source.kind or "").lower().replace("-", "_")
        effective_zoo = zoo or Zoo(website_url=source.url)
        request_policy = RequestPolicy.from_zoo_source(effective_zoo, source)
        response = _fetch(self.fetcher, source.url, request_policy)
        root_http_status = _response_status(response)
        _raise_for_status(response, request_policy, source.url)
        if kind in {"registry_only", "health_check", "healthcheck", "press"}:
            return DiscoveryResult(root_http_status=root_http_status)
        try:
            payload = _response_text(response)
            if kind in {"rss", "atom", "feed"}:
                values = parse_feed(payload, source.url)
            elif kind == "sitemap":
                values = self._discover_sitemap(payload, source, request_policy)
            elif kind in {"archive", "html", "news_archive"}:
                values = self._discover_archive(payload, source, request_policy)
            else:
                raise ValueError(f"unsupported source kind: {source.kind}")
        except Exception as exc:
            # The configured source response is the source-level HTTP
            # evidence. Downstream child/pagination failures keep their own
            # target/status in the message, but must not replace that root
            # status in persistence.
            raise DiscoveryError(str(exc), status_code=root_http_status) from exc
        policy = URLPolicy.from_zoo_source(effective_zoo, source)
        candidates: list[DiscoveryCandidate] = []
        seen: set[str] = set()
        duplicate_count = 0
        for value in values:
            candidate = _as_candidate(value, source)
            key = normalize_url(candidate.url)
            if not key or not policy.accepts(key, source_url=source.url):
                continue
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            candidates.append(candidate)
        return DiscoveryResult(
            candidates,
            root_http_status=root_http_status,
            duplicate_candidate_count=duplicate_count,
        )

    def _discover_archive(self, payload: str, source: Source, request_policy: RequestPolicy) -> list[Any]:
        config = dict(source.config or {})
        # ``parse_archive`` fetches pages through this callback.  Preserve the
        # exact href (including TYPO3 cHash) by resolving from the current URL.
        def fetch_page(url: str) -> str:
            response = _fetch(self.fetcher, url, request_policy)
            _raise_for_status(response, request_policy, url)
            return _response_text(response)

        return parse_archive(
            payload, config, source.url, fetch_page=fetch_page,
            max_pages=config.get("max_pages"), preserve_duplicates=True,
        )

    def _discover_sitemap(self, payload: str, source: Source, request_policy: RequestPolicy) -> list[Any]:
        entries = parse_sitemap(payload, source.url)
        result: list[Any] = []
        config = dict(source.config or {})
        max_sitemaps = max(1, int(config.get("max_sitemaps", 20)))
        pending = list(entries)
        visited: set[str] = {normalize_url(source.url)}
        while pending:
            entry = pending.pop(0)
            if entry.is_index:
                url = normalize_url(entry.url)
                if url in visited or len(visited) >= max_sitemaps:
                    continue
                visited.add(url)
                response = _fetch(self.fetcher, url, request_policy)
                _raise_for_status(response, request_policy, url)
                children = parse_sitemap(_response_text(response), url)
                pending.extend(children)
            else:
                result.append(entry)
        return result


def discover_source(source: Source, fetcher: Any, *, zoo: Optional[Zoo] = None) -> DiscoveryResult:
    """Functional convenience wrapper used by small integrations/tests."""

    return DiscoveryEngine(fetcher).discover(source, zoo=zoo)


__all__ = [
    "DiscoveryCandidate",
    "DiscoveryError",
    "DiscoveryEngine",
    "DiscoveryResult",
    "SourceRegistry",
    "URLPolicy",
    "discover_source",
    "source_priority",
]
