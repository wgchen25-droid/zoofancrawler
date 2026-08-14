"""Batch crawler orchestration.

``Crawler`` coordinates configured source discovery, article fetching,
normalization, deduplication and storage.  It keeps failures at source and zoo
boundaries so one broken endpoint cannot abort the batch.
"""

from __future__ import annotations

import logging
import hashlib
import inspect
import uuid
from copy import copy
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, cast

from .discovery import DiscoveryCandidate, DiscoveryEngine, DiscoveryError, SourceRegistry, URLPolicy, source_priority
from .fetcher import Fetcher, RequestPolicy, scoped_fetch
from .models import Article, CrawlRun, CrawlRunStat, Source, Zoo
from .normalization import normalize_url
from .parsers import parse_article, parse_datetime
from .statuses import (
    ALL_STATUSES,
    ERROR_STATUSES,
    ContentParseError,
    DateParseError,
    JavascriptRequiredError,
    classify_error,
    sanitize_error,
)


LOGGER = logging.getLogger(__name__)


_PHASE0_EMPTY_SOURCE_LIMITATIONS = frozenset(
    {
        "robots_disallowed",
        "javascript_required",
        "blocked",
        "unsupported",
        "partial",
        "source_not_found",
        "success_no_items",
    }
)


@dataclass
class _UpsertResult:
    """Compatibility wrapper for old and outcome-aware storage adapters."""

    article: Article
    created: bool
    updated: bool = False

    def __iter__(self):
        # Existing integrations occasionally unpack the private helper as a
        # two-tuple.  Keep that behavior while exposing the new update signal.
        yield self.article
        yield self.created


@dataclass
class _FallbackZooResult:
    """Record used only while running against a legacy storage/model pair."""

    crawl_run_id: Optional[str] = None
    zoo_id: Optional[str] = None
    zoo_slug: Optional[str] = None
    zoo_name: Optional[str] = None
    status: str = "failed"
    source_status: Optional[str] = None
    error_category: Optional[str] = None
    discovered: int = 0
    parsed: int = 0
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    duplicate_filtered: int = 0
    duration_ms: Optional[int] = None
    source_url: Optional[str] = None
    http_status: Optional[int] = None
    error_summary: Optional[str] = None
    started_at: Any = None
    finished_at: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _status_code(error: BaseException) -> Optional[int]:
    value = getattr(error, "status_code", getattr(error, "status", None))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _positive_limit(value: Any, name: str) -> Optional[int]:
    """Validate an optional bounded-smoke limit.

    Limits are deliberately strict at the crawler boundary so integrations
    cannot accidentally turn a malformed value into an unbounded crawl.  A
    value of ``None`` retains the historical unlimited behavior.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


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


def _config_flag(value: Any) -> bool:
    """Interpret an explicit configuration flag without treating prose as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {
            "1", "true", "yes", "y", "on", "required", "enabled",
        }
    return False


def _source_requires_javascript(source: Source) -> bool:
    """Return true only when the source explicitly opts into JS rendering."""

    for attribute in (
        "javascript_required", "requires_javascript", "requires_js", "js_required",
    ):
        if _config_flag(getattr(source, attribute, False)):
            return True
    config = dict(getattr(source, "config", {}) or {})
    for key in (
        "javascript_required",
        "requires_javascript",
        "requires_js",
        "js_required",
        "javascript",
        "js",
        "client_rendered",
        "javascript_only",
    ):
        if key in config and _config_flag(config[key]):
            return True
    rendering = config.get("rendering", config.get("renderer"))
    if isinstance(rendering, str) and rendering.strip().casefold() in {
        "javascript", "js", "browser", "client",
    }:
        return True
    return str(getattr(source, "kind", "")).strip().casefold() in {
        "javascript", "javascript_required", "js_required",
    }


@dataclass
class CrawlResult:
    """Result of a batch crawl, with durable run/stat records when available."""

    run: Optional[CrawlRun] = None
    stats: list[CrawlRunStat] = field(default_factory=list)
    zoo_results: list[Any] = field(default_factory=list)
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
        return self.inserted_count or sum(stat.stored_count for stat in self.stats)

    @property
    def parsed_count(self) -> int:
        if self.zoo_results:
            return sum(int(_first_attr(item, "parsed", "parsed_count", default=0) or 0) for item in self.zoo_results)
        return self.fetched_count

    @property
    def inserted_count(self) -> int:
        if self.zoo_results:
            return sum(int(_first_attr(item, "inserted", "inserted_count", default=0) or 0) for item in self.zoo_results)
        return sum(stat.stored_count for stat in self.stats)

    @property
    def updated_count(self) -> int:
        return sum(int(_first_attr(item, "updated", "updated_count", default=0) or 0) for item in self.zoo_results)

    @property
    def failed_count(self) -> int:
        if self.zoo_results:
            return sum(int(_first_attr(item, "failed", "failed_count", default=0) or 0) for item in self.zoo_results)
        return sum(stat.error_count for stat in self.stats)

    @property
    def error_count(self) -> int:
        return self.failed_count

    @property
    def already_known_count(self) -> int:
        return sum(stat.already_known_count for stat in self.stats)

    @property
    def duplicate_candidate_count(self) -> int:
        return self.duplicate_filtered_count

    @property
    def duplicate_filtered_count(self) -> int:
        if self.zoo_results:
            return sum(int(_first_attr(item, "duplicate_filtered", "duplicate_filtered_count", default=0) or 0) for item in self.zoo_results)
        return sum(stat.duplicate_candidate_count for stat in self.stats)

    @property
    def since_filtered_count(self) -> int:
        if self.zoo_results:
            return sum(
                int(
                    (
                        _first_attr(item, "metadata", default={}) or {}
                    ).get(
                        "since_filtered",
                        (
                            _first_attr(item, "metadata", default={}) or {}
                        ).get("since_filtered_count", 0),
                    )
                    or 0
                )
                for item in self.zoo_results
            )
        return sum(
            int((stat.metadata or {}).get("since_filtered_count", 0) or 0)
            for stat in self.stats
        )

    @property
    def since_filtered(self) -> int:
        return self.since_filtered_count

    @property
    def candidates_processed_count(self) -> int:
        return int(self.metadata.get("candidates_processed", 0) or 0)

    @property
    def candidates_limited_count(self) -> int:
        return int(self.metadata.get("candidates_limited", 0) or 0)

    @property
    def error_classifications(self) -> list[str]:
        """Return stable runtime categories represented by this run."""

        values: list[str] = []
        for stat in self.stats:
            metadata = getattr(stat, "metadata", {}) or {}
            categories = metadata.get("error_classifications", [])
            if isinstance(categories, str):
                categories = [categories]
            for category in categories or []:
                normalized = str(category).strip().lower().replace("-", "_")
                if normalized and normalized not in values:
                    values.append(normalized)
        for item in self.zoo_results:
            metadata = _first_attr(item, "metadata", default={}) or {}
            categories = metadata.get("error_categories", [])
            if isinstance(categories, str):
                categories = [categories]
            for category in categories or []:
                normalized = str(category).strip().lower().replace("-", "_")
                if normalized and normalized not in values:
                    values.append(normalized)
        return values

    @staticmethod
    def _zoo_result_dict(value: Any) -> dict[str, Any]:
        """Serialize a model/fallback zoo result without assuming one schema."""

        if hasattr(value, "as_dict") and callable(value.as_dict):
            try:
                payload = value.as_dict()
                if isinstance(payload, Mapping):
                    return dict(payload)
            except Exception:
                # A result should remain inspectable even if an optional model
                # serializer has a bug; fall through to dataclass/attributes.
                pass
        if isinstance(value, Mapping):
            payload = dict(value)
            if "source_status" not in payload and "error_category" in payload:
                payload["source_status"] = payload.get("error_category")
            return payload
        if is_dataclass(value) and not isinstance(value, type):
            payload = asdict(value)
            for name in ("source_status", "zoo_slug", "zoo_name"):
                if name not in payload and hasattr(value, name):
                    payload[name] = getattr(value, name)
            return payload
        try:
            return dict(vars(value))
        except TypeError:
            return {"value": str(value)}

    def as_dict(self) -> dict[str, Any]:
        processed = int(self.metadata.get("processed", len(self.zoo_results)) or 0)
        enabled = int(self.metadata.get("enabled", self.metadata.get("enabled_zoos_count", processed)) or 0)
        processed_zoos = list(self.metadata.get("processed_zoos", []))
        enabled_zoos = list(self.metadata.get("enabled_zoos", []))
        selected_zoos = list(self.metadata.get("selected_zoos", processed_zoos))
        result_counts: dict[str, int] = {}
        for item in self.zoo_results:
            zoo_id = _first_attr(item, "zoo_id", default=None)
            if zoo_id is None:
                continue
            key = str(zoo_id)
            result_counts[key] = result_counts.get(key, 0) + 1
        missing_zoos = [zoo_id for zoo_id in processed_zoos if result_counts.get(str(zoo_id), 0) == 0]
        duplicate_zoos = [
            zoo_id for zoo_id in processed_zoos if result_counts.get(str(zoo_id), 0) > 1
        ]
        exactly_one = bool(processed_zoos) and not missing_zoos and not duplicate_zoos and len(result_counts) == len(processed_zoos)
        coverage = {
            "processed": processed,
            "enabled": enabled,
            "processed_zoos": processed_zoos,
            "enabled_zoos": enabled_zoos,
            "selected_zoos": selected_zoos,
            "zoo_result_counts": result_counts,
            "missing_processed_zoos": missing_zoos,
            "duplicate_processed_zoos": duplicate_zoos,
            "exactly_one_result_per_processed_zoo": exactly_one,
        }
        return {
            "status": self.status,
            "error": self.error,
            "run_id": getattr(self.run, "id", None),
            "processed": processed,
            "enabled": enabled,
            "metadata": dict(self.metadata),
            "processed_zoos": processed_zoos,
            "enabled_zoos": enabled_zoos,
            "selected_zoos": selected_zoos,
            "coverage": coverage,
            "discovered": self.discovered_count,
            "fetched": self.fetched_count,
            "stored": self.stored_count,
            "parsed": self.parsed_count,
            "inserted": self.inserted_count,
            "updated": self.updated_count,
            "failed": self.failed_count,
            "duplicate_filtered": self.duplicate_filtered_count,
            "since_filtered": self.since_filtered_count,
            "candidates_processed": self.candidates_processed_count,
            "candidates_limited": self.candidates_limited_count,
            "error_classifications": self.error_classifications,
            "error_categories": self.error_classifications,
            "parsed_count": self.parsed_count,
            "inserted_count": self.inserted_count,
            "updated_count": self.updated_count,
            "failed_count": self.failed_count,
            "duplicate_filtered_count": self.duplicate_filtered_count,
            "since_filtered_count": self.since_filtered_count,
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
                    "error_category": getattr(stat, "error_category", None)
                    or (stat.metadata or {}).get("error_classification"),
                    "metadata": dict(stat.metadata or {}),
                    "error_classifications": list(
                        (stat.metadata or {}).get("error_classifications", [])
                    ),
                }
                for stat in self.stats
            ],
            "zoo_results": [self._zoo_result_dict(item) for item in self.zoo_results],
        }


def _first_attr(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        result = getattr(value, name, None)
        if result is not None:
            return result
    return default


def _accepted_parameters(factory: Any) -> Optional[set[str]]:
    """Return constructor parameter names when introspection is available."""

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return None
    parameters = set()
    accepts_kwargs = False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_kwargs = True
        elif parameter.name != "self":
            parameters.add(parameter.name)
    return None if accepts_kwargs else parameters


def _construct_model(factory: Any, values: Mapping[str, Any]) -> Any:
    """Construct a model using only fields supported by its stable schema."""

    accepted = _accepted_parameters(factory)
    payload = dict(values) if accepted is None else {
        key: value for key, value in values.items() if key in accepted
    }
    return factory(**payload)


def _set_optional_attributes(target: Any, values: Mapping[str, Any]) -> Any:
    """Attach newly introduced evidence fields to legacy model instances."""

    for key, value in values.items():
        if value is None:
            continue
        try:
            setattr(target, key, value)
        except Exception:
            pass
    return target


def _outcome_value(outcome: Any, name: str, default: Any = None) -> Any:
    if isinstance(outcome, Mapping):
        return outcome.get(name, default)
    return getattr(outcome, name, default)


class Crawler:
    """Run configured sources using an injectable fetcher and storage adapter."""

    def __init__(
        self,
        config: Any,
        storage: Any = None,
        fetcher: Any = None,
        *,
        registry: Optional[SourceRegistry] = None,
        adapter_registry: Optional[Any] = None,
        custom_adapters: Optional[Any] = None,
        logger: Optional[logging.Logger] = None,
        recent_recheck: int | float = 0,
        max_candidates_per_source: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> None:
        self.config = config
        self.logger = logger or LOGGER
        self.storage = storage
        self.registry = registry or SourceRegistry.from_config(config)
        configured_adapters = _first_attr(config, "adapter_registry", "custom_adapters", default=None)
        self.adapter_registry = adapter_registry if adapter_registry is not None else (
            custom_adapters if custom_adapters is not None else configured_adapters
        )
        self.recent_recheck = max(0.0, float(recent_recheck or _first_attr(config, "recent_recheck", "recent_recheck_days", default=0) or 0))
        self.max_candidates_per_source = _positive_limit(
            max_candidates_per_source, "max_candidates_per_source"
        )
        self.max_pages = _positive_limit(max_pages, "max_pages")
        if fetcher is None:
            fetcher = Fetcher(
                user_agent=str(_first_attr(config, "user_agent", default="ZooFanCrawler/0.1")),
                timeout=float(_first_attr(config, "timeout", default=20.0)),
                delay=float(_first_attr(config, "request_delay", "delay", default=1.0)),
                retries=int(_first_attr(config, "max_retries", "retries", default=3)),
            )
        self.fetcher = fetcher
        self.discovery = DiscoveryEngine(
            fetcher,
            logger=self.logger,
            adapter_registry=self.adapter_registry,
        )

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
        if not requested or (len(requested) == 1 and str(requested[0]).lower() == "all"):
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

    @staticmethod
    def _has_authoritative_phase0_provenance(zoo: Zoo) -> bool:
        """Return whether ``zoo`` carries the closed Phase 0 evidence record."""

        provenance = _first_attr(zoo, "list_provenance", default=[])
        if not isinstance(provenance, (list, tuple)) or len(provenance) != 1:
            return False
        entry = provenance[0]
        if not isinstance(entry, Mapping):
            return False
        return (
            entry.get("source") == "data/zoofan_phase0_zoo_list_v0_1.md"
            and entry.get("version") == "Phase0 v0.1"
            and entry.get("sha256") == "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd"
            and entry.get("authority") == "user_designated_authoritative_membership_list"
            and entry.get("expanded_count") == 73
        )

    @staticmethod
    def _declared_source_status(value: Any, *, default: str = "") -> str:
        """Normalize configuration/runtime source-status aliases."""

        status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if status == "not_found":
            return "source_not_found"
        return status or default

    @classmethod
    def _is_authoritative_phase0_no_source(cls, zoo: Zoo) -> bool:
        """Return whether an empty source set is an explicit Phase 0 gap.

        A configured zoo with no executable source is normally a configuration
        failure.  The authoritative Phase 0 roster has a deliberately narrow
        exception for rows whose local evidence says that no official site was
        verified.  Keep the check local and conservative: runtime crawling
        must not turn an arbitrary ``source_status=not_found`` (or a missing
        source declaration in a hand-written config) into a successful crawl.

        The config validator is the primary guard for these fields.  Repeating
        the stable evidence markers here protects callers that construct
        :class:`Zoo` records directly and lets the crawler retain the same
        semantics without network or filesystem access.
        """

        website_url = _first_attr(zoo, "website_url", "url", default=None)
        if website_url:
            return False
        source_status = str(_first_attr(zoo, "source_status", default="") or "").strip().lower()
        if source_status not in {"not_found", "source_not_found"}:
            return False

        metadata = _first_attr(zoo, "metadata", default={})
        if not isinstance(metadata, Mapping):
            return False
        if (
            metadata.get("no_official_website_verified") is not True
            or metadata.get("no_executable_source") is not True
        ):
            return False
        import_gate = metadata.get("import_gate")
        if not isinstance(import_gate, Mapping):
            return False
        blockers = import_gate.get("blockers", ())
        if isinstance(blockers, str):
            blockers = (blockers,)
        if not isinstance(blockers, (list, tuple, set)):
            return False
        normalized_blockers = {
            str(value).strip().lower() for value in blockers if str(value).strip()
        }
        if "missing_official_website_url" not in normalized_blockers:
            return False

        # These values are the closed local evidence boundary used by config
        # validation.  ``source_line`` and ``checked_at`` are useful evidence,
        # but are intentionally not required at runtime because older valid
        # imports may not carry either optional field.
        if not cls._has_authoritative_phase0_provenance(zoo):
            return False
        return True

    def _persist_status(self, source: Source, *, status: str, success: bool, error: Optional[str] = None, http_status: Optional[int] = None) -> Optional[tuple[str, str]]:
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
                category, message = self._error_detail(exc, phase="storage")
                self.logger.warning("source status persistence failed source=%s error=%s", source.id, message)
                return category, message
        return None

    def _new_run(self, selection: str | Iterable[str]) -> Optional[CrawlRun]:
        if self.storage is None or not hasattr(self.storage, "start_crawl_run"):
            return None
        run = CrawlRun(batch_id=uuid.uuid4().hex, metadata={"selection": selection if isinstance(selection, str) else list(selection)})
        return self.storage.start_crawl_run(run)

    def _record_stat(self, stat: CrawlRunStat) -> CrawlRunStat:
        if self.storage is not None and hasattr(self.storage, "record_run_stat"):
            return self.storage.record_run_stat(stat)
        return stat

    def _record_stat_checked(
        self, stat: CrawlRunStat
    ) -> tuple[CrawlRunStat, Optional[tuple[str, str]]]:
        """Record a stat without allowing one storage failure to abort a batch."""

        try:
            persisted = self._record_stat(stat)
            # ``error_category`` was added after the original run-stat model;
            # retain it on adapters that return a reconstructed legacy object.
            category = (stat.metadata or {}).get("error_classification")
            if category:
                _set_optional_attributes(persisted, {"error_category": category})
            return persisted, None
        except Exception as exc:
            category, message = self._error_detail(exc, phase="storage")
            stat.status = "error"
            stat.error_count += 1
            stat.errors = [*(stat.errors or []), message]
            stat.error = "; ".join(stat.errors)
            stat.metadata["error_classification"] = category
            stat.metadata["error_classifications"] = list(
                dict.fromkeys([*(stat.metadata.get("error_classifications", []) or []), category])
            )
            _set_optional_attributes(stat, {"error_category": category})
            return stat, (category, message)

    def _existing_article(self, url: str) -> Optional[Article]:
        if self.storage is None:
            return None
        method = getattr(self.storage, "get_article_by_url", None)
        return method(url) if method else None

    @staticmethod
    def _candidate_identity_keys(
        candidate: DiscoveryCandidate, source: Source, zoo: Zoo
    ) -> list[str]:
        """Return the discovered and explicitly declared canonical keys.

        The discovered URL remains the request URL and relationship evidence.
        A feed-level canonical is an additional identity probe only when it is
        an accepted official article URL; it must not weaken the URL policy.
        """

        keys: list[str] = []
        for value in (
            _first_attr(candidate, "url", default=None),
            _first_attr(candidate, "canonical_url", default=None),
        ):
            normalized = normalize_url(value)
            if not normalized or normalized in keys:
                continue
            if normalized != normalize_url(_first_attr(candidate, "url", default=None)):
                try:
                    policy = URLPolicy.from_zoo_source(zoo, source)
                    if not policy.accepts(normalized, source_url=source.url):
                        continue
                except Exception:
                    # Discovery already validated the candidate URL.  A
                    # malformed optional canonical must not prevent fetching
                    # the discovered URL or turn a cache probe into a source
                    # failure.
                    continue
            keys.append(normalized)
        return keys

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
            return _UpsertResult(article, True, False)
        method = getattr(self.storage, "upsert_article_with_outcome", None)
        if method is not None:
            outcome = method(article, source_id=source.id, discovered_url=candidate.url, discovered_at=_now())
            persisted = _outcome_value(outcome, "article", article)
            created = bool(_outcome_value(outcome, "created", False))
            updated = bool(_outcome_value(outcome, "updated", False))
            action = str(_outcome_value(outcome, "action", _outcome_value(outcome, "operation", "")) or "").lower()
            if action in {"updated", "update"}:
                updated = True
            return _UpsertResult(persisted, created, updated)
        method = getattr(self.storage, "upsert_article", None) or getattr(self.storage, "save_article", None)
        if method is None:
            raise TypeError("storage must provide upsert_article(article, source_id=...)")
        return _UpsertResult(
            method(article, source_id=source.id, discovered_url=candidate.url, discovered_at=_now()),
            True,
            False,
        )

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

    @staticmethod
    def _bounded_discovery_source(source: Source, max_pages: Optional[int]) -> Source:
        """Return a source view with a narrowed pagination budget.

        Discovery owns its adapters and their transport policy.  The crawler
        therefore supplies a shallow source copy with a temporary, narrower
        parser setting instead of mutating the configured source or teaching
        each adapter about CLI-only smoke options.  RSS/Atom sources are
        unchanged; archive and sitemap sources use the same bound for their
        respective page/file traversal.
        """

        if max_pages is None:
            return source
        kind = str(source.kind or "").lower().replace("-", "_")
        if kind not in {"archive", "html", "news_archive", "sitemap"}:
            return source
        bounded = copy(source)
        config = dict(source.config or {})
        key = "max_sitemaps" if kind == "sitemap" else "max_pages"
        configured = config.get(key)
        if configured is None:
            configured_limit = max_pages
        else:
            try:
                configured_limit = int(configured)
            except (TypeError, ValueError):
                configured_limit = max_pages
        config[key] = max(1, min(max_pages, configured_limit))
        bounded.config = config
        return bounded

    def _effective_limits(
        self,
        max_candidates_per_source: Optional[int],
        max_pages: Optional[int],
    ) -> tuple[Optional[int], Optional[int], Optional[int]]:
        """Resolve per-call limits and return ``(candidate, pages, effective)``.

        ``max_pages`` bounds source pagination; it is intentionally separate
        from article-candidate processing because one archive page may contain
        multiple legitimate articles.  ``effective`` is retained as a small
        compatibility/diagnostic alias for the candidate limit.
        """

        candidate_limit = (
            self.max_candidates_per_source
            if max_candidates_per_source is None
            else _positive_limit(max_candidates_per_source, "max_candidates_per_source")
        )
        page_limit = self.max_pages if max_pages is None else _positive_limit(max_pages, "max_pages")
        return candidate_limit, page_limit, candidate_limit

    @staticmethod
    def _published_at_utc(value: Any) -> Optional[datetime]:
        """Parse publication evidence for a date-window filter.

        Unknown or malformed dates deliberately return ``None``.  A missing
        publication date must not become a fetched-at substitute.
        """

        parsed: Optional[datetime]
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            parsed = datetime.combine(value, datetime.min.time())
        else:
            parsed = parse_datetime(value)
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _is_before_cutoff(cls, value: Any, cutoff: Optional[datetime]) -> bool:
        if cutoff is None:
            return False
        published_at = cls._published_at_utc(value)
        return published_at is not None and published_at < cutoff

    @staticmethod
    def _article_from_parsed(
        *,
        candidate: DiscoveryCandidate,
        parsed: Any,
        raw_bytes: bytes,
        source: Source,
        zoo: Zoo,
        canonical: str,
        status: Optional[int],
        metadata: Mapping[str, Any],
        language: Optional[str],
    ) -> Article:
        """Build an Article while accepting evidence fields from newer parsers."""

        parsed_published = _first_attr(parsed, "published_at", default=None)
        published_at = parsed_published or _first_attr(candidate, "published_at", default=None)
        optional_values = {
            "published_at_raw": _first_attr(parsed, "published_at_raw", default=None)
            or _first_attr(candidate, "published_at_raw", default=None),
            "content_html": _first_attr(parsed, "content_html", default=None)
            or metadata.get("content_html"),
            "image_url": _first_attr(parsed, "image_url", default=None)
            or metadata.get("image_url")
            or metadata.get("og:image")
            or metadata.get("twitter:image"),
            "parse_status": _first_attr(parsed, "parse_status", default=None)
            or metadata.get("parse_status")
            or "success",
        }
        optional_values["published_at_raw"] = (
            optional_values["published_at_raw"] or metadata.get("published_at_raw")
        )
        values = {
            "url": candidate.url,
            "canonical_url": canonical,
            "title": _first_attr(parsed, "title", default=None) or candidate.title,
            "published_at": published_at,
            "updated_at_source": _first_attr(parsed, "updated_at_source", default=None) or candidate.updated_at_source,
            "author": _first_attr(parsed, "author", default=None) or candidate.author,
            "summary": _first_attr(parsed, "summary", default=None) or candidate.summary,
            "content": _first_attr(parsed, "content", default=None),
            "content_hash": _first_attr(parsed, "content_hash", default=None),
            # SHA-256 over the exact response body bytes, independent of the
            # parsed-content identity hash retained in ``content_hash``.
            "html_hash": hashlib.sha256(raw_bytes).hexdigest(),
            "language": language,
            "http_status": status,
            "crawl_status": "success",
            "last_fetched_at": _now(),
            "raw_html": _first_attr(parsed, "raw_html", default=None),
            "metadata": dict(metadata),
            **optional_values,
        }
        article = _construct_model(Article, values)
        return _set_optional_attributes(article, optional_values)

    @staticmethod
    def _zoo_result_values(
        *,
        run: Optional[CrawlRun],
        zoo: Zoo,
        status: str,
        source_status: Optional[str],
        discovered: int,
        parsed: int,
        inserted: int,
        updated: int,
        failed: int,
        duplicate_filtered: int,
        duration_ms: Optional[int],
        source_url: Optional[str],
        http_status: Optional[int],
        error_summary: list[str],
        started_at: Any,
        finished_at: Any,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Canonical values shared by model construction and JSON output."""

        run_id = getattr(run, "id", None)
        zoo_id = str(zoo.id or zoo.slug)
        # Include both concise and storage-oriented aliases.  The model
        # constructor filters to whichever schema is installed, while the
        # fallback record retains the concise names requested by the API.
        return {
            "id": None,
            "crawl_run_id": run_id,
            "run_id": run_id,
            "zoo_id": zoo_id,
            "zoo_slug": zoo.slug,
            "zoo_name": zoo.name,
            "status": status,
            "source_status": source_status,
            "error_category": (
                source_status
                if status not in {"success", "success_no_items"}
                else None
            ),
            "discovered": discovered,
            "discovered_count": discovered,
            "parsed": parsed,
            "parsed_count": parsed,
            "inserted": inserted,
            "inserted_count": inserted,
            "updated": updated,
            "updated_count": updated,
            "failed": failed,
            "failed_count": failed,
            "duplicate_filtered": duplicate_filtered,
            "duplicate_filtered_count": duplicate_filtered,
            "duration": duration_ms,
            "duration_ms": duration_ms,
            "source_url": source_url,
            "http_status": http_status,
            "error_summary": "; ".join(error_summary) if error_summary else None,
            "started_at": started_at,
            "finished_at": finished_at,
            "metadata": dict(metadata or {}),
        }

    @staticmethod
    def _build_zoo_result(values: Mapping[str, Any]) -> Any:
        """Use the shared model when present, with a legacy-safe fallback."""

        try:
            from . import models as models_module

            factory = getattr(models_module, "CrawlZooResult", None)
        except Exception:  # pragma: no cover - package import is stable
            factory = None
        if factory is None:
            return _FallbackZooResult(
                crawl_run_id=cast(Optional[str], values.get("crawl_run_id")),
                zoo_id=cast(Optional[str], values.get("zoo_id")),
                zoo_slug=cast(Optional[str], values.get("zoo_slug")),
                zoo_name=cast(Optional[str], values.get("zoo_name")),
                status=cast(str, values.get("status")),
                source_status=cast(Optional[str], values.get("source_status")),
                error_category=cast(Optional[str], values.get("error_category")),
                discovered=cast(int, values.get("discovered")),
                parsed=cast(int, values.get("parsed")),
                inserted=cast(int, values.get("inserted")),
                updated=cast(int, values.get("updated")),
                failed=cast(int, values.get("failed")),
                duplicate_filtered=cast(int, values.get("duplicate_filtered")),
                duration_ms=cast(Optional[int], values.get("duration_ms")),
                source_url=cast(Optional[str], values.get("source_url")),
                http_status=cast(Optional[int], values.get("http_status")),
                error_summary=cast(Optional[str], values.get("error_summary")),
                started_at=values.get("started_at"),
                finished_at=values.get("finished_at"),
                metadata=cast(dict[str, Any], values.get("metadata")),
            )
        built = _construct_model(factory, values)
        return _set_optional_attributes(
            built,
            {
                "source_status": values.get("source_status"),
                "zoo_slug": values.get("zoo_slug"),
                "zoo_name": values.get("zoo_name"),
            },
        )

    def _record_zoo_result(self, result: Any, run: Optional[CrawlRun]) -> Any:
        """Persist one result when the storage adapter supports zoo results."""

        if self.storage is None:
            return result
        method = getattr(self.storage, "upsert_zoo_run_result", None)
        if method is None:
            return result
        run_id = getattr(run, "id", None)
        # Adapters shipped during the migration use one of these compatible
        # signatures.  Inspecting names avoids catching a TypeError raised by
        # the adapter itself and accidentally hiding a persistence failure.
        try:
            signature = inspect.signature(method)
            params = [
                parameter for parameter in signature.parameters.values()
                if parameter.name != "self"
            ]
        except (TypeError, ValueError):
            params = []
        names = {parameter.name for parameter in params}
        if params and params[0].kind is inspect.Parameter.KEYWORD_ONLY and params[0].name in {"result", "zoo_result", "item"}:
            persisted = method(**{params[0].name: result})
        elif params and params[0].name in {"run_id", "crawl_run_id", "crawl_id"} and len(params) >= 2:
            persisted = method(run_id, result)
        elif (
            len(params) >= 2
            and params[0].name in {"result", "zoo_result", "item"}
            and params[1].name in {"run_id", "crawl_run_id", "crawl_id"}
            and params[1].default is inspect.Parameter.empty
        ):
            persisted = method(result, run_id)
        elif "crawl_run_id" in names and "result" not in names and "zoo_result" not in names:
            persisted = method(result, crawl_run_id=run_id)
        elif "run_id" in names and "result" not in names and "zoo_result" not in names:
            persisted = method(result, run_id=run_id)
        else:
            persisted = method(result)
        persisted = persisted if persisted is not None else result
        return _set_optional_attributes(
            persisted,
            {
                "source_status": _first_attr(result, "source_status", default=None),
                "zoo_slug": _first_attr(result, "zoo_slug", default=None),
                "zoo_name": _first_attr(result, "zoo_name", default=None),
            },
        )

    @staticmethod
    def _error_detail(exc: BaseException, *, phase: str) -> tuple[str, str]:
        category = classify_error(exc, phase=phase)
        message = sanitize_error(str(exc) or type(exc).__name__)
        return category, message

    @staticmethod
    def _mark_zoo_result_storage_failure(
        result: Any, category: str, message: str
    ) -> Any:
        summary = f"{category}: {message}"
        metadata = dict(_first_attr(result, "metadata", default={}) or {})
        metadata.setdefault("error_sources", [])
        metadata["error_categories"] = list(
            dict.fromkeys([*(metadata.get("error_categories", []) or []), category])
        )
        _set_optional_attributes(
            result,
            {
                "status": "failed",
                "source_status": category,
                "error_category": category,
                "error_summary": summary,
                "failed": max(1, int(_first_attr(result, "failed", default=0) or 0)),
                "metadata": metadata,
            },
        )
        return result

    def _record_zoo_exception(
        self,
        result: CrawlResult,
        run: Optional[CrawlRun],
        zoo: Zoo,
        started_at: datetime,
        exc: BaseException,
        *,
        phase: str = "configuration",
    ) -> Any:
        """Materialize an unexpected zoo-boundary failure exactly once.

        The normal source path records expected discovery/article/storage
        failures in a source stat.  This boundary handles failures before a
        source stat exists (selection, sorting, or setup) and keeps the
        outer zoo loop alive.  It intentionally tolerates result persistence
        failures because the in-memory result is still useful to callers.
        """

        zoo_id = str(zoo.id or zoo.slug)
        for existing in result.zoo_results:
            if str(_first_attr(existing, "zoo_id", default="")) == zoo_id:
                return existing
        category, message = self._error_detail(exc, phase=phase)
        website = _first_attr(zoo, "website_url", "url", default=None)
        safe_source = RequestPolicy.safe_target(str(website)) if website else None
        finished_at = _now()
        error_metadata = {
            "error_sources": [safe_source] if safe_source else [],
            "error_categories": [category],
            "since_filtered": 0,
            "since_filtered_count": 0,
        }
        values = self._zoo_result_values(
            run=run,
            zoo=zoo,
            status="failed",
            source_status=category,
            discovered=0,
            parsed=0,
            inserted=0,
            updated=0,
            failed=1,
            duplicate_filtered=0,
            duration_ms=max(0, int((finished_at - started_at).total_seconds() * 1000)),
            source_url=safe_source,
            http_status=_status_code(exc),
            error_summary=[f"{category}: {message}"],
            started_at=started_at,
            finished_at=finished_at,
            metadata=error_metadata,
        )
        zoo_result = self._build_zoo_result(values)
        try:
            persisted = self._record_zoo_result(zoo_result, run)
        except Exception as storage_exc:
            storage_category, storage_message = self._error_detail(storage_exc, phase="storage")
            persisted = self._mark_zoo_result_storage_failure(
                zoo_result, storage_category, storage_message
            )
        result.zoo_results.append(persisted)
        self.logger.error(
            "crawl zoo failed zoo=%s category=%s error=%s",
            zoo_id,
            category,
            message,
        )
        return persisted

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
            request_policy = RequestPolicy.from_zoo_source(zoo, source)
            raise DiscoveryError(
                f"HTTP {status} for source {request_policy.source_id} at {request_policy.safe_target(candidate.url)}",
                status_code=status,
            )
        if raw_bytes is None:
            raise TypeError("response body must be text or bytes-like")
        parsed = parse_article(raw_bytes, candidate.url, source.config)
        parsed_metadata = dict(_first_attr(parsed, "metadata", default={}) or {})
        parsed_categories = parsed_metadata.get("error_classifications", [])
        if isinstance(parsed_categories, str):
            parsed_categories = [parsed_categories]
        parsed_categories = [
            str(value).strip().lower().replace("-", "_")
            for value in (parsed_categories or [])
            if str(value).strip()
        ]
        parsed_details = parsed_metadata.get("error_details", [])
        if isinstance(parsed_details, str):
            parsed_details = [parsed_details]
        detail_text = "; ".join(str(value) for value in (parsed_details or []) if value)
        if "date_parse_error" in parsed_categories:
            raise DateParseError(detail_text or "article publication date could not be parsed")
        if "content_parse_error" in parsed_categories:
            raise ContentParseError(detail_text or "article content could not be extracted")
        parse_status = str(_first_attr(parsed, "parse_status", default="") or "").lower()
        if parse_status == "content_parse_error":
            raise ContentParseError(detail_text or "article content could not be extracted")
        if parse_status in {"failed", "error"}:
            if parse_status == "error":
                raise ContentParseError("article content parse failed")
            raise ContentParseError("article content could not be extracted")
        canonical = (
            _first_attr(parsed, "canonical_url", default=None)
            or _first_attr(candidate, "canonical_url", default=None)
            or candidate.url
        )
        url_policy = URLPolicy.from_zoo_source(zoo, source)
        if not url_policy.accepts(canonical, source_url=source.url):
            raise ValueError(f"article canonical URL outside official/allow policy: {canonical}")
        metadata = {**(candidate.metadata or {}), **parsed_metadata}
        language = (
            metadata.get("html_language")
            or metadata.get("structured_language")
            or source.language
            or zoo.language
        )
        return self._article_from_parsed(
            candidate=candidate,
            parsed=parsed,
            raw_bytes=raw_bytes,
            source=source,
            zoo=zoo,
            canonical=canonical,
            status=status,
            metadata=metadata,
            language=language,
        )

    def crawl(
        self,
        selection: str | Iterable[str] = "all",
        *,
        since_days: Optional[int] = None,
        max_candidates_per_source: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> CrawlResult:
        if since_days is not None and (
            isinstance(since_days, bool)
            or not isinstance(since_days, int)
            or since_days < 0
        ):
            raise ValueError("since_days must be a non-negative integer")
        candidate_limit, page_limit, effective_candidate_limit = self._effective_limits(
            max_candidates_per_source, max_pages
        )
        storage = self._ensure_storage()
        # Registry is written before any network request, including disabled
        # or health-check-only sources.
        self.registry.register_storage(storage)
        selected_zoos = self._zoos(selection)
        requested = [selection] if isinstance(selection, str) else list(selection)
        if requested and not (len(requested) == 1 and str(requested[0]).lower() == "all") and not selected_zoos:
            raise ValueError(f"unknown zoo selection: {sanitize_error(','.join(str(item) for item in requested))}")
        crawl_started = _now()
        cutoff = (
            crawl_started - timedelta(days=since_days)
            if since_days is not None
            else None
        )
        run = self._new_run(selection)
        result = CrawlResult(run=run)
        seen_articles: dict[str, Article] = {}
        pre_run_article_ids = {
            str(article.id) for article in (storage.list_articles() if hasattr(storage, "list_articles") else [])
            if article.id is not None
        }
        resolved_article_ids: set[str] = set()
        for zoo in selected_zoos:
            zoo_started = _now()
            try:
                self._crawl_zoo(
                    result=result,
                    run=run,
                    zoo=zoo,
                    zoo_started=zoo_started,
                    cutoff=cutoff,
                    seen_articles=seen_articles,
                    pre_run_article_ids=pre_run_article_ids,
                    resolved_article_ids=resolved_article_ids,
                    max_candidates_per_source=effective_candidate_limit,
                    max_pages=page_limit,
                )
            except Exception as exc:
                self._record_zoo_exception(
                    result, run, zoo, zoo_started, exc, phase="configuration"
                )
        processed_ids = [str(zoo.id or zoo.slug) for zoo in selected_zoos]
        enabled_ids = [
            str(zoo.id or zoo.slug)
            for zoo in self._unique(self.registry.zoos.values())
            if bool(zoo.enabled)
        ]
        candidates_processed = sum(
            int((_first_attr(item, "metadata", default={}) or {}).get("candidates_processed", 0) or 0)
            for item in result.zoo_results
        )
        candidates_limited = sum(
            int((_first_attr(item, "metadata", default={}) or {}).get("candidates_limited", 0) or 0)
            for item in result.zoo_results
        )
        zoo_failures = [item for item in result.zoo_results if str(_first_attr(item, "status", default="")) in {"partial", "failed"}]
        result.status = "completed_with_errors" if zoo_failures or result.error_count else "completed"
        run_classifications = result.error_classifications
        result.metadata = {
            "zoos": len(selected_zoos),
            "sources": len(result.stats),
            "processed": len(processed_ids),
            "enabled": len(enabled_ids),
            "processed_count": len(processed_ids),
            "enabled_count": len(enabled_ids),
            "selected": len(processed_ids),
            "selected_zoos": processed_ids,
            "processed_zoos": processed_ids,
            "enabled_zoos": enabled_ids,
            "since_filtered": result.since_filtered_count,
            "since_filtered_count": result.since_filtered_count,
            "since_days": since_days,
            "since_cutoff": cutoff.isoformat() if cutoff is not None else None,
            "max_candidates_per_source": candidate_limit,
            "max_pages": page_limit,
            "effective_candidate_limit": effective_candidate_limit,
            "bounded": effective_candidate_limit is not None or page_limit is not None,
            "candidates_processed": candidates_processed,
            "candidates_limited": candidates_limited,
            "error_classifications": run_classifications,
            "error_categories": run_classifications,
        }
        if zoo_failures:
            summaries: list[str] = []
            for item in zoo_failures:
                summary = _first_attr(item, "error_summary", default=None)
                if isinstance(summary, str):
                    summaries.append(summary)
                elif isinstance(summary, Iterable):
                    summaries.extend(str(value) for value in summary)
            result.error = "; ".join(sanitize_error(item) for item in summaries) or None
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

    def crawl_all(
        self,
        *,
        since_days: Optional[int] = None,
        max_candidates_per_source: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> CrawlResult:
        return self.crawl(
            "all",
            since_days=since_days,
            max_candidates_per_source=max_candidates_per_source,
            max_pages=max_pages,
        )

    def crawl_zoo(
        self,
        slug: str,
        *,
        since_days: Optional[int] = None,
        max_candidates_per_source: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> CrawlResult:
        return self.crawl(
            slug,
            since_days=since_days,
            max_candidates_per_source=max_candidates_per_source,
            max_pages=max_pages,
        )

    def _crawl_zoo(
        self,
        *,
        result: CrawlResult,
        run: Optional[CrawlRun],
        zoo: Zoo,
        zoo_started: datetime,
        cutoff: Optional[datetime],
        seen_articles: dict[str, Article],
        pre_run_article_ids: set[str],
        resolved_article_ids: set[str],
        max_candidates_per_source: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> None:
        """Process one zoo; the caller owns the failure boundary."""
        zoo_id = str(zoo.id or zoo.slug)
        try:
            sources = sorted(self.registry.sources_for_zoo(zoo), key=source_priority)
            source_url: Optional[str] = (
                RequestPolicy.safe_target(sources[0].url) if sources else None
            )
        except Exception as exc:
            self._record_zoo_exception(result, run, zoo, zoo_started, exc)
            return
        zoo_http_status: Optional[int] = None
        zoo_discovered = 0
        zoo_parsed = 0
        zoo_inserted = 0
        zoo_updated = 0
        zoo_failed = 0
        zoo_duplicate_filtered = 0
        zoo_since_filtered = 0
        zoo_candidates_limited = 0
        zoo_candidates_processed = 0
        source_failures = 0
        successful_sources = 0
        successful_articles = 0
        zoo_storage_failed = False
        zoo_errors: list[tuple[str, str, str]] = []

        # An enabled zoo with no enabled sources is still a processed zoo.
        # Persisting this explicit result avoids silently dropping coverage
        # from dashboards and scheduler summaries.  An authoritative Phase 0
        # row may have only disabled source records: those records are evidence
        # and must never be fetched, while an ordinary empty source set remains
        # a configuration failure.
        if not sources:
            finished = _now()
            configured_status = self._declared_source_status(
                _first_attr(zoo, "source_status", default=None)
            )
            try:
                evidence_sources_configured = sorted(
                    self.registry.sources_for_zoo(zoo, enabled_only=False),
                    key=source_priority,
                )
            except Exception:
                # Source selection itself succeeded above.  A legacy registry
                # that cannot expose disabled entries should not turn this
                # evidence-only path into a network attempt.
                evidence_sources_configured = []

            evidence_sources: list[dict[str, Any]] = []
            evidence_urls: list[str] = []
            evidence_categories: list[str] = []
            evidence_reasons: dict[str, str] = {}
            for evidence_source in evidence_sources_configured:
                raw_url = str(getattr(evidence_source, "url", "") or "")
                safe_url = RequestPolicy.safe_target(raw_url) if raw_url else None
                source_status = self._declared_source_status(
                    _first_attr(
                        evidence_source,
                        "source_status",
                        "status",
                        default=None,
                    )
                )
                source_id = str(getattr(evidence_source, "id", "") or "")
                config = _first_attr(evidence_source, "config", default={})
                reason = (
                    str(config.get("source_status_reason"))
                    if isinstance(config, Mapping) and config.get("source_status_reason")
                    else ""
                )
                evidence = {
                    "id": source_id or None,
                    "status": source_status or None,
                    "enabled": bool(getattr(evidence_source, "enabled", False)),
                    "url": safe_url,
                }
                if reason:
                    safe_reason = sanitize_error(reason)
                    evidence["reason"] = safe_reason
                    if source_id:
                        evidence_reasons[source_id] = safe_reason
                evidence_sources.append(evidence)
                if safe_url and safe_url not in evidence_urls:
                    evidence_urls.append(safe_url)
                if source_status and source_status not in evidence_categories:
                    evidence_categories.append(source_status)

            authoritative = self._has_authoritative_phase0_provenance(zoo)
            accepted_status = (
                authoritative
                and configured_status in _PHASE0_EMPTY_SOURCE_LIMITATIONS
            )
            if accepted_status:
                zoo_status = "success_no_items"
                source_status = configured_status
                failed = 0
                categories = list(
                    dict.fromkeys(
                        [
                            configured_status,
                            *evidence_categories,
                        ]
                    )
                )
                error_summary = [
                    f"{configured_status}: no enabled sources configured"
                ]
                error_metadata = {
                    "error_sources": evidence_urls,
                    "error_categories": categories,
                    "configured_source_status": configured_status,
                    "evidence_sources": evidence_sources,
                    "evidence_urls": evidence_urls,
                    "source_status_reasons": evidence_reasons,
                    "disabled_source_count": len(evidence_sources),
                    "since_filtered": 0,
                    "since_filtered_count": 0,
                    "candidates_processed": 0,
                    "candidates_limited": 0,
                    "max_candidates_per_source": max_candidates_per_source,
                    "max_pages": max_pages,
                }
                source_url = evidence_urls[0] if evidence_urls else None
            else:
                zoo_status = "failed"
                source_status = "configuration_error"
                failed = 1
                error_summary = [
                    "configuration_error: no enabled sources configured"
                ]
                error_metadata = {
                    "error_sources": [],
                    "error_categories": ["configuration_error"],
                    "configured_source_status": configured_status or None,
                    "evidence_sources": evidence_sources,
                    "evidence_urls": evidence_urls,
                    "source_status_reasons": evidence_reasons,
                    "disabled_source_count": len(evidence_sources),
                    "since_filtered": 0,
                    "since_filtered_count": 0,
                    "candidates_processed": 0,
                    "candidates_limited": 0,
                    "max_candidates_per_source": max_candidates_per_source,
                    "max_pages": max_pages,
                }
                source_url = None
            values = self._zoo_result_values(
                run=run, zoo=zoo, status=zoo_status, source_status=source_status,
                discovered=0, parsed=0, inserted=0, updated=0, failed=failed,
                duplicate_filtered=0, duration_ms=max(0, int((finished - zoo_started).total_seconds() * 1000)),
                source_url=source_url, http_status=None,
                error_summary=error_summary,
                started_at=zoo_started, finished_at=finished,
                metadata=error_metadata,
            )
            zoo_result = self._build_zoo_result(values)
            try:
                persisted_zoo_result = self._record_zoo_result(zoo_result, run)
            except Exception as exc:
                category, message = self._error_detail(exc, phase="storage")
                persisted_zoo_result = self._mark_zoo_result_storage_failure(
                    zoo_result, category, message
                )
            result.zoo_results.append(persisted_zoo_result)
            return

        for source in sources:
            source_storage_failed = False
            source_id = self._source_id(source)
            stat_started = _now()
            stat = CrawlRunStat(
                crawl_run_id=getattr(run, "id", None), zoo_id=zoo_id, source_id=source_id,
                started_at=stat_started, status="running",
                metadata={"since_filtered_count": 0},
            )
            if max_candidates_per_source is not None:
                stat.metadata["max_candidates_per_source"] = max_candidates_per_source
            if max_pages is not None:
                stat.metadata["max_pages"] = max_pages
            errors: list[str] = []
            classifications: list[str] = []
            root_http_status: Optional[int] = None
            source_since_filtered = 0
            try:
                if _source_requires_javascript(source):
                    raise JavascriptRequiredError(
                        f"source {source_id} is explicitly configured to require JavaScript rendering"
                    )
                discovery_source = self._bounded_discovery_source(source, max_pages)
                discovered_candidates = self.discovery.discover(discovery_source, zoo=zoo)
                stat.discovered_count = len(discovered_candidates)
                stat.duplicate_candidate_count = int(
                    getattr(discovered_candidates, "duplicate_candidate_count", 0)
                )
                root_http_status = getattr(discovered_candidates, "root_http_status", None)
                zoo_discovered += stat.discovered_count
                zoo_duplicate_filtered += stat.duplicate_candidate_count
                if zoo_http_status is None:
                    zoo_http_status = root_http_status
                candidates = list(discovered_candidates)
                limited_count = 0
                if max_candidates_per_source is not None and len(candidates) > max_candidates_per_source:
                    limited_count = len(candidates) - max_candidates_per_source
                    candidates = candidates[:max_candidates_per_source]
                stat.metadata["candidates_processed"] = len(candidates)
                stat.metadata["candidates_limited"] = limited_count
                zoo_candidates_processed += len(candidates)
                zoo_candidates_limited += limited_count
                successful_sources += 1
                status_error = self._persist_status(
                    source,
                    status="discovered",
                    success=False,
                    http_status=root_http_status,
                )
                if status_error:
                    source_storage_failed = True
                    zoo_storage_failed = True
                    zoo_failed += 1
                    classifications.append(status_error[0])
                    errors.append(status_error[1])
                    zoo_errors.append((status_error[0], status_error[1], RequestPolicy.safe_target(source.url)))
            except Exception as exc:
                category, message = self._error_detail(exc, phase="discovery")
                safe_source = RequestPolicy.safe_target(source.url)
                errors.append(message)
                classifications.append(category)
                zoo_errors.append((category, message, safe_source))
                stat.error_count = 1
                stat.errors = errors
                stat.error = message
                stat.status = "error"
                stat.metadata["error_classification"] = category
                stat.metadata["error_classifications"] = [category]
                _set_optional_attributes(stat, {"error_category": category})
                source_failures += 1
                zoo_failed += 1
                error_status = _status_code(exc)
                if error_status is not None and (zoo_http_status is None or error_status >= 400):
                    zoo_http_status = error_status
                status_error = self._persist_status(
                    source,
                    status="error",
                    success=False,
                    error=message,
                    http_status=error_status,
                )
                if status_error:
                    source_storage_failed = True
                    zoo_storage_failed = True
                    zoo_failed += 1
                    zoo_errors.append((status_error[0], status_error[1], safe_source))
                self.logger.error("crawl source failed zoo=%s source=%s error=%s", zoo.slug, safe_source, message)
                stat.finished_at = _now()
                stat.duration_ms = max(0, int((stat.finished_at - stat_started).total_seconds() * 1000))
                persisted_stat, stat_error = self._record_stat_checked(stat)
                result.stats.append(persisted_stat)
                if stat_error:
                    source_storage_failed = True
                    zoo_storage_failed = True
                    zoo_failed += 1
                    zoo_errors.append((stat_error[0], stat_error[1], safe_source))
                continue

            recheck = self._recheck_enabled(source, self.recent_recheck)
            for candidate in candidates:
                identity_keys = self._candidate_identity_keys(candidate, source, zoo)
                key = identity_keys[0] if identity_keys else ""
                if not key:
                    continue
                if self._is_before_cutoff(
                    _first_attr(candidate, "published_at", default=None), cutoff
                ):
                    source_since_filtered += 1
                    zoo_since_filtered += 1
                    continue
                existing = next(
                    (seen_articles[identity_key] for identity_key in identity_keys if identity_key in seen_articles),
                    None,
                )
                if existing is None:
                    for identity_key in identity_keys:
                        existing = self._existing_article(identity_key)
                        if existing is not None:
                            break
                if existing is not None and not recheck:
                    try:
                        self._record_existing_discovery(existing, source, candidate)
                    except Exception as exc:
                        category, message = self._error_detail(exc, phase="storage")
                        safe_source = RequestPolicy.safe_target(source.url)
                        errors.append(message)
                        classifications.append(category)
                        zoo_errors.append((category, message, safe_source))
                        stat.error_count += 1
                        zoo_failed += 1
                        source_storage_failed = True
                        zoo_storage_failed = True
                        continue
                    for identity_key in identity_keys:
                        seen_articles[identity_key] = existing
                    identity = str(getattr(existing, "id", None) or key)
                    if identity in resolved_article_ids:
                        stat.duplicate_candidate_count += 1
                        zoo_duplicate_filtered += 1
                    elif identity in pre_run_article_ids:
                        stat.already_known_count += 1
                    else:
                        stat.duplicate_candidate_count += 1
                        zoo_duplicate_filtered += 1
                    resolved_article_ids.add(identity)
                    continue
                try:
                    article = self._fetch_article(candidate, source, zoo)
                    stat.fetched_count += 1
                    zoo_parsed += 1
                    successful_articles += 1
                except Exception as exc:
                    category, message = self._error_detail(exc, phase="article")
                    safe_candidate = RequestPolicy.safe_target(candidate.url)
                    detail = sanitize_error(f"{safe_candidate}: {message}")
                    errors.append(detail)
                    classifications.append(category)
                    zoo_errors.append((category, detail, RequestPolicy.safe_target(source.url)))
                    stat.error_count += 1
                    zoo_failed += 1
                    status_code = _status_code(exc)
                    if status_code is not None and (zoo_http_status is None or status_code >= 400):
                        zoo_http_status = status_code
                    self.logger.warning(
                        "crawl article failed zoo=%s source=%s url=%s error=%s",
                        zoo.slug, RequestPolicy.safe_target(source.url), safe_candidate, message,
                    )
                    continue

                if self._is_before_cutoff(
                    _first_attr(article, "published_at", default=None), cutoff
                ):
                    source_since_filtered += 1
                    zoo_since_filtered += 1
                    continue

                try:
                    outcome = self._upsert_article(article, source, candidate)
                    stored = _outcome_value(outcome, "article", None)
                    created = bool(_outcome_value(outcome, "created", False))
                    if stored is None:
                        stored, created = outcome
                except Exception as exc:
                    category, message = self._error_detail(exc, phase="storage")
                    safe_source = RequestPolicy.safe_target(source.url)
                    detail = sanitize_error(f"{safe_source}: {message}")
                    errors.append(detail)
                    classifications.append(category)
                    zoo_errors.append((category, detail, safe_source))
                    stat.error_count += 1
                    zoo_failed += 1
                    source_storage_failed = True
                    zoo_storage_failed = True
                    self.logger.warning(
                        "crawl article storage failed zoo=%s source=%s error=%s",
                        zoo.slug, safe_source, message,
                    )
                    continue

                updated = bool(getattr(outcome, "updated", False))
                if created:
                    stat.stored_count += 1
                    zoo_inserted += 1
                elif updated:
                    zoo_updated += 1
                identity = str(getattr(stored, "id", None) or normalize_url(getattr(stored, "canonical_url", None) or key) or key)
                # An outcome explicitly marked ``updated`` is neither a
                # pre-existing skip nor a duplicate candidate.
                if not created and not updated and identity in resolved_article_ids:
                    stat.duplicate_candidate_count += 1
                    zoo_duplicate_filtered += 1
                elif not created and not updated and identity in pre_run_article_ids:
                    stat.already_known_count += 1
                elif not created and not updated:
                    stat.duplicate_candidate_count += 1
                    zoo_duplicate_filtered += 1
                resolved_article_ids.add(identity)
                for identity_key in identity_keys:
                    seen_articles[identity_key] = stored
                # The article response may expose a canonical URL that
                # differs from both the discovered and feed canonical
                # URLs.  Keep that identity available for later
                # same-batch candidates as well.
                stored_key = normalize_url(getattr(stored, "canonical_url", None) or key)
                if stored_key:
                    seen_articles[stored_key] = stored

            stat.metadata["since_filtered_count"] = source_since_filtered
            stat.errors = errors
            stat.status = "error" if source_storage_failed else ("completed" if not errors else "partial")
            stat.error = "; ".join(errors) if errors else None
            if classifications:
                stat.metadata["error_classifications"] = list(dict.fromkeys(classifications))
                stat.metadata["error_classification"] = classifications[0]
                _set_optional_attributes(stat, {"error_category": classifications[0]})
            stat.finished_at = _now()
            stat.duration_ms = max(0, int((stat.finished_at - stat_started).total_seconds() * 1000))
            # Discovery succeeded even when an individual article page
            # failed.  Keep ``last_success`` truthful at source level;
            # article errors remain visible in the per-source run stat.
            status_error = self._persist_status(
                source,
                status=stat.status,
                success=not source_storage_failed,
                error=stat.error,
                http_status=root_http_status,
            )
            if status_error:
                source_storage_failed = True
                zoo_storage_failed = True
                zoo_failed += 1
                errors.append(status_error[1])
                classifications.append(status_error[0])
                stat.error_count += 1
                stat.errors = errors
                stat.error = "; ".join(errors)
                zoo_errors.append((status_error[0], status_error[1], RequestPolicy.safe_target(source.url)))
            self.logger.info(
                "crawl source zoo=%s source=%s status=%s discovered=%d fetched=%d stored=%d errors=%d",
                zoo.slug, RequestPolicy.safe_target(source.url), stat.status, stat.discovered_count, stat.fetched_count,
                stat.stored_count, stat.error_count,
            )
            persisted_stat, stat_error = self._record_stat_checked(stat)
            result.stats.append(persisted_stat)
            if stat_error:
                source_storage_failed = True
                zoo_storage_failed = True
                zoo_failed += 1
                safe_source = RequestPolicy.safe_target(source.url)
                zoo_errors.append((stat_error[0], stat_error[1], safe_source))
                self._persist_status(
                    source,
                    status="error",
                    success=False,
                    error=stat_error[1],
                    http_status=root_http_status,
                )

        finished = _now()
        if zoo_errors:
            categories = [item[0] for item in zoo_errors]
            source_status = categories[0]
            if source_failures and successful_sources == 0:
                zoo_status = "failed"
            elif successful_articles and zoo_failed:
                zoo_status = "partial"
            elif source_failures and successful_sources:
                zoo_status = "partial"
            elif zoo_storage_failed and successful_sources:
                zoo_status = "partial"
            elif zoo_failed:
                zoo_status = "failed"
            else:
                zoo_status = "partial"
            error_summary = [f"{category}: {message}" for category, message, _ in zoo_errors]
            error_sources = list(dict.fromkeys(item[2] for item in zoo_errors if item[2]))
            error_metadata = {
                "error_sources": error_sources,
                "error_categories": list(dict.fromkeys(categories)),
                "since_filtered": zoo_since_filtered,
                "since_filtered_count": zoo_since_filtered,
                "candidates_processed": zoo_candidates_processed,
                "candidates_limited": zoo_candidates_limited,
                "max_candidates_per_source": max_candidates_per_source,
                "max_pages": max_pages,
            }
            source_url = error_sources[0] if error_sources else source_url
        elif zoo_discovered == 0:
            source_status = "success_no_items"
            zoo_status = "success_no_items"
            error_summary = []
            error_metadata = {
                "since_filtered": zoo_since_filtered,
                "since_filtered_count": zoo_since_filtered,
                "candidates_processed": zoo_candidates_processed,
                "candidates_limited": zoo_candidates_limited,
                "max_candidates_per_source": max_candidates_per_source,
                "max_pages": max_pages,
            }
        else:
            source_status = "success"
            zoo_status = "success"
            error_summary = []
            error_metadata = {
                "since_filtered": zoo_since_filtered,
                "since_filtered_count": zoo_since_filtered,
                "candidates_processed": zoo_candidates_processed,
                "candidates_limited": zoo_candidates_limited,
                "max_candidates_per_source": max_candidates_per_source,
                "max_pages": max_pages,
            }
        values = self._zoo_result_values(
            run=run, zoo=zoo, status=zoo_status, source_status=source_status,
            discovered=zoo_discovered, parsed=zoo_parsed, inserted=zoo_inserted,
            updated=zoo_updated, failed=zoo_failed,
            duplicate_filtered=zoo_duplicate_filtered,
            duration_ms=max(0, int((finished - zoo_started).total_seconds() * 1000)),
            source_url=source_url, http_status=zoo_http_status,
            error_summary=error_summary, started_at=zoo_started, finished_at=finished,
            metadata=error_metadata,
        )
        zoo_result = self._build_zoo_result(values)
        try:
            persisted_zoo_result = self._record_zoo_result(zoo_result, run)
        except Exception as exc:
            category, message = self._error_detail(exc, phase="storage")
            persisted_zoo_result = self._mark_zoo_result_storage_failure(
                zoo_result, category, message
            )
            for source in sources:
                self._persist_status(
                    source,
                    status="error",
                    success=False,
                    error=message,
                    http_status=zoo_http_status,
                )
        result.zoo_results.append(persisted_zoo_result)

    run = crawl



def crawl(
    config: Any,
    *,
    selection: str = "all",
    storage: Any = None,
    fetcher: Any = None,
    since_days: Optional[int] = None,
    **kwargs: Any,
) -> CrawlResult:
    """Functional entry point for scripts and integrations."""

    return Crawler(config, storage=storage, fetcher=fetcher, **kwargs).crawl(
        selection, since_days=since_days
    )


# Friendly aliases for integrations that call this component an orchestrator.
CrawlerOrchestrator = Crawler
run_crawl = crawl


__all__ = [
    "ALL_STATUSES", "ERROR_STATUSES", "Crawler", "CrawlerOrchestrator",
    "CrawlResult", "crawl", "run_crawl",
]
