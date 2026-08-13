"""Batch crawler orchestration.

``Crawler`` coordinates configured source discovery, article fetching,
normalization, deduplication and storage.  It keeps failures at source and zoo
boundaries so one broken endpoint cannot abort the batch.
"""

from __future__ import annotations

import logging
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from .discovery import DiscoveryCandidate, DiscoveryEngine, DiscoveryError, SourceRegistry, URLPolicy, source_priority
from .fetcher import Fetcher, RequestPolicy, scoped_fetch
from .models import Article, CrawlRun, CrawlRunStat, Source, Zoo
from .normalization import normalize_url
from .parsers import parse_article


LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_code(error: BaseException) -> Optional[int]:
    return getattr(error, "status_code", getattr(error, "status", None))


def _body_bytes(value: Any) -> bytes:
    """Return exact bytes for text or any object implementing the buffer API."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    try:
        return bytes(memoryview(value))
    except TypeError as exc:
        raise TypeError("response body must be text or bytes-like") from exc


@dataclass
class CrawlResult:
    """Result of a batch crawl, with durable run/stat records when available."""

    run: Optional[CrawlRun] = None
    stats: list[CrawlRunStat] = field(default_factory=list)
    status: str = "completed"
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def crawl_run(self) -> Optional[CrawlRun]:
        return self.run

    @property
    def source_stats(self) -> list[CrawlRunStat]:
        return self.stats

    @property
    def discovered_count(self) -> int:
        return sum(stat.discovered_count for stat in self.stats)

    @property
    def fetched_count(self) -> int:
        return sum(stat.fetched_count for stat in self.stats)

    @property
    def stored_count(self) -> int:
        return sum(stat.stored_count for stat in self.stats)

    @property
    def error_count(self) -> int:
        return sum(stat.error_count for stat in self.stats)

    @property
    def already_known_count(self) -> int:
        return sum(stat.already_known_count for stat in self.stats)

    @property
    def duplicate_candidate_count(self) -> int:
        return sum(stat.duplicate_candidate_count for stat in self.stats)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "error": self.error,
            "run_id": getattr(self.run, "id", None),
            "discovered": self.discovered_count,
            "fetched": self.fetched_count,
            "stored": self.stored_count,
            "already_known": self.already_known_count,
            "duplicate_candidates": self.duplicate_candidate_count,
            "errors": self.error_count,
            "stats": [
                {
                    "zoo_id": stat.zoo_id,
                    "source_id": stat.source_id,
                    "status": stat.status,
                    "discovered": stat.discovered_count,
                    "fetched": stat.fetched_count,
                    "stored": stat.stored_count,
                    "already_known": stat.already_known_count,
                    "duplicate_candidates": stat.duplicate_candidate_count,
                    "errors": stat.errors or ([stat.error] if stat.error else []),
                }
                for stat in self.stats
            ],
        }


def _first_attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return default


class Crawler:
    """Run configured sources using an injectable fetcher and storage adapter."""

    def __init__(
        self,
        config: Any,
        storage: Any = None,
        fetcher: Any = None,
        *,
        registry: Optional[SourceRegistry] = None,
        logger: Optional[logging.Logger] = None,
        recent_recheck: int | float = 0,
    ) -> None:
        self.config = config
        self.logger = logger or LOGGER
        self.storage = storage
        self.registry = registry or SourceRegistry.from_config(config)
        self.recent_recheck = max(0.0, float(recent_recheck or _first_attr(config, "recent_recheck", "recent_recheck_days", default=0) or 0))
        if fetcher is None:
            fetcher = Fetcher(
                user_agent=str(_first_attr(config, "user_agent", default="ZooFanCrawler/0.1")),
                timeout=float(_first_attr(config, "timeout", default=20.0)),
                delay=float(_first_attr(config, "request_delay", "delay", default=1.0)),
                retries=int(_first_attr(config, "max_retries", "retries", default=3)),
            )
        self.fetcher = fetcher
        self.discovery = DiscoveryEngine(fetcher, logger=self.logger)

    def _ensure_storage(self) -> Any:
        if self.storage is not None:
            return self.storage
        from .storage import SQLiteStorage

        self.storage = SQLiteStorage(":memory:")
        return self.storage

    @staticmethod
    def _unique(values: Iterable[Any]) -> list[Any]:
        return list({id(value): value for value in values}.values())

    def _zoos(self, selection: str | Iterable[str]) -> list[Zoo]:
        requested = [selection] if isinstance(selection, str) else list(selection)
        all_zoos = self._unique(self.registry.zoos.values())
        if not requested or requested == ["all"]:
            return [zoo for zoo in all_zoos if bool(zoo.enabled)]
        wanted = {str(item).lower() for item in requested}
        aliases = {
            "berlin": "zoo-berlin",
            "zoo-berlin": "zoo-berlin",
            "koeln": "koeln-zoo",
            "koeln-zoo": "koeln-zoo",
            "cologne": "koeln-zoo",
            "gaia": "gaiazoo",
            "gaiazoo": "gaiazoo",
        }
        result: list[Zoo] = []
        for zoo in all_zoos:
            keys = {str(item).lower() for item in (zoo.id, zoo.slug, zoo.name) if item}
            keys.update({aliases.get(key, key) for key in list(keys)})
            if keys & wanted and bool(zoo.enabled):
                result.append(zoo)
        return result

    @staticmethod
    def _source_id(source: Source) -> str:
        if source.id:
            return str(source.id)
        source.id = f"{source.zoo_id or 'zoo'}:{normalize_url(source.url)}"
        return str(source.id)

    def _persist_status(self, source: Source, *, status: str, success: bool, error: Optional[str] = None, http_status: Optional[int] = None) -> None:
        source.status = status
        source.last_checked = _now()
        source.last_error = error
        if success:
            source.last_success = source.last_checked
        source.last_http_status = http_status
        if self.storage is not None and hasattr(self.storage, "update_source_status") and source.id:
            try:
                self.storage.update_source_status(
                    str(source.id), status=status, checked_at=source.last_checked,
                    success=success, error=error, http_status=http_status,
                )
            except Exception as exc:  # status recording must not stop another source
                self.logger.warning("source status persistence failed source=%s error=%s", source.id, exc)

    def _new_run(self, selection: str | Iterable[str]) -> Optional[CrawlRun]:
        if self.storage is None or not hasattr(self.storage, "start_crawl_run"):
            return None
        run = CrawlRun(batch_id=uuid.uuid4().hex, metadata={"selection": selection if isinstance(selection, str) else list(selection)})
        return self.storage.start_crawl_run(run)

    def _record_stat(self, stat: CrawlRunStat) -> CrawlRunStat:
        if self.storage is not None and hasattr(self.storage, "record_run_stat"):
            return self.storage.record_run_stat(stat)
        return stat

    def _existing_article(self, url: str) -> Optional[Article]:
        if self.storage is None:
            return None
        method = getattr(self.storage, "get_article_by_url", None)
        return method(url) if method else None

    def _record_existing_discovery(self, article: Article, source: Source, candidate: DiscoveryCandidate) -> None:
        if self.storage is None:
            return
        if hasattr(self.storage, "record_discovery"):
            self.storage.record_discovery(
                article_id=article.id, source_id=source.id,
                discovered_url=candidate.url, discovered_at=_now(), metadata=candidate.metadata,
            )

    def _upsert_article(self, article: Article, source: Source, candidate: DiscoveryCandidate) -> Any:
        if self.storage is None:
            return article, True
        method = getattr(self.storage, "upsert_article_with_outcome", None)
        if method is not None:
            outcome = method(article, source_id=source.id, discovered_url=candidate.url, discovered_at=_now())
            return outcome.article, bool(outcome.created)
        method = getattr(self.storage, "upsert_article", None) or getattr(self.storage, "save_article", None)
        if method is None:
            raise TypeError("storage must provide upsert_article(article, source_id=...)")
        return method(article, source_id=source.id, discovered_url=candidate.url, discovered_at=_now()), True

    @staticmethod
    def _recheck_enabled(source: Source, default: float) -> bool:
        config = dict(source.config or {})
        value = config.get("recent_recheck", config.get("recent_recheck_days", default))
        if isinstance(value, bool):
            return value
        try:
            return float(value or 0) > 0
        except (TypeError, ValueError):
            return False

    def _fetch_article(self, candidate: DiscoveryCandidate, source: Source, zoo: Zoo) -> Article:
        response = scoped_fetch(
            self.fetcher, candidate.url, RequestPolicy.from_zoo_source(zoo, source)
        )
        status = None
        if isinstance(response, tuple) and len(response) >= 2:
            status = int(response[0])
            raw_bytes = _body_bytes(response[1])
        elif isinstance(response, str):
            raw_bytes = _body_bytes(response)
        else:
            try:
                raw_bytes = _body_bytes(response)
            except TypeError:
                raw_bytes = None
            status_value = getattr(response, "status_code", getattr(response, "status", None))
            status = int(status_value) if status_value is not None else None
            content = getattr(response, "content", None)
            if raw_bytes is not None:
                pass
            elif content is not None:
                raw_bytes = _body_bytes(content)
            else:
                text = getattr(response, "text", "")
                text = text() if callable(text) else text
                raw_bytes = _body_bytes(text or "")
        if status is not None and status >= 400:
            policy = RequestPolicy.from_zoo_source(zoo, source)
            raise DiscoveryError(
                f"HTTP {status} for source {policy.source_id} at {policy.safe_target(candidate.url)}",
                status_code=status,
            )
        parsed = parse_article(raw_bytes, candidate.url, source.config)
        canonical = getattr(parsed, "canonical_url", None) or candidate.url
        policy = URLPolicy.from_zoo_source(zoo, source)
        if not policy.accepts(canonical, source_url=source.url):
            raise ValueError(f"article canonical URL outside official/allow policy: {canonical}")
        title = getattr(parsed, "title", None) or candidate.title
        metadata = {**(candidate.metadata or {}), **(getattr(parsed, "metadata", {}) or {})}
        language = (
            metadata.get("html_language")
            or metadata.get("structured_language")
            or source.language
            or zoo.language
        )
        return Article(
            url=candidate.url,
            canonical_url=canonical,
            title=title,
            published_at=getattr(parsed, "published_at", None) or candidate.published_at,
            updated_at_source=getattr(parsed, "updated_at_source", None) or candidate.updated_at_source,
            author=getattr(parsed, "author", None) or candidate.author,
            summary=getattr(parsed, "summary", None) or candidate.summary,
            content=getattr(parsed, "content", None),
            content_hash=getattr(parsed, "content_hash", None),
            # SHA-256 over the exact response body bytes, independent of the
            # parsed-content identity hash retained in ``content_hash``.
            html_hash=hashlib.sha256(raw_bytes).hexdigest(),
            language=language,
            http_status=status,
            crawl_status="success",
            last_fetched_at=_now(),
            raw_html=getattr(parsed, "raw_html", None),
            metadata=metadata,
        )

    def crawl(self, selection: str | Iterable[str] = "all") -> CrawlResult:
        storage = self._ensure_storage()
        # Registry is written before any network request, including disabled
        # or health-check-only sources.
        self.registry.register_storage(storage)
        run = self._new_run(selection)
        selected_zoos = self._zoos(selection)
        result = CrawlResult(run=run)
        seen_articles: dict[str, Article] = {}
        pre_run_article_ids = {
            str(article.id) for article in (storage.list_articles() if hasattr(storage, "list_articles") else [])
            if article.id is not None
        }
        resolved_article_ids: set[str] = set()
        zoo_errors = 0

        for zoo in selected_zoos:
            zoo_id = str(zoo.id or zoo.slug)
            sources = sorted(self.registry.sources_for_zoo(zoo), key=source_priority)
            if not sources:
                continue
            for source in sources:
                source_id = self._source_id(source)
                stat = CrawlRunStat(
                    crawl_run_id=getattr(run, "id", None), zoo_id=zoo_id, source_id=source_id,
                    started_at=_now(), status="running",
                )
                errors: list[str] = []
                root_http_status: Optional[int] = None
                try:
                    candidates = self.discovery.discover(source, zoo=zoo)
                    stat.discovered_count = len(candidates)
                    stat.duplicate_candidate_count = int(getattr(candidates, "duplicate_candidate_count", 0))
                    root_http_status = getattr(candidates, "root_http_status", None)
                    self._persist_status(source, status="discovered", success=False, http_status=root_http_status)
                except Exception as exc:
                    message = str(exc)
                    errors.append(message)
                    stat.error_count = 1
                    stat.errors = errors
                    stat.error = message
                    stat.status = "error"
                    zoo_errors += 1
                    self._persist_status(source, status="error", success=False, error=message, http_status=_status_code(exc))
                    safe_source = RequestPolicy.safe_target(source.url)
                    self.logger.error("crawl source failed zoo=%s source=%s error=%s", zoo.slug, safe_source, message)
                    stat.finished_at = _now()
                    result.stats.append(self._record_stat(stat))
                    continue

                recheck = self._recheck_enabled(source, self.recent_recheck)
                for candidate in candidates:
                    key = normalize_url(candidate.url)
                    if not key:
                        continue
                    existing = seen_articles.get(key) or self._existing_article(key)
                    if existing is not None and not recheck:
                        self._record_existing_discovery(existing, source, candidate)
                        seen_articles[key] = existing
                        identity = str(existing.id)
                        if identity in resolved_article_ids:
                            stat.duplicate_candidate_count += 1
                        elif identity in pre_run_article_ids:
                            stat.already_known_count += 1
                        else:
                            stat.duplicate_candidate_count += 1
                        resolved_article_ids.add(identity)
                        continue
                    try:
                        article = self._fetch_article(candidate, source, zoo)
                        stat.fetched_count += 1
                        stored, created = self._upsert_article(article, source, candidate)
                        if created:
                            stat.stored_count += 1
                        identity = str(stored.id)
                        if not created and identity in resolved_article_ids:
                            stat.duplicate_candidate_count += 1
                        elif not created and identity in pre_run_article_ids:
                            stat.already_known_count += 1
                        elif not created:
                            stat.duplicate_candidate_count += 1
                        resolved_article_ids.add(identity)
                        seen_articles[key] = stored
                        # The canonical URL may differ from the discovered URL.
                        seen_articles[normalize_url(getattr(stored, "canonical_url", None) or key)] = stored
                    except Exception as exc:
                        safe_candidate = RequestPolicy.safe_target(candidate.url)
                        message = f"{safe_candidate}: {exc}"
                        errors.append(message)
                        stat.error_count += 1
                        self.logger.warning(
                            "crawl article failed zoo=%s source=%s url=%s error=%s",
                            zoo.slug, RequestPolicy.safe_target(source.url), safe_candidate, exc,
                        )

                stat.errors = errors
                stat.status = "completed" if not errors else "partial"
                stat.error = "; ".join(errors) if errors else None
                stat.finished_at = _now()
                # Discovery succeeded even when an individual article page
                # failed.  Keep ``last_success`` truthful at source level;
                # article errors remain visible in the per-source run stat.
                self._persist_status(source, status=stat.status, success=True, error=stat.error, http_status=root_http_status)
                self.logger.info(
                    "crawl source zoo=%s source=%s status=%s discovered=%d fetched=%d stored=%d errors=%d",
                    zoo.slug, RequestPolicy.safe_target(source.url), stat.status, stat.discovered_count, stat.fetched_count,
                    stat.stored_count, stat.error_count,
                )
                result.stats.append(self._record_stat(stat))

        result.status = "completed_with_errors" if zoo_errors or result.error_count else "completed"
        result.metadata = {"zoos": len(selected_zoos), "sources": len(result.stats)}
        if run is not None and hasattr(storage, "finish_crawl_run"):
            finished = storage.finish_crawl_run(getattr(run, "id", ""), status=result.status)
            if finished is not None:
                result.run = finished
        self.logger.info(
            "crawl batch status=%s zoos=%d sources=%d discovered=%d fetched=%d stored=%d errors=%d",
            result.status, len(selected_zoos), len(result.stats), result.discovered_count,
            result.fetched_count, result.stored_count, result.error_count,
        )
        return result

    def crawl_all(self) -> CrawlResult:
        return self.crawl("all")

    def crawl_zoo(self, slug: str) -> CrawlResult:
        return self.crawl(slug)

    run = crawl


def crawl(config: Any, *, selection: str = "all", storage: Any = None, fetcher: Any = None, **kwargs: Any) -> CrawlResult:
    """Functional entry point for scripts and integrations."""

    return Crawler(config, storage=storage, fetcher=fetcher, **kwargs).crawl(selection)


# Friendly aliases for integrations that call this component an orchestrator.
CrawlerOrchestrator = Crawler
run_crawl = crawl


__all__ = ["Crawler", "CrawlerOrchestrator", "CrawlResult", "crawl", "run_crawl"]
