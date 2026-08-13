"""Stable domain records shared by parsers, crawlers and storage.

Records intentionally contain no SQLite-specific types.  Timestamps may be
``datetime`` instances (the preferred form) or ISO strings when data came from
an external source and has not been parsed yet.  Storage normalizes either
form to an ISO-8601 string at its boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Union


Timestamp = Optional[Union[datetime, str]]


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
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.website_url is None and self.url:
            self.website_url = self.url
        elif self.url is None and self.website_url:
            self.url = self.website_url
        if not self.slug and self.name:
            self.slug = self.name.lower().replace(" ", "-")


@dataclass
class Source:
    """A discoverable source (RSS, Atom, sitemap or archive) for a zoo."""

    id: Optional[str] = None
    zoo_id: Optional[str] = None
    url: str = ""
    normalized_url: Optional[str] = None
    kind: str = "rss"
    name: Optional[str] = None
    config: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    status: str = "pending"
    success: Optional[bool] = None
    error: Optional[str] = None
    last_checked: Timestamp = None
    last_success: Timestamp = None
    last_error: Optional[str] = None
    last_http_status: Optional[int] = None

    def __post_init__(self) -> None:
        from .normalization import normalize_url

        self.normalized_url = self.normalized_url or normalize_url(self.url)
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
    # Hash of the exact raw HTML snapshot. content_hash remains the public
    # compatibility name used by older callers.
    html_hash: Optional[str] = None
    raw_html: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Import lazily to avoid making models <-> normalization import each
        # other during package initialization.
        from .normalization import normalize_url

        if self.canonical_url is None and self.url:
            self.canonical_url = normalize_url(self.url)
        if self.url is None and self.canonical_url:
            self.url = self.canonical_url
        if self.normalized_url is None:
            candidate = self.canonical_url or self.url
            if candidate:
                self.normalized_url = normalize_url(candidate)
        if self.content_hash is None and self.html_hash is not None:
            self.content_hash = self.html_hash
        if self.html_hash is None and self.content_hash is not None:
            self.html_hash = self.content_hash


@dataclass
class ArticleDiscovery:
    """The relationship between an article and each source that discovered it."""

    id: Optional[str] = None
    article_id: Optional[str] = None
    source_id: Optional[str] = None
    discovered_url: Optional[str] = None
    discovered_at: Timestamp = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CrawlRun:
    """One batch crawl execution."""

    id: Optional[str] = None
    batch_id: Optional[str] = None
    started_at: Timestamp = None
    finished_at: Timestamp = None
    status: str = "running"
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
    error_count: int = 0
    started_at: Timestamp = None
    finished_at: Timestamp = None
    error: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# A shorter name is convenient for callers and preserves a likely API spelling.
RunStat = CrawlRunStat
CrawlRunStats = CrawlRunStat


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
