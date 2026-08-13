"""Core domain and ingestion helpers for ZooFanCrawler.

The project deliberately keeps this package independent of a web framework.  The
small records in :mod:`zoofan.models` are the boundary between crawlers and the
storage implementation, which makes it possible to replace SQLite with a
PostgreSQL adapter later without changing parsers.
"""

from .models import (
    Article,
    ArticleDiscovery,
    CrawlRun,
    CrawlRunStat,
    FetchResponse,
    ParsedArticle,
    ParsedFeedItem,
    ParsedSitemapEntry,
    Source,
    Zoo,
)
from .normalization import normalize_url
from .storage import SQLiteStorage

__all__ = [
    "Article",
    "ArticleDiscovery",
    "CrawlRun",
    "CrawlRunStat",
    "FetchResponse",
    "ParsedArticle",
    "ParsedFeedItem",
    "ParsedSitemapEntry",
    "Source",
    "Zoo",
    "SQLiteStorage",
    "normalize_url",
]

