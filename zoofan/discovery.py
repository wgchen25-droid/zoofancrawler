"""Source registry and URL discovery for configured zoos.

The discovery layer deliberately does not persist anything.  It turns one
configured source into a stream of candidates and leaves fetching article
pages, deduplication and change detection to :mod:`zoofan.crawler`.
"""

from __future__ import annotations

import logging
import inspect
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .adapters import (
    AdapterFetchContext,
    AdapterRegistry,
    UnsupportedAdapterError,
    canonical_adapter_kind,
    normalize_adapter_token,
)
from .models import ParsedFeedItem, ParsedSitemapEntry, Source, Zoo
from .fetcher import RequestPolicy, scoped_fetch
from .normalization import normalize_url
from .parsers import (
    parse_archive,
    parse_feed,
    parse_json_api,
    parse_sitemap,
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
    # A feed can expose a stable article identity separately from the URL
    # used to discover it (for example a tracking/alternate link).  Preserve
    # that evidence so the crawler can probe both identities before fetching
    # the article page.
    canonical_url: Optional[str] = None

    def __post_init__(self) -> None:
        self.url = normalize_url(self.url)
        if self.canonical_url:
            self.canonical_url = normalize_url(self.canonical_url)


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

    @staticmethod
    def _configured_host(value: Any) -> str:
        """Return a normalized configured hostname, or ``""`` if invalid.

        Host declarations are intentionally reduced to their hostname.  A
        scheme is tolerated for compatibility with older configuration, but
        paths, credentials, queries, fragments and ports are not: accepting
        those fields would make a declaration mean something other than a
        host boundary.
        """

        if not isinstance(value, str) or not value.strip():
            return ""
        raw = value.strip()
        try:
            parsed = urlsplit(raw if "://" in raw else "//" + raw)
            # Accessing ``port`` validates malformed and out-of-range ports.
            port = parsed.port
            hostname = parsed.hostname
        except (TypeError, ValueError):
            return ""
        if (
            not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or port is not None
        ):
            return ""
        return hostname.lower().rstrip(".")

    @classmethod
    def _configured_hosts(cls, config: Mapping[str, Any]) -> tuple[str, ...]:
        """Collect the official host and all explicitly declared aliases.

        Keep this as one ordered, de-duplicated set so the source and zoo
        configuration forms have identical semantics.  ``official_hosts`` /
        ``allowed_*`` remain supported as legacy spellings, while aliases are
        additive rather than replacing the primary host.
        """

        hosts: list[str] = []
        for key in (
            "official_host",
            "host",
            "official_hosts",
            "allowed_hosts",
            "allowed_domains",
            "host_aliases",
            "official_host_aliases",
        ):
            value = config.get(key)
            if isinstance(value, str):
                values: tuple[Any, ...] = (value,)
            elif isinstance(value, (list, tuple, set)):
                values = tuple(value)
            else:
                values = ()
            for item in values:
                host = cls._configured_host(item)
                if host and host not in hosts:
                    hosts.append(host)
        return tuple(hosts)

    @classmethod
    def from_zoo_source(cls, zoo: Zoo | Mapping[str, Any], source: Source | Mapping[str, Any] | None = None) -> "URLPolicy":
        z = zoo if isinstance(zoo, Zoo) else Zoo(**dict(zoo))
        source_value = source if isinstance(source, Source) else Source(**dict(source or {}))
        config = dict(getattr(z, "metadata", {}) or {})
        config.update(dict(getattr(source_value, "config", {}) or {}))

        hosts = cls._configured_hosts(config)
        if not hosts:
            # Match RequestPolicy's fallback: an explicitly configured zoo
            # website is preferred, then the source URL keeps standalone
            # source records usable when no website field is available.
            for fallback_url in (
                getattr(z, "website_url", None),
                getattr(source_value, "url", None),
            ):
                try:
                    fallback_host = urlsplit(str(fallback_url or "")).hostname or ""
                except (TypeError, ValueError):
                    fallback_host = ""
                fallback_host = cls._configured_host(fallback_host)
                if fallback_host:
                    hosts = (fallback_host,)
                    break

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
        try:
            parsed = urlsplit(str(url).strip())
            # Accessing ``port`` validates malformed and out-of-range ports;
            # ports themselves remain valid because the boundary is hostname
            # based, just like RequestPolicy.
            parsed.port
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except (TypeError, ValueError):
            return False
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        # A configured host authorizes exactly that hostname.  Subdomains
        # must be listed explicitly as aliases; this prevents suffix spoofing
        # and keeps candidate/canonical checks aligned with request checks.
        configured = {
            host
            for value in self.official_hosts
            if (host := self._configured_host(value))
        }
        return hostname in configured

    @staticmethod
    def _matches(patterns: Sequence[str], value: str) -> bool:
        for pattern in patterns:
            try:
                if re.search(pattern, value, flags=re.IGNORECASE):
                    return True
            except re.error:
                # A malformed optional config should reject rather than make
                # a source accidentally broad.
                continue
        return False

    def accepts(self, url: str, *, source_url: Optional[str] = None) -> bool:
        try:
            normalized = normalize_url(url)
        except (TypeError, ValueError):
            return False
        if not normalized or not self.is_official(normalized):
            return False
        allowed = bool(self.allow_patterns and self._matches(self.allow_patterns, normalized))
        if self.exclude_patterns and self._matches(self.exclude_patterns, normalized):
            return False
        path = urlsplit(normalized).path
        # Homepage/root and the configured source archive are never article
        # candidates, even when an explicit allow rule matches them.
        if source_url:
            try:
                source_key = normalize_url(source_url)
            except (TypeError, ValueError):
                source_key = ""
            if normalized == source_key:
                return False
        if path.rstrip("/").lower() in {"", "/"}:
            return False
        # Explicit allow rules are authoritative for the default navigation
        # gate (e.g. a publisher can intentionally use /events/ as an article
        # path), but a configured navigation exclusion remains authoritative.
        if self.allow_patterns and not allowed:
            return False
        if self._matches(self.navigation_patterns, path):
            return False
        if not allowed and self._matches(self.DEFAULT_NAVIGATION, path):
            return False
        if allowed:
            return True
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
        # Missing URLs are legitimate for explicit ``unsupported`` roster
        # entries.  Do not collapse every such entry into one in-memory source
        # merely because SQLite's nullable normalized URL has no identity.
        source_key = source.url or f"__missing__:{source.id or id(source)}"
        key = (str(source.zoo_id), source_key)
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
        return DiscoveryCandidate(
            item.url,
            source,
            item.title,
            item.published_at,
            item.updated_at_source,
            item.summary,
            item.author,
            dict(item.metadata),
            item.canonical_url,
        )
    if isinstance(item, ParsedSitemapEntry):
        return DiscoveryCandidate(item.url, source, metadata=dict(item.metadata))
    if isinstance(item, str):
        return DiscoveryCandidate(item, source)
    if isinstance(item, Mapping):
        return DiscoveryCandidate(
            str(item.get("url") or item.get("link") or ""), source,
            item.get("title"), item.get("published_at"), item.get("updated_at_source"),
            item.get("summary"), item.get("author"), dict(item.get("metadata") or {}),
            item.get("canonical_url"),
        )
    return DiscoveryCandidate(
        str(getattr(item, "url", "")),
        source,
        canonical_url=getattr(item, "canonical_url", None),
    )


def _normalize_identity_text(value: str) -> str:
    """Normalize feed text for deterministic, case-insensitive matching.

    NFKC handles compatibility forms such as a non-breaking space, ``split``
    collapses all Unicode whitespace to one ordinary space, and ``casefold``
    gives stable case-insensitive behavior across non-ASCII feed text.  The
    result intentionally keeps punctuation and word boundaries intact: a
    configured phrase is an ordinary substring, not a regular expression.
    """

    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _identity_metadata_text(value: Any) -> Iterable[str]:
    """Yield only deterministic textual values from feed metadata.

    Parser metadata is normally a flat string mapping, but custom parser
    integrations may expose nested mappings or lists.  Mapping keys are
    traversed in string order and unordered collections are deliberately
    ignored so matching never depends on iteration order.  Non-text values
    (including timestamps and arbitrary objects) are not identity evidence.
    """

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        keys = sorted(
            (key for key in value if isinstance(key, str)),
            key=lambda key: key,
        )
        for key in keys:
            yield from _identity_metadata_text(value[key])
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _identity_metadata_text(item)


def _identity_filter_tokens(source: Source) -> Optional[tuple[str, ...]]:
    """Return normalized required identity phrases, or ``None`` if absent.

    ``identity_filter`` is intentionally opt-in.  Once present, however, its
    shape is strict: it must be a mapping containing a non-empty list/tuple of
    non-empty strings and may not contain unknown keys.  Rejecting malformed
    values prevents a typo from silently disabling the filter and broadening
    a shared feed.
    """

    config = getattr(source, "config", {})
    if config is None:
        config = {}
    if not isinstance(config, Mapping):
        raise ValueError(f"source {source.id or source.url!r} config must be a mapping")
    if "identity_filter" not in config:
        return None

    configured = config["identity_filter"]
    context = f"source {source.id or source.url!r} identity_filter"
    if not isinstance(configured, Mapping):
        raise ValueError(f"{context} must be a mapping")
    unknown_keys = sorted(str(key) for key in configured if key != "required_text_any")
    if unknown_keys:
        raise ValueError(f"{context} has unsupported keys: {', '.join(unknown_keys)}")
    if "required_text_any" not in configured:
        raise ValueError(f"{context}.required_text_any is required")

    values = configured["required_text_any"]
    # Sets are rejected even though they are iterable: their order is not a
    # deterministic configuration representation and can hide malformed YAML.
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{context}.required_text_any must be a list of strings")
    if not values:
        raise ValueError(f"{context}.required_text_any must not be empty")

    tokens: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise ValueError(
                f"{context}.required_text_any[{index}] must be a non-empty string"
            )
        token = _normalize_identity_text(value)
        if not token:
            raise ValueError(
                f"{context}.required_text_any[{index}] must be a non-empty string"
            )
        if token not in seen:
            seen.add(token)
            tokens.append(token)
    return tuple(tokens)


def _identity_filter_accepts(
    candidate: DiscoveryCandidate,
    tokens: tuple[str, ...],
) -> bool:
    """Check identity phrases against feed-provided text only.

    Title, summary and author are the candidate's textual feed fields;
    textual values nested in ``metadata`` are included as additional feed
    evidence.  URLs, GUIDs, timestamps and anything fetched from an article
    page are intentionally outside this set.
    """

    values: list[str] = []
    for value in (candidate.title, candidate.summary, candidate.author):
        if isinstance(value, str):
            values.append(value)
    values.extend(_identity_metadata_text(candidate.metadata))
    normalized_values = tuple(
        normalized
        for value in values
        if (normalized := _normalize_identity_text(value))
    )
    return any(token in value for token in tokens for value in normalized_values)


class DiscoveryEngine:
    """Discover candidate article URLs from one configured source."""

    def __init__(
        self,
        fetcher: Any,
        *,
        logger: Optional[logging.Logger] = None,
        adapter_registry: Optional[Any] = None,
        custom_adapters: Optional[Any] = None,
    ) -> None:
        self.fetcher = fetcher
        self.logger = logger or LOGGER
        registry = adapter_registry if adapter_registry is not None else custom_adapters
        if registry is None:
            registry = AdapterRegistry()
        elif isinstance(registry, Mapping):
            registry = AdapterRegistry(registry)
        self.adapter_registry = registry

    @staticmethod
    def _custom_name(source: Source) -> Optional[str]:
        value = getattr(source, "adapter", None)
        if not value or normalize_adapter_token(value) in {"custom_adapter", "custom", "custom_adapter"}:
            config = dict(source.config or {})
            value = config.get("custom_adapter", config.get("adapter_name"))
        if not value:
            return None
        return str(value).strip() or None

    def _custom_adapter(self, source: Source) -> Any:
        name = self._custom_name(source)
        resolver = getattr(self.adapter_registry, "get", None) or getattr(self.adapter_registry, "resolve", None)
        adapter = resolver(name) if resolver is not None else None
        if adapter is None and isinstance(self.adapter_registry, Mapping) and name:
            adapter = self.adapter_registry.get(name)
        if adapter is None:
            raise UnsupportedAdapterError(
                f"unsupported custom adapter {name!r} for source {source.id or source.url}"
            )
        method = getattr(adapter, "discover", adapter if callable(adapter) else None)
        if not callable(method):
            raise UnsupportedAdapterError(
                f"custom adapter {name!r} does not implement discover"
            )
        return adapter

    @staticmethod
    def _custom_zoo_config(zoo: Optional[Zoo], source: Source) -> Any:
        """Provide a mapping/attribute-compatible view for compact adapters."""

        class ZooConfigView(dict):
            def __init__(self, record: Optional[Zoo], configured_source: Source) -> None:
                values = {
                    "id": getattr(record, "id", None),
                    "slug": getattr(record, "slug", None),
                    "name": getattr(record, "name", None),
                    "website_url": getattr(record, "website_url", None),
                    "country_code": getattr(record, "country_code", None),
                    "language": getattr(record, "language", None),
                    "region": getattr(record, "region", None),
                    "city": getattr(record, "city", None),
                    "source": configured_source,
                    "sources": [configured_source],
                    "source_config": dict(configured_source.config or {}),
                }
                super().__init__(values)
                self._record = record

            def __getattr__(self, key: str) -> Any:
                if self._record is not None and hasattr(self._record, key):
                    return getattr(self._record, key)
                raise AttributeError(key)

        return ZooConfigView(zoo, source)

    @staticmethod
    def _invoke_custom(
        adapter: Any,
        source: Source,
        zoo: Optional[Zoo],
        fetcher: AdapterFetchContext,
        policy: RequestPolicy,
    ) -> Any:
        """Invoke supported custom signatures without swallowing adapter bugs."""

        method = getattr(adapter, "discover", adapter if callable(adapter) else None)
        if not callable(method):
            raise UnsupportedAdapterError("custom adapter does not implement discover")
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            # A C-extension callable may not expose a signature.  The stable
            # protocol's complete call is the only fallback in that case.
            return method(source, zoo=zoo, fetcher=fetcher, request_policy=policy)
        parameters = list(signature.parameters.values())
        has_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        view = DiscoveryEngine._custom_zoo_config(zoo, source)
        known_values = {
            "source": source,
            "zoo": zoo,
            "zoo_config": view,
            "config": view,
            "fetcher": fetcher,
            "fetch_context": fetcher,
            "context": fetcher,
            "request_policy": policy,
        }
        kwargs: dict[str, Any] = {}
        positional: list[Any] = []
        for parameter in parameters:
            if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if parameter.name in known_values:
                value = known_values[parameter.name]
            elif parameter.default is inspect.Parameter.empty:
                # An unnamed required argument is still deterministic and
                # receives the source record; it cannot trigger imports or
                # evaluation and gives the adapter a useful failure boundary.
                value = source
            else:
                continue
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(value)
            else:
                kwargs[parameter.name] = value
        if has_kwargs:
            # The complete protocol is useful for adapters that intentionally
            # accept context via **kwargs even when no named parameters are
            # declared in their signature.
            for key, value in known_values.items():
                kwargs.setdefault(key, value)
        return method(*positional, **kwargs)

    def _discover_custom(self, source: Source, *, zoo: Optional[Zoo]) -> DiscoveryResult:
        identity_tokens = _identity_filter_tokens(source)
        adapter = self._custom_adapter(source)
        effective_zoo = zoo or Zoo(website_url=source.url)
        policy = RequestPolicy.from_zoo_source(effective_zoo, source)
        # Never hand a custom adapter the crawler's raw fetcher.  The context
        # pins every adapter request to this source/zoo policy and rejects
        # attempts to supply an alternate policy or request options.
        fetch_context = AdapterFetchContext(self.fetcher, policy)
        values = self._invoke_custom(adapter, source, zoo, fetch_context, policy)
        if values is None:
            values = []
        if isinstance(values, DiscoveryResult):
            root_status = values.root_http_status
            raw_values: Iterable[Any] = values
            duplicate_count = values.duplicate_candidate_count
        elif isinstance(values, Mapping):
            root_status = values.get("root_http_status")
            duplicate_count = int(values.get("duplicate_candidate_count", 0) or 0)
            raw_values = values.get("items", values.get("candidates", [])) or []
        elif isinstance(values, (str, bytes, DiscoveryCandidate, ParsedFeedItem, ParsedSitemapEntry)):
            root_status = None
            duplicate_count = 0
            raw_values = [values]
        else:
            root_status = None
            duplicate_count = 0
            raw_values = values
        return self._filter_candidates(
            raw_values,
            source,
            effective_zoo,
            root_http_status=root_status,
            initial_duplicate_count=duplicate_count,
            identity_tokens=identity_tokens,
        )

    @staticmethod
    def _filter_candidates(
        values: Iterable[Any],
        source: Source,
        zoo: Zoo,
        *,
        root_http_status: Optional[int] = None,
        initial_duplicate_count: int = 0,
        identity_tokens: Optional[tuple[str, ...]] = None,
    ) -> DiscoveryResult:
        if identity_tokens is None:
            identity_tokens = _identity_filter_tokens(source)
        policy = URLPolicy.from_zoo_source(zoo, source)
        candidates: list[DiscoveryCandidate] = []
        seen: set[str] = set()
        duplicate_count = int(initial_duplicate_count or 0)
        for value in values:
            candidate = _as_candidate(value, source)
            # Identity is deliberately checked before URL policy and
            # duplicate suppression.  It is evidence from the feed item only;
            # article HTML is not fetched until this candidate survives.
            if identity_tokens is not None and not _identity_filter_accepts(candidate, identity_tokens):
                continue
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

    def discover(self, source: Source, *, zoo: Optional[Zoo] = None) -> DiscoveryResult:
        # Validate opt-in identity filtering before any source request.  A
        # malformed filter must fail closed rather than silently broadening a
        # shared feed.
        identity_tokens = _identity_filter_tokens(source)
        kind = canonical_adapter_kind(source.kind) or normalize_adapter_token(source.kind)
        if kind == "unsupported":
            raise UnsupportedAdapterError(
                f"unsupported source adapter explicitly configured for {source.id or source.url}"
            )
        if kind == "custom_adapter":
            # Unknown/absent custom names are classified unsupported before
            # any network request is attempted.
            return self._discover_custom(source, zoo=zoo)
        effective_zoo = zoo or Zoo(website_url=source.url)
        request_policy = RequestPolicy.from_zoo_source(effective_zoo, source)
        response = _fetch(self.fetcher, source.url, request_policy)
        root_http_status = _response_status(response)
        _raise_for_status(response, request_policy, source.url)
        if kind == "registry_only":
            return DiscoveryResult(root_http_status=root_http_status)
        try:
            payload = _response_text(response)
            if kind in {"rss", "atom"}:
                values = parse_feed(payload, source.url)
            elif kind == "sitemap":
                values = self._discover_sitemap(payload, source, request_policy)
            elif kind == "archive":
                values = self._discover_archive(payload, source, request_policy)
            elif kind == "json_api":
                values = parse_json_api(payload, source.config, source.url)
            else:
                raise ValueError(f"unsupported source kind: {source.kind}")
        except Exception as exc:
            # The configured source response is the source-level HTTP
            # evidence. Downstream child/pagination failures keep their own
            # target/status in the message, but must not replace that root
            # status in persistence.
            raise DiscoveryError(str(exc), status_code=root_http_status) from exc
        return self._filter_candidates(
            values,
            source,
            effective_zoo,
            root_http_status=root_http_status,
            identity_tokens=identity_tokens,
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
