"""Application/query service for the read-only crawler console.

The service deliberately contains no SQL.  It turns repository projections
into stable JSON-ready response DTOs, performs small cross-table aggregations
through repository methods, and supplies consistent pagination metadata.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Union

from .console_repository import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ConsoleRepository,
    _json_ready,
    _page_values,
    parse_metadata,
)


_STATUS_LABELS = {
    "running": "Running",
    "stale": "Stale",
    "unknown": "Unknown",
    "success": "Success",
    "warning": "Warning",
    "failed": "Failed",
    "budget_hit": "Budget Hit",
}
_STATUS_ALIASES = {
    "finished": "success",
    "completed": "success",
    "complete": "success",
    "success": "success",
    "succeeded": "success",
    "ok": "success",
    "done": "success",
    "completed_with_errors": "warning",
    "partial": "warning",
    "warning": "warning",
    "failed": "failed",
    "failure": "failed",
    "error": "failed",
    "aborted": "failed",
    "cancelled": "failed",
    "canceled": "failed",
    "stopped": "failed",
    "running": "running",
    "active": "running",
    "in_progress": "running",
    "stale": "stale",
    "unknown": "unknown",
    "budget_hit": "budget_hit",
}
_BUDGET_REASONS = {
    "article_limit", "page_limit", "runtime_limit", "url_discovery_limit",
    "candidate_limit", "batch_limit", "max_candidates_per_source", "max_pages",
    "max_sitemaps", "archive_page_limit", "sitemap_page_limit", "no_new_urls",
}


def _status_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    token = str(value).strip().casefold().replace("-", "_").replace(" ", "_")
    return _STATUS_ALIASES.get(token)


def _normalise_status_dto(value: Any) -> Any:
    """Add the stable status contract without changing repository data."""

    if not isinstance(value, Mapping):
        return value
    # Source and event records have their own operational status vocabulary;
    # do not reinterpret those statuses as run classifications.
    if "source_id" in value or "event_type" in value:
        return dict(value)
    status_fields = {
        "status", "run_status", "raw_status", "classified_status", "status_key",
        "display_status", "terminal_status", "stale", "status_observable",
    }
    if not any(field in value for field in status_fields):
        return dict(value)
    result = dict(value)
    raw_status = result.get("raw_status")
    if raw_status in (None, ""):
        raw_status = result.get("run_status", result.get("status", "unknown"))
    raw_status = str(raw_status) if raw_status not in (None, "") else "unknown"

    classified = (
        _status_key(result.get("classified_status"))
        or _status_key(result.get("status_key"))
        or _status_key(result.get("display_status"))
        or _status_key(result.get("status"))
        or _status_key(result.get("run_status"))
    )
    reason = result.get("stop_reason")
    reason_token = str(reason).strip().casefold().replace("-", "_").replace(" ", "_") if reason else ""
    if reason_token in _BUDGET_REASONS or reason_token in {"budget", "budget_hit", "crawl_budget_hit"}:
        classified = "budget_hit"
    if not classified:
        classified = "unknown"
    # A legacy running row without liveness evidence is not an active run.
    if classified == "running" and (
        result.get("stale") is True
        or result.get("status_observable") is False
        or result.get("active") is False
        or result.get("is_active") is False
    ):
        classified = "stale" if result.get("stale") is True else "unknown"

    terminal = result.get("terminal_status")
    if terminal is None and classified in {"success", "warning", "failed", "budget_hit"}:
        terminal = raw_status
    result.update({
        "status": classified,
        "classified_status": classified,
        "display_status": classified if result.get("display_status") in (None, "") else result["display_status"],
        "status_key": classified,
        "status_label": _STATUS_LABELS[classified] if result.get("status_label") in (None, "") else result["status_label"],
        "raw_status": raw_status,
        "run_status": result.get("run_status", raw_status),
        "terminal_status": terminal,
    })
    if classified != "running" or result.get("stale") is True or result.get("status_observable") is False:
        if "active" in result:
            result["active"] = False
        if "is_active" in result:
            result["is_active"] = False
        if "active_run" in result:
            result["active_run"] = None
    if "poll_hint" in result and isinstance(result["poll_hint"], Mapping):
        polling = classified == "running" and result.get("active", True) is not False
        result["poll_hint"] = {
            **result["poll_hint"],
            "enabled": polling,
            "interval_seconds": 2.5 if polling else None,
        }
        if "poll_after_seconds" in result:
            result["poll_after_seconds"] = 2.5 if polling else None
    return result


def _normalise_status_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _normalise_status_dto(value)
    if isinstance(value, list):
        return [_normalise_status_tree(item) for item in value]
    return value


def _page(items: List[Dict[str, Any]], total: int, limit: Any, offset: Any) -> Dict[str, Any]:
    safe_limit, safe_offset = _page_values(limit, offset)
    page_items = [] if safe_limit == 0 else items[safe_offset : safe_offset + safe_limit]
    has_more = safe_offset + len(page_items) < total
    next_offset = safe_offset + len(page_items) if has_more else None
    return {
        "items": [_json_dto(item) for item in page_items],
        "pagination": {
            "limit": safe_limit,
            "offset": safe_offset,
            "total": max(0, int(total)),
            "has_more": has_more,
            "next_offset": next_offset,
        },
    }


def _json_dto(value: Any) -> Any:
    return _normalise_status_tree(_json_ready(value))


def _named_page(page: Dict[str, Any], name: str) -> Dict[str, Any]:
    """Add the resource-specific collection key while retaining ``items``."""

    result = dict(page)
    result[name] = list(result.get("items") or [])
    pagination = result.get("pagination")
    if isinstance(pagination, Mapping):
        result.setdefault("total", pagination.get("total", 0))
        result.setdefault("count", len(result[name]))
    return result


class ConsoleService:
    """Read-only service facade consumed by a future API or UI."""

    def __init__(
        self,
        repository: Optional[Union[ConsoleRepository, Any]] = None,
        *,
        database: Any = ":memory:",
    ) -> None:
        if repository is None:
            self.repository = ConsoleRepository(database)
        elif isinstance(repository, ConsoleRepository):
            self.repository = repository
        else:
            self.repository = ConsoleRepository(repository)

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> "ConsoleService":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @staticmethod
    def json_ready(value: Any) -> Any:
        """Return a JSON-compatible copy suitable for an API response."""

        return _json_dto(value)

    @staticmethod
    def parse_metadata(value: Any) -> Dict[str, Any]:
        return parse_metadata(value)

    safe_metadata = parse_metadata

    def get_active_run(
        self, *, now: Any = None, stale_after_seconds: Any = 300
    ) -> Optional[Dict[str, Any]]:
        return _json_dto(
            self.repository.get_active_run(
                now=now, stale_after_seconds=stale_after_seconds
            )
        )

    current_run = get_active_run

    def get_current_status(
        self, *, now: Any = None, stale_after_seconds: Any = 300
    ) -> Dict[str, Any]:
        current = self.repository.get_current_status(
            now=now, stale_after_seconds=stale_after_seconds
        )
        run = current.get("run") if isinstance(current, Mapping) else None
        if isinstance(run, Mapping) and run.get("id"):
            summary = self.repository.get_run_summary(str(run["id"]))
            if summary is not None:
                run = {**summary, **run}
                current = {**current, "run": run, "current_run": run}
                if current.get("status") == "running":
                    current["active_run"] = run
                else:
                    current["active_run"] = None
        # Keep liveness and polling fields at the stable status boundary even
        # when a legacy repository only returns a partially populated run.
        run = current.get("current_run") if isinstance(current, Mapping) else None
        if isinstance(run, Mapping):
            current = {
                **current,
                "heartbeat_at": run.get("heartbeat_at"),
                "progress_at": run.get("progress_at"),
                "current_phase": run.get("current_phase"),
                "last_updated": run.get("last_updated")
                or run.get("heartbeat_at")
                or run.get("progress_at"),
            }
        is_active = current.get("status") == "running"
        current.setdefault(
            "poll_hint",
            {
                "enabled": is_active,
                "interval_seconds": 2.5 if is_active else None,
                "after_id": None,
            },
        )
        current.setdefault("poll_after_seconds", 2.5 if is_active else None)
        return _json_dto(current)

    get_status = get_current_status
    current_status = get_current_status
    get_crawler_status = get_current_status

    def list_runs(
        self,
        *,
        status: Optional[str] = None,
        limit: Any = DEFAULT_PAGE_SIZE,
        offset: Any = 0,
    ) -> Dict[str, Any]:
        rows = self.repository.list_runs(status=status)
        enriched: List[Dict[str, Any]] = []
        for row in rows:
            summary = self.repository.get_run_summary(str(row.get("id"))) if row.get("id") else None
            if summary is not None:
                # The list must retain the classified status from the run
                # projection, even when the summary contributes counters.
                enriched.append({**summary, **row})
            else:
                enriched.append(dict(row))
        rows = enriched
        return _named_page(_page(rows, len(rows), limit, offset), "runs")

    get_runs = list_runs
    get_run_list = list_runs
    list_crawl_runs = list_runs

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return _json_dto(self.repository.get_run(run_id))

    def get_run_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        summary = self.repository.get_run_summary(run_id)
        if summary is None:
            return None
        return _json_dto(
            {
                **summary,
                "zoo_results": self.repository.list_run_zoo_results(run_id),
                "source_results": self.repository.list_run_source_results(run_id),
            }
        )

    get_run_detail = get_run_summary
    get_crawl_run = get_run_summary

    def get_run_zoo_results(
        self, run_id: str, *, limit: Any = None, offset: Any = 0
    ) -> Any:
        rows = _json_dto(self.repository.list_run_zoo_results(run_id))
        if limit is None:
            return rows
        return _named_page(_page(rows, len(rows), limit, offset), "zoos")

    list_run_zoo_results = get_run_zoo_results
    list_run_zoos = get_run_zoo_results

    def get_run_source_results(
        self,
        run_id: str,
        *,
        zoo_id: Optional[str] = None,
        source_id: Optional[str] = None,
        limit: Any = None,
        offset: Any = 0,
    ) -> Any:
        rows = _json_dto(
            self.repository.list_run_source_results(
                run_id, zoo_id=zoo_id, source_id=source_id
            )
        )
        if limit is None:
            return rows
        return _named_page(_page(rows, len(rows), limit, offset), "sources")

    list_run_source_results = get_run_source_results
    get_run_zoo_source_results = get_run_source_results

    def get_run_results(self, run_id: str) -> Optional[Dict[str, Any]]:
        summary = self.repository.get_run_summary(run_id)
        if summary is None:
            return None
        return _json_dto(
            {
                "run": summary,
                "summary": summary,
                "zoos": self.repository.list_run_zoo_results(run_id),
                "sources": self.repository.list_run_source_results(run_id),
            }
        )

    def get_run_articles(
        self,
        run_id: str,
        *,
        limit: Any = DEFAULT_PAGE_SIZE,
        offset: Any = 0,
        stored_only: bool = False,
        search: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> Dict[str, Any]:
        observability = self.repository.get_run_articles_observability(run_id)
        total = self.repository.count_run_articles(
            run_id, stored_only=stored_only, search=search, outcome=outcome
        )
        rows = self.repository.list_run_articles(
            run_id,
            limit=None,
            offset=0,
            stored_only=stored_only,
            search=search,
            outcome=outcome,
        )
        page = _page(rows, total, limit, offset)
        page["observability"] = observability
        return _json_dto(_named_page(page, "articles"))

    list_run_articles = get_run_articles

    def get_run_events(
        self,
        run_id: str,
        *,
        level: Optional[str] = None,
        zoo: Optional[str] = None,
        zoo_id: Optional[str] = None,
        source: Optional[str] = None,
        source_id: Optional[str] = None,
        component: Optional[str] = None,
        event_type: Optional[str] = None,
        text: Optional[str] = None,
        search: Optional[str] = None,
        after_id: Any = None,
        limit: Any = DEFAULT_PAGE_SIZE,
        offset: Any = 0,
    ) -> Dict[str, Any]:
        rows, total = self.repository.query_run_events(
            run_id,
            level=level,
            zoo=zoo,
            zoo_id=zoo_id,
            source=source,
            source_id=source_id,
            component=component,
            event_type=event_type,
            text=text if text not in (None, "") else search,
            after_id=after_id,
            limit=None,
            offset=0,
        )
        page = _page(rows, total, limit, offset)
        page["filters"] = {
            "level": level,
            "zoo": zoo_id if zoo_id not in (None, "") else zoo,
            "source": source_id if source_id not in (None, "") else source,
            "component": component,
            "event_type": event_type,
            "text": text,
            "after_id": after_id,
        }
        page = _named_page(page, "events")
        if page["events"]:
            last_event = page["events"][-1]
            if isinstance(last_event, Mapping):
                page["next_after_id"] = last_event.get("id", last_event.get("event_id"))
        return _json_dto(page)

    list_run_events = get_run_events

    def get_zoos(
        self,
        *,
        enabled_only: bool = False,
        limit: Any = None,
        offset: Any = 0,
    ) -> Any:
        rows = _json_dto(self.repository.list_zoos(enabled_only=enabled_only))
        if limit is None:
            return rows
        return _named_page(_page(rows, len(rows), limit, offset), "zoos")

    list_zoos = get_zoos
    get_zoo_list = get_zoos

    def get_zoo(self, zoo_id: str) -> Optional[Dict[str, Any]]:
        return _json_dto(self.repository.get_zoo_detail(zoo_id))

    get_zoo_detail = get_zoo

    def get_zoo_history(
        self,
        zoo_id: str,
        *,
        limit: Any = DEFAULT_PAGE_SIZE,
        offset: Any = 0,
    ) -> Dict[str, Any]:
        total = self.repository.count_zoo_history(zoo_id)
        rows = self.repository.list_zoo_history(zoo_id, limit=None, offset=0)
        return _named_page(_page(rows, total, limit, offset), "history")

    list_zoo_history = get_zoo_history
    list_zoo_crawl_history = get_zoo_history


__all__ = [
    "ConsoleService",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
]
