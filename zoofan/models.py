"""Stable domain records shared by parsers, crawlers and storage.

Records intentionally contain no SQLite-specific types.  Timestamps may be
``datetime`` instances (the preferred form) or ISO strings when data came from
an external source and has not been parsed yet.  Storage normalizes either
form to an ISO-8601 string at its boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Union, cast


Timestamp = Optional[Union[datetime, str]]
_UNSET = object()


@dataclass
class Zoo:
    """A zoo or publisher being crawled."""

    id: Optional[str] = None
    slug: str = ""
    name: str = ""
    website_url: Optional[str] = None
    # ``url`` is accepted for config/API ergonomics; website_url remains the
    # canonical persisted field.
    url: Optional[str] = None
    country_code: Optional[str] = None
    language: Optional[str] = None
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    # YAML/config values commonly arrive as lists; the domain representation
    # is immutable while accepting both list and tuple inputs.
    groups: tuple[str, ...] = field(default=cast(tuple[str, ...], _UNSET))
    region: Optional[str] = None
    city: Optional[str] = None
    source_status: Optional[str] = None
    list_provenance: list[Any] = field(default=cast(list[Any], _UNSET))
    # Distinguish omitted optional registry fields from an explicit empty
    # list.  This lets authoritative config imports clear stale values while
    # preserving the old partial-update behavior for legacy constructors.

    def __setattr__(self, name: str, value: Any) -> None:
        # Also recognize an explicit clear performed after construction, which
        # is common when a config adapter reuses a Zoo instance.
        if name == "groups" and value is not None and self.__dict__.get("_registry_fields_initialized", False):
            object.__setattr__(self, "_groups_provided", True)
        elif name == "list_provenance" and value is not None and self.__dict__.get("_registry_fields_initialized", False):
            object.__setattr__(self, "_list_provenance_provided", True)
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        if self.website_url is None and self.url:
            self.website_url = self.url
        elif self.url is None and self.website_url:
            self.url = self.website_url
        if not self.slug and self.name:
            self.slug = self.name.lower().replace(" ", "-")
        self._groups_provided = self.groups is not _UNSET and self.groups is not None
        if self.groups is _UNSET or self.groups is None:
            self.groups = ()
        elif isinstance(self.groups, str):
            self.groups = (self.groups,)
        else:
            self.groups = tuple(str(value) for value in self.groups)
        self._list_provenance_provided = self.list_provenance is not _UNSET and self.list_provenance is not None
        if self.list_provenance is _UNSET or self.list_provenance is None:
            self.list_provenance = []
        elif isinstance(self.list_provenance, tuple):
            self.list_provenance = list(self.list_provenance)
        self._registry_fields_initialized = True


@dataclass
class Source:
    """A discoverable source (RSS, Atom, sitemap or archive) for a zoo."""

    id: Optional[str] = None
    zoo_id: Optional[str] = None
    url: str = ""
    normalized_url: Optional[str] = None
    kind: str = "rss"
    name: Optional[str] = None
    language: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    status: str = "pending"
    success: Optional[bool] = None
    error: Optional[str] = None
    last_checked: Timestamp = None
    last_success: Timestamp = None
    last_error: Optional[str] = None
    last_http_status: Optional[int] = None
    # ``kind`` is the canonical taxonomy used for dispatch.  ``adapter`` is
    # the first-class implementation name: built-ins normalize to their
    # canonical kind, while a custom adapter keeps its injected registry name.
    # Appending these fields preserves positional construction for legacy
    # callers which only supplied the original source fields.
    adapter: Optional[str] = None
    source_status: Optional[str] = None

    def __post_init__(self) -> None:
        from .normalization import normalize_url

        # Config loading performs the same normalization, but doing it here
        # keeps directly constructed Source records and registry discovery
        # behavior identical.  Unknown tokens remain visible so the caller
        # can classify them as unsupported instead of silently changing the
        # configured value.
        try:
            from .adapters import canonical_adapter_kind

            canonical_kind = canonical_adapter_kind(self.kind)
        except Exception:  # pragma: no cover - import-cycle defensive path
            canonical_kind = None
        if canonical_kind:
            self.kind = canonical_kind
        configured_adapter = None
        if isinstance(self.config, dict):
            configured_adapter = self.config.get("adapter", self.config.get("custom_adapter", self.config.get("adapter_name")))
        if self.adapter is None:
            self.adapter = configured_adapter or canonical_kind or (str(self.kind).strip() if self.kind else None)
        else:
            adapter_token = str(self.adapter).strip()
            try:
                canonical_adapter = canonical_adapter_kind(adapter_token)
            except Exception:  # pragma: no cover - import-cycle defensive path
                canonical_adapter = None
            # An alias used as the implementation spelling is normalized to
            # the same canonical built-in.  A non-taxonomy token is a custom
            # registry name and is deliberately preserved case-sensitively.
            self.adapter = canonical_adapter or adapter_token or None
        if self.kind == "custom_adapter" and self.adapter and isinstance(self.config, dict):
            # SQLite's legacy source schema persists config_json but not an
            # adapter column.  Retain the injected name there so a source can
            # be reloaded without turning it into an anonymous custom source.
            self.config.setdefault("adapter", self.adapter)

        # Empty/missing source URLs are unknown, not the shared empty-string
        # identity.  Keeping them NULL allows multiple legacy health-check
        # rows without creating a false unique collision.
        self.normalized_url = self.normalized_url or (normalize_url(self.url) or None)
        if self.error is None:
            self.error = self.last_error
        if self.last_error is None:
            self.last_error = self.error


@dataclass
class Article:
    """An article after discovery and/or article-page parsing."""

    id: Optional[str] = None
    canonical_url: Optional[str] = None
    normalized_url: Optional[str] = None
    # Original URL is retained when canonicalization changes it.
    url: Optional[str] = None
    title: Optional[str] = None
    published_at: Timestamp = None
    updated_at_source: Timestamp = None
    author: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    content_hash: Optional[str] = None
    # SHA-256 of the exact response body bytes.  This is deliberately
    # independent from content_hash, which contributes evidence to the
    # composite content identity together with the normalized title.
    html_hash: Optional[str] = None
    language: Optional[str] = None
    http_status: Optional[int] = None
    crawl_status: Optional[str] = None
    last_fetched_at: Timestamp = None
    raw_html: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Keep the source representation independent from the parsed timestamp.
    # These fields are appended so legacy positional construction remains
    # valid while newer callers can use descriptive keyword arguments.
    published_at_raw: Optional[str] = None
    content_html: Optional[str] = None
    image_url: Optional[str] = None
    parse_status: Optional[str] = None
    # Database-maintained composite identity for parsed-content evidence.
    # It is derived from content_hash and the normalized title by storage;
    # callers should not treat it as an independent writable identity.
    content_identity_key: Optional[str] = None
    # Preserve the exact URL lexeme supplied by discovery/fetching separately
    # from the normalized identity fields.  ``url`` is normalized at the
    # storage boundary for backwards compatibility; this field is evidence,
    # not an identity key.
    source_url_raw: Optional[str] = None

    def __post_init__(self) -> None:
        # Import lazily to avoid making models <-> normalization import each
        # other during package initialization.
        from .normalization import normalize_url

        raw_url = self.url
        raw_canonical_url = self.canonical_url
        if self.canonical_url is None and self.url:
            self.canonical_url = normalize_url(self.url)
        if self.url is None and self.canonical_url:
            self.url = self.canonical_url
        if self.normalized_url is None:
            candidate = self.canonical_url or self.url
            if candidate:
                self.normalized_url = normalize_url(candidate)
        if self.source_url_raw is None:
            # Keep the caller's original spelling (including query ordering,
            # tracking parameters, and surrounding whitespace) before any
            # storage normalization occurs.  A canonical-only constructor has
            # no separate source lexeme, so the canonical input is the best
            # available evidence.
            self.source_url_raw = (
                raw_url if raw_url is not None else raw_canonical_url
            )

    # Read-only compatibility aliases.  Canonical fields remain the only
    # writable representation so old readers can migrate without creating a
    # second source of truth.
    @property
    def article_id(self) -> Optional[str]:
        return self.id

    @property
    def fetched_at(self) -> Timestamp:
        return self.last_fetched_at

    @property
    def content_text(self) -> Optional[str]:
        return self.content


@dataclass(frozen=True)
class ArticleUpsertOutcome:
    """Outcome-aware companion result without changing legacy upsert callers."""

    article: Article
    created: bool
    # ``updated`` distinguishes a true article evidence change from a
    # repeated discovery that only refreshes relationship last-seen state.
    # Keep a default so two-field construction by older adapters remains
    # source-compatible.
    updated: bool = False

    @property
    def already_known(self) -> bool:
        return not self.created

    @property
    def action(self) -> str:
        """Stable human-readable operation name for outcome-aware callers."""

        if self.created:
            return "created"
        return "updated" if self.updated else "unchanged"


@dataclass
class ArticleDiscovery:
    """The relationship between an article and each source that discovered it."""

    id: Optional[str] = None
    article_id: Optional[str] = None
    source_id: Optional[str] = None
    discovered_url: Optional[str] = None
    discovered_at: Timestamp = None
    last_discovered_at: Timestamp = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # ``discovered_url`` is the normalized relationship identity.  Keep the
    # original candidate lexeme independently so provenance can explain why
    # two equivalent candidates collapsed to one relationship.
    discovered_url_raw: Optional[str] = None


@dataclass
class ArticleReadModel(Article):
    """Joined article evidence used by read-only consumers.

    The base :class:`Article` fields remain available for compatibility while
    the read model adds relationship bounds and the complete discovery
    provenance.  Storage timestamps are kept distinct from fetch/source
    timestamps so callers can tell when evidence was persisted versus when
    the page was observed.
    """

    first_discovered_at: Timestamp = None
    last_discovered_at: Timestamp = None
    created_at: Timestamp = None
    storage_updated_at: Timestamp = None
    discoveries: list[ArticleDiscovery] = field(default_factory=list)

    @property
    def article(self) -> Article:
        """Compatibility view for consumers expecting composition."""

        return self

    @property
    def article_discoveries(self) -> list[ArticleDiscovery]:
        """Explicit provenance alias used by report/dashboard adapters."""

        return self.discoveries


@dataclass
class CrawlRun:
    """One batch crawl execution."""

    id: Optional[str] = None
    batch_id: Optional[str] = None
    started_at: Timestamp = None
    finished_at: Timestamp = None
    duration_ms: Optional[int] = None
    status: str = "running"
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Console/runtime state is first-class domain data.  These fields are
    # deliberately nullable: a legacy row, or a newly-started run before its
    # first state update, has no honest liveness/activity evidence yet.
    heartbeat_at: Timestamp = None
    progress_at: Timestamp = None
    current_phase: Optional[str] = None
    current_zoo_id: Optional[str] = None
    current_source_id: Optional[str] = None
    progress: Optional[dict[str, Any]] = None
    stop_reason: Optional[str] = None
    # Constructor compatibility for callers that used the early console
    # spelling.  Storage treats ``progress`` as canonical on writes.
    progress_json: Optional[str] = None

    def __post_init__(self) -> None:
        if self.progress is None and self.progress_json not in (None, ""):
            try:
                decoded = json.loads(str(self.progress_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, dict):
                self.progress = decoded

    # Read-only aliases retain the old attribute vocabulary without adding
    # dynamically-created fields to instances returned by storage.
    @property
    def heartbeat(self) -> Timestamp:
        return self.heartbeat_at

    @property
    def current_zoo(self) -> Optional[str]:
        return self.current_zoo_id

    @property
    def current_source(self) -> Optional[str]:
        return self.current_source_id


@dataclass
class CrawlRunStat:
    """Per-zoo/per-source results and errors for a crawl run."""

    id: Optional[str] = None
    crawl_run_id: Optional[str] = None
    zoo_id: Optional[str] = None
    source_id: Optional[str] = None
    status: str = "running"
    discovered_count: int = 0
    fetched_count: int = 0
    stored_count: int = 0
    already_known_count: int = 0
    duplicate_candidate_count: int = 0
    error_count: int = 0
    duration_ms: Optional[int] = None
    started_at: Timestamp = None
    finished_at: Timestamp = None
    error: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_reason: Optional[str] = None


# A shorter name is convenient for callers and preserves a likely API spelling.
RunStat = CrawlRunStat
CrawlRunStats = CrawlRunStat


@dataclass(init=False)
class CrawlZooResult:
    """Durable result for one zoo within a crawl run.

    ``crawl_run_id`` is canonical, matching :class:`CrawlRunStat`; the
    shorter ``run_id`` spelling is accepted as a constructor alias for
    integrations that use the public run-result vocabulary.
    """

    id: Optional[str]
    crawl_run_id: Optional[str]
    zoo_id: Optional[str]
    zoo_slug: Optional[str]
    zoo_name: Optional[str]
    status: str
    source_status: Optional[str]
    discovered: int
    parsed: int
    inserted: int
    updated: int
    failed: int
    duplicate_filtered: int
    duration_ms: Optional[int]
    source_url: Optional[str]
    http_status: Optional[int]
    error_category: Optional[str]
    error_summary: Optional[str]
    started_at: Timestamp
    finished_at: Timestamp
    metadata: dict[str, Any]
    stop_reason: Optional[str]

    def __init__(
        self,
        id: Optional[str] = None,
        crawl_run_id: Optional[str] = None,
        zoo_id: Optional[str] = None,
        zoo_slug: Optional[str] = None,
        zoo_name: Optional[str] = None,
        status: str = "running",
        source_status: Optional[str] = None,
        discovered: int = 0,
        parsed: int = 0,
        inserted: int = 0,
        updated: int = 0,
        failed: int = 0,
        duplicate_filtered: int = 0,
        duration_ms: Optional[int] = None,
        source_url: Optional[str] = None,
        http_status: Optional[int] = None,
        error_category: Optional[str] = None,
        error_summary: Optional[str] = None,
        started_at: Timestamp = None,
        finished_at: Timestamp = None,
        metadata: Optional[dict[str, Any]] = None,
        *,
        stop_reason: Optional[str] = None,
        run_id: Optional[str] = None,
        discovered_count: Optional[int] = None,
        parsed_count: Optional[int] = None,
        inserted_count: Optional[int] = None,
        updated_count: Optional[int] = None,
        failed_count: Optional[int] = None,
        duplicate_filtered_count: Optional[int] = None,
    ) -> None:
        self.id = id
        self.crawl_run_id = crawl_run_id or run_id
        self.zoo_id = zoo_id
        self.zoo_slug = zoo_slug
        self.zoo_name = zoo_name
        self.status = status
        self.source_status = source_status
        self.discovered = int((discovered_count if discovered_count is not None else discovered) or 0)
        self.parsed = int((parsed_count if parsed_count is not None else parsed) or 0)
        self.inserted = int((inserted_count if inserted_count is not None else inserted) or 0)
        self.updated = int((updated_count if updated_count is not None else updated) or 0)
        self.failed = int((failed_count if failed_count is not None else failed) or 0)
        self.duplicate_filtered = int(
            (
                duplicate_filtered_count
                if duplicate_filtered_count is not None
                else duplicate_filtered
            )
            or 0
        )
        self.duration_ms = duration_ms
        self.source_url = source_url
        self.http_status = http_status
        self.error_category = error_category
        self.error_summary = error_summary
        self.stop_reason = stop_reason
        self.started_at = started_at
        self.finished_at = finished_at
        self.metadata = dict(metadata or {})

    @property
    def run_id(self) -> Optional[str]:
        return self.crawl_run_id

    @property
    def discovered_count(self) -> int:
        return self.discovered

    @property
    def parsed_count(self) -> int:
        return self.parsed

    @property
    def inserted_count(self) -> int:
        return self.inserted

    @property
    def updated_count(self) -> int:
        return self.updated

    @property
    def failed_count(self) -> int:
        return self.failed

    @property
    def duplicate_filtered_count(self) -> int:
        return self.duplicate_filtered


@dataclass
class ParsedFeedItem:
    """A normalized item emitted by an RSS/Atom parser."""

    url: str
    title: Optional[str] = None
    published_at: Timestamp = None
    updated_at_source: Timestamp = None
    summary: Optional[str] = None
    author: Optional[str] = None
    guid: Optional[str] = None
    canonical_url: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .normalization import normalize_url

        self.url = normalize_url(self.url)
        if self.canonical_url:
            self.canonical_url = normalize_url(self.canonical_url)


@dataclass
class ParsedSitemapEntry:
    """An entry from either a sitemap URL set or sitemap index."""

    url: str
    lastmod: Timestamp = None
    is_index: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .normalization import normalize_url

        self.url = normalize_url(self.url)


@dataclass
class ParsedArticle:
    """Fields extracted from an article page."""

    url: Optional[str] = None
    canonical_url: Optional[str] = None
    normalized_url: Optional[str] = None
    title: Optional[str] = None
    published_at: Timestamp = None
    updated_at_source: Timestamp = None
    author: Optional[str] = None
    summary: Optional[str] = None
    content: Optional[str] = None
    content_hash: Optional[str] = None
    raw_html: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    published_at_raw: Optional[str] = None
    content_html: Optional[str] = None
    image_url: Optional[str] = None
    parse_status: Optional[str] = None

    def __post_init__(self) -> None:
        from .normalization import normalize_url

        if self.canonical_url:
            self.canonical_url = normalize_url(self.canonical_url)
        if self.url:
            self.url = normalize_url(self.url)
        self.normalized_url = self.normalized_url or normalize_url(
            self.canonical_url or self.url or ""
        ) or None


@dataclass
class FetchResponse:
    """Transport-neutral HTTP response returned by :class:`Fetcher`."""

    url: str
    status_code: int
    content: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    reason: Optional[str] = None
    history: tuple[str, ...] = ()

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def text(self) -> str:
        content_type = ""
        try:
            content_type = next(
                value for key, value in self.headers.items() if key.lower() == "content-type"
            )
        except StopIteration:
            pass
        encoding = "utf-8"
        if "charset=" in content_type.lower():
            encoding = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()
        try:
            return self.content.decode(encoding, errors="replace")
        except (LookupError, UnicodeError):
            return self.content.decode("utf-8", errors="replace")

    def raise_for_status(self) -> None:
        if not self.ok:
            from .fetcher import HTTPStatusError

            raise HTTPStatusError(self.status_code, self.url, self.reason)
