"""Read-only query repository for the crawler console.

The crawler console is intentionally a consumer of the persisted crawler
tables.  It does not share the storage write API and it never creates or
migrates a database.  The repository owns the small amount of schema
discovery needed by the read-only dashboard: old databases can omit optional
tables/columns and still produce useful, JSON-safe projections.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import quote


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500
_JSON_MAX_DEPTH = 32
STATUS_LABELS = {
    "running": "Running",
    "stale": "Stale",
    "unknown": "Unknown",
    "success": "Success",
    "warning": "Warning",
    "failed": "Failed",
    "budget_hit": "Budget Hit",
}
_CLASSIFIED_STATUSES = set(STATUS_LABELS)
_LEGACY_TERMINAL_FILTERS = {"finished", "terminal"}
_RUNNING_STATUSES = {
    "running",
    "active",
    "in_progress",
    "in-progress",
    "started",
    "pending",
}
_TERMINAL_STATUSES = {
    "completed",
    "complete",
    "finished",
    "success",
    "succeeded",
    "success_no_items",
    "ok",
    "done",
    "failed",
    "failure",
    "error",
    "aborted",
    "cancelled",
    "canceled",
    "stopped",
    "completed_with_errors",
    "partial",
    "warning",
    "budget_hit",
}
_SUCCESS_STATUSES = {
    "completed",
    "complete",
    "finished",
    "success",
    "succeeded",
    "success_no_items",
    "ok",
    "done",
}
_WARNING_STATUSES = {"completed_with_errors", "partial", "warning"}
_FAILED_STATUSES = {
    "failed",
    "failure",
    "error",
    "aborted",
    "cancelled",
    "canceled",
    "stopped",
}
_BUDGET_STOP_REASONS = {
    "article_limit",
    "page_limit",
    "runtime_limit",
    "url_discovery_limit",
    "candidate_limit",
    "batch_limit",
    "max_candidates_per_source",
    "max_pages",
    "max_sitemaps",
    "archive_page_limit",
    "sitemap_page_limit",
}
_FAILURE_STOP_REASONS = {
    "retry_exhausted",
    "robots_blocked",
    "http_error",
    "parser_error",
    "content_parse_error",
    "unknown_error",
}
_SENSITIVE_METADATA_KEYS = {
    "raw_html",
    "raw_html_json",
    "raw_html_text",
    "rawhtml",
    "rawhtmljson",
    "rawhtmltext",
}

_TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "zoos": ("zoos",),
    "sources": ("sources",),
    "articles": ("articles",),
    "discoveries": ("article_discoveries", "discoveries", "article_sources"),
    "runs": ("crawl_runs", "runs"),
    "stats": ("crawl_run_stats", "run_stats", "crawl_stats"),
    "zoo_results": ("crawl_zoo_results", "zoo_run_results", "crawl_results"),
    "events": ("crawl_run_events", "run_events", "crawl_events"),
    "run_articles": ("crawl_run_articles", "run_articles", "crawl_articles"),
    "leases": ("crawler_leases", "crawl_leases", "leases"),
    "article_identities": ("article_zoo_identities", "article_identities"),
}


class ConsoleRepositoryError(RuntimeError):
    """A database infrastructure failure while serving a read request."""


def _quote_identifier(value: str) -> str:
    """Quote an identifier selected from a fixed internal allow-list."""

    return '"' + str(value).replace('"', '""') + '"'


def _row_dict(row: Any, description: Sequence[Any]) -> Dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {str(key): row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return {str(key): value for key, value in row.items()}
    return {str(item[0]): value for item, value in zip(description, row)}


def _json_ready(value: Any, depth: int = 0) -> Any:
    """Convert arbitrary persisted values to a bounded JSON-compatible tree."""

    if depth > _JSON_MAX_DEPTH:
        return None
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.casefold() in _SENSITIVE_METADATA_KEYS:
                continue
            result[key_text] = _json_ready(item, depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(item, depth + 1) for item in value]
    return str(value)


def parse_metadata(value: Any) -> Dict[str, Any]:
    """Parse a metadata object without allowing malformed rows to escape.

    Metadata in legacy databases is not guaranteed to be JSON, or even to be
    an object.  Console DTOs use an object consistently; malformed, scalar,
    deeply nested, and otherwise unusable values become ``{}``.
    """

    if isinstance(value, Mapping):
        parsed: Any = value
    elif isinstance(value, (bytes, bytearray)):
        try:
            parsed = json.loads(bytes(value).decode("utf-8"))
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
            return {}
    elif isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return {}
    else:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    safe = _json_ready(parsed)
    return safe if isinstance(safe, dict) else {}


safe_metadata = parse_metadata


def _safe_value(value: Any) -> Any:
    return _json_ready(value)


def _first(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return default


def _safe_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value)
    return text if text != "" else None


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else default
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[+-]?\d+", text):
            return default
        try:
            return int(text)
        except (TypeError, ValueError, OverflowError):
            return default
    return default


def _count(value: Any) -> int:
    parsed = _safe_int(value)
    return parsed if parsed is not None and parsed >= 0 else 0


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled", ""}:
            return False
    return False


def _timestamp_score(value: Any) -> float:
    if value is None:
        return float("-inf")
    if isinstance(value, datetime):
        candidate = value
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        return candidate.timestamp()
    text = str(value).strip()
    if not text:
        return float("-inf")
    try:
        candidate = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return float("-inf")
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=timezone.utc)
    try:
        return candidate.timestamp()
    except (OverflowError, OSError, ValueError):
        return float("-inf")


def _duration_ms(started_at: Any, finished_at: Any) -> Optional[int]:
    start = _timestamp_score(started_at)
    finish = _timestamp_score(finished_at)
    if start == float("-inf") or finish == float("-inf") or finish < start:
        return None
    return int(round((finish - start) * 1000))


def _now_score(value: Any = None) -> float:
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    score = _timestamp_score(value)
    return datetime.now(timezone.utc).timestamp() if score == float("-inf") else score


def _page_values(limit: Any, offset: Any) -> Tuple[int, int]:
    safe_limit = _safe_int(limit, DEFAULT_PAGE_SIZE)
    safe_offset = _safe_int(offset, 0)
    if safe_limit is None:
        safe_limit = DEFAULT_PAGE_SIZE
    if safe_offset is None or safe_offset < 0:
        safe_offset = 0
    safe_limit = max(0, min(MAX_PAGE_SIZE, safe_limit))
    return safe_limit, safe_offset


def _page_slice(items: Sequence[Dict[str, Any]], limit: Any, offset: Any) -> List[Dict[str, Any]]:
    safe_limit, safe_offset = _page_values(limit, offset)
    if safe_limit == 0:
        return []
    return list(items[safe_offset : safe_offset + safe_limit])


def _stop_reason(metadata: Mapping[str, Any], *rows: Mapping[str, Any]) -> Optional[str]:
    for item in (metadata, *rows):
        candidate_metadata = item if isinstance(item, Mapping) else {}
        value = _first(
            candidate_metadata,
            "stop_reason",
            "stopReason",
            "reason",
            "termination_reason",
            "terminationReason",
        )
        if value not in (None, ""):
            return _safe_text(value)
    return None


def _status_token(value: Any) -> str:
    """Return a comparable status/stop-reason token without parsing prose."""

    if value is None:
        return ""
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def status_label(value: Any) -> str:
    """Return the stable human label for a classified status key."""

    return STATUS_LABELS.get(_status_token(value), STATUS_LABELS["unknown"])


def _record_stop_reasons(record: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    direct = _first(record, "stop_reason", "termination_reason", "reason")
    if direct not in (None, ""):
        reasons.append(_status_token(direct))
    metadata = record.get("metadata")
    if not isinstance(metadata, Mapping):
        metadata = parse_metadata(record.get("metadata_json"))
    metadata_reason = _first(
        metadata,
        "stop_reason",
        "stopReason",
        "termination_reason",
        "terminationReason",
    )
    if metadata_reason not in (None, ""):
        reasons.append(_status_token(metadata_reason))
    return reasons


def _record_has_error(record: Mapping[str, Any]) -> bool:
    """Check persisted, structured error evidence without inspecting logs."""

    for name in (
        "errors",
        "error_count",
        "failed",
        "failure_count",
        "source_failures",
        "zoo_failures",
    ):
        value = record.get(name)
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, (int, float)):
            if value > 0:
                return True
        elif isinstance(value, (list, tuple, set, Mapping)):
            if value:
                return True
        elif value not in (None, ""):
            parsed = _safe_int(value)
            if parsed is not None and parsed > 0:
                return True

    for name in (
        "error",
        "error_summary",
        "error_category",
        "failure",
        "failure_reason",
    ):
        value = record.get(name)
        if value in (None, "", False):
            continue
        if isinstance(value, str) and _status_token(value) in {"none", "null", "ok"}:
            continue
        return True

    for name in ("errors_list", "errors_json", "error_list"):
        value = record.get(name)
        if isinstance(value, (list, tuple, set, Mapping)) and value:
            return True
        if isinstance(value, str) and value.strip() not in {"", "[]", "{}", "null"}:
            return True

    return any(reason in _FAILURE_STOP_REASONS for reason in _record_stop_reasons(record))


def _record_indicates_failure(record: Mapping[str, Any]) -> bool:
    raw = _status_token(
        record.get("raw_status")
        or record.get("run_status")
        or record.get("status")
    )
    source_status = _status_token(record.get("source_status"))
    return (
        raw in _FAILED_STATUSES
        or raw in _WARNING_STATUSES
        or source_status in _FAILED_STATUSES
        or source_status in _WARNING_STATUSES
        or _record_has_error(record)
    )


def classify_status(
    record: Mapping[str, Any],
    *,
    liveness_status: Optional[str] = None,
    failure_records: Iterable[Mapping[str, Any]] = (),
) -> str:
    """Classify a run/result using only structured state and evidence.

    ``liveness_status`` is supplied by the run classifier after applying the
    existing heartbeat/lease rules.  Result rows do not have that evidence and
    therefore use their persisted status directly.  The raw status is never
    inferred from a message or log string.
    """

    raw = _status_token(
        record.get("raw_status")
        or record.get("run_status")
        or record.get("status")
    )
    if liveness_status is not None and raw in _RUNNING_STATUSES:
        live = _status_token(liveness_status)
        return live if live in {"running", "stale", "unknown"} else "unknown"
    if raw in _RUNNING_STATUSES:
        return "running"
    if raw == "stale":
        return "stale"
    if raw in {"", "unknown", "not_recorded", "not_observable"}:
        return "warning" if _record_has_error(record) else "unknown"
    if raw in _FAILED_STATUSES:
        return "failed"
    if raw in _WARNING_STATUSES:
        return "warning"
    if raw == "budget_hit":
        return "budget_hit"

    related = [item for item in failure_records if isinstance(item, Mapping)]
    has_failure = any(_record_indicates_failure(item) for item in related)
    has_error = _record_has_error(record) or has_failure
    reasons = _record_stop_reasons(record)
    for item in related:
        reasons.extend(_record_stop_reasons(item))
    if any(reason in _FAILURE_STOP_REASONS for reason in reasons):
        has_error = True
    has_budget = any(reason in _BUDGET_STOP_REASONS for reason in reasons)

    if raw in _SUCCESS_STATUSES or raw in _TERMINAL_STATUSES:
        if has_error:
            return "warning"
        if has_budget:
            return "budget_hit"
        return "success"
    if has_error:
        return "warning"
    if has_budget:
        return "budget_hit"
    return "unknown"


def with_status_fields(
    record: Mapping[str, Any],
    *,
    classified_status: Optional[str] = None,
    liveness_status: Optional[str] = None,
    failure_records: Iterable[Mapping[str, Any]] = (),
    terminal_status: Any = None,
) -> Dict[str, Any]:
    """Add the stable display fields while retaining raw compatibility keys."""

    result = dict(record)
    raw = _safe_text(
        result.get("raw_status")
        or result.get("run_status")
        or result.get("status")
    ) or "unknown"
    classified = _status_token(classified_status) if classified_status else ""
    if classified not in STATUS_LABELS:
        classified = classify_status(
            result,
            liveness_status=liveness_status,
            failure_records=failure_records,
        )
    result.update(
        {
            "status": classified,
            "classified_status": classified,
            "display_status": classified,
            "status_key": classified,
            "status_label": status_label(classified),
            "raw_status": raw,
        }
    )
    if "run_status" not in result:
        result["run_status"] = raw
    if terminal_status is not None:
        result["terminal_status"] = _safe_text(terminal_status)
    return result


class ConsoleRepository:
    """Read-only repository over a crawler SQLite database.

    ``database`` may be a path, a ``sqlite3.Connection``, or an existing
    ``SQLiteStorage`` instance.  Existing connections are borrowed and never
    reconfigured or closed.  Paths are opened in SQLite read-only mode; a
    missing path is represented by an in-memory empty view rather than being
    created.
    """

    def __init__(
        self,
        database: Union[str, Path, sqlite3.Connection, Any] = ":memory:",
        *,
        connection: Optional[sqlite3.Connection] = None,
        storage: Any = None,
    ) -> None:
        supplied = connection if connection is not None else storage
        if supplied is None and not isinstance(database, (str, Path, sqlite3.Connection)):
            supplied = database
        self._owns_connection = False
        if supplied is not None:
            borrowed = getattr(supplied, "connection", supplied)
            if not isinstance(borrowed, sqlite3.Connection):
                raise TypeError("database must be a path, sqlite3.Connection, or storage object")
            self._connection = borrowed
        elif isinstance(database, sqlite3.Connection):
            self._connection = database
        else:
            path = str(database)
            try:
                if path == ":memory:":
                    self._connection = sqlite3.connect(":memory:")
                else:
                    candidate = Path(path).expanduser().resolve()
                    if candidate.is_file():
                        uri = "file:" + quote(str(candidate), safe="/") + "?mode=ro"
                        self._connection = sqlite3.connect(uri, uri=True)
                    else:
                        self._connection = sqlite3.connect(":memory:")
                self._connection.row_factory = sqlite3.Row
                self._connection.execute("PRAGMA query_only = ON")
            except sqlite3.Error as error:
                raise ConsoleRepositoryError("console database open failed") from error
            self._owns_connection = True
        self._table_cache: Dict[str, Optional[str]] = {}
        self._column_cache: Dict[str, Dict[str, str]] = {}
        self._column_info_cache: Dict[str, Dict[str, str]] = {}

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        if self._owns_connection:
            self._connection.close()
            self._owns_connection = False

    def __enter__(self) -> "ConsoleRepository":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _columns(self, table: str) -> Dict[str, str]:
        if table in self._column_cache:
            return self._column_cache[table]
        try:
            rows = self._connection.execute(
                "PRAGMA table_info(" + _quote_identifier(table) + ")"
            ).fetchall()
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database introspection failed") from error
        columns: Dict[str, str] = {}
        for row in rows:
            if isinstance(row, sqlite3.Row):
                name = row["name"]
            elif isinstance(row, Mapping):
                name = row.get("name")
            else:
                name = row[1] if len(row) > 1 else None
            if name is not None:
                columns[str(name).casefold()] = str(name)
        self._column_cache[table] = columns
        return columns

    def _column_info(self, table: str) -> Dict[str, str]:
        if table in self._column_info_cache:
            return self._column_info_cache[table]
        try:
            rows = self._connection.execute(
                "PRAGMA table_info(" + _quote_identifier(table) + ")"
            ).fetchall()
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database introspection failed") from error
        info: Dict[str, str] = {}
        for row in rows:
            if isinstance(row, sqlite3.Row):
                name, declared_type = row["name"], row["type"]
            elif isinstance(row, Mapping):
                name, declared_type = row.get("name"), row.get("type")
            else:
                name = row[1] if len(row) > 1 else None
                declared_type = row[2] if len(row) > 2 else None
            if name is not None:
                info[str(name).casefold()] = str(declared_type or "").strip().upper()
        self._column_info_cache[table] = info
        return info

    def _table(self, logical_name: str) -> Optional[str]:
        if logical_name in self._table_cache:
            return self._table_cache[logical_name]
        selected: Optional[str] = None
        for candidate in _TABLE_ALIASES.get(logical_name, (logical_name,)):
            if self._columns(candidate):
                selected = candidate
                break
        self._table_cache[logical_name] = selected
        return selected

    def _column(self, table: str, *names: str) -> Optional[str]:
        columns = self._columns(table)
        for name in names:
            found = columns.get(name.casefold())
            if found:
                return found
        return None

    def _expr(self, table: str, names: Sequence[str], *, rowid: bool = False) -> str:
        column = self._column(table, *names)
        if column:
            return _quote_identifier(column)
        if rowid:
            return "rowid"
        return "NULL"

    def _query(self, sql: str, args: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        try:
            cursor = self._connection.execute(sql, tuple(args))
            description = cursor.description or ()
            return [_row_dict(row, description) for row in cursor.fetchall()]
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise ConsoleRepositoryError("console database query failed") from error

    def _execute(self, sql: str, args: Sequence[Any] = ()) -> Any:
        try:
            return self._connection.execute(sql, tuple(args))
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database query failed") from error

    @staticmethod
    def _fetchone(cursor: Any) -> Any:
        try:
            return cursor.fetchone()
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database query failed") from error

    @staticmethod
    def _fetchall(cursor: Any) -> List[Any]:
        try:
            return cursor.fetchall()
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database query failed") from error

    @staticmethod
    def _fetchmany(cursor: Any, size: int) -> List[Any]:
        try:
            return cursor.fetchmany(size)
        except sqlite3.Error as error:
            raise ConsoleRepositoryError("console database query failed") from error

    def _scalar(self, sql: str, args: Sequence[Any] = (), default: Any = 0) -> Any:
        rows = self._query(sql, args)
        if not rows:
            return default
        return next(iter(rows[0].values()), default)

    def _select_projection(
        self,
        logical_name: str,
        projection: Mapping[str, Sequence[str]],
        *,
        where: Optional[str] = None,
        args: Sequence[Any] = (),
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        table = self._table(logical_name)
        if table is None:
            return []
        select_parts = [
            self._expr(table, names, rowid=label in {"id", "event_id"})
            + " AS "
            + _quote_identifier(label)
            for label, names in projection.items()
        ]
        sql = "SELECT " + ", ".join(select_parts) + " FROM " + _quote_identifier(table)
        if where:
            sql += " WHERE " + where
        if order_by:
            sql += " ORDER BY " + order_by
        if limit is not None:
            sql += " LIMIT ?"
            args = tuple(args) + (limit,)
            if offset is not None:
                sql += " OFFSET ?"
                args = tuple(args) + (offset,)
        return self._query(sql, args)

    def _run_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "run_id", "crawl_run_id"),
            "batch_id": ("batch_id", "batch", "batch_key"),
            "started_at": ("started_at", "start_time", "started", "created_at"),
            "finished_at": ("finished_at", "completed_at", "ended_at", "end_time", "terminal_at", "stopped_at"),
            "duration_ms": ("duration_ms", "elapsed_ms", "duration"),
            "status": ("status", "state"),
            "error": ("error", "last_error", "error_message"),
            "heartbeat_at": ("heartbeat_at", "last_heartbeat_at", "heartbeat", "last_seen_at"),
            "progress_at": ("progress_at", "last_progress_at", "progress_updated_at"),
            "current_phase": ("current_phase", "phase", "current_step"),
            "lease_until": ("lease_until", "lease_expires_at", "lease_expiry", "expires_at"),
            "terminal": ("is_terminal", "terminal", "finished", "completed"),
            "current_zoo_id": ("current_zoo_id", "active_zoo_id", "zoo_id"),
            "current_source_id": ("current_source_id", "active_source_id", "source_id"),
            "stop_reason": ("stop_reason", "termination_reason", "reason"),
            "progress_raw": ("progress_json", "progress", "progress_metadata"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
            "updated_at": ("updated_at", "last_updated", "modified_at"),
        }

    def _run_record(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = _safe_text(_first(row, "id"))
        metadata = parse_metadata(_first(row, "metadata_raw"))
        duration = _safe_int(_first(row, "duration_ms"))
        if duration is None:
            duration = _duration_ms(_first(row, "started_at"), _first(row, "finished_at"))
        heartbeat_at = _safe_value(
            _first(row, "heartbeat_at", default=metadata.get("heartbeat_at"))
        )
        progress_at = _safe_value(
            _first(
                row,
                "progress_at",
                default=_first(metadata, "progress_at", "progressAt"),
            )
        )
        current_phase = _safe_text(
            _first(
                row,
                "current_phase",
                default=_first(metadata, "current_phase", "currentPhase", "phase"),
            )
        )
        lease_until = _safe_value(
            _first(row, "lease_until", default=metadata.get("lease_until"))
        )
        terminal_value = _first(row, "terminal", default=metadata.get("terminal"))
        progress = parse_metadata(_first(row, "progress_raw"))
        if not progress:
            progress = parse_metadata(_first(metadata, "progress", "progress_json"))
        last_updated = _safe_value(
            _first(
                row,
                "updated_at",
                default=_first(metadata, "last_updated", "updated_at"),
            )
        )
        if last_updated in (None, ""):
            candidates = [
                heartbeat_at,
                progress_at,
                _first(row, "finished_at"),
                _first(row, "started_at"),
            ]
            valid = [item for item in candidates if _timestamp_score(item) != float("-inf")]
            last_updated = max(valid, key=_timestamp_score) if valid else None
        stop_reason = _safe_text(
            _first(row, "stop_reason", default=metadata.get("stop_reason"))
        )
        return {
            "id": run_id,
            "run_id": run_id,
            "batch_id": _safe_text(_first(row, "batch_id")),
            "started_at": _safe_value(_first(row, "started_at")),
            "finished_at": _safe_value(_first(row, "finished_at")),
            "duration_ms": duration,
            "elapsed_ms": duration,
            "status": _safe_text(_first(row, "status")) or "unknown",
            "run_status": _safe_text(_first(row, "status")) or "unknown",
            "error": _safe_text(_first(row, "error")),
            "heartbeat_at": heartbeat_at,
            "progress_at": progress_at,
            "current_phase": current_phase,
            "lease_until": lease_until,
            "terminal": _safe_bool(terminal_value) if terminal_value is not None else None,
            "current_zoo_id": _safe_text(_first(row, "current_zoo_id")),
            "current_source_id": _safe_text(_first(row, "current_source_id")),
            "progress": progress,
            "metadata": metadata,
            "last_updated": last_updated,
            "stop_reason": stop_reason or _stop_reason(metadata),
        }

    def _run_rows(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        table = self._table("runs")
        if table is None:
            return []
        status_column = self._column(table, "status", "state")
        clauses: List[str] = []
        args: List[Any] = []
        if status and status_column:
            clauses.append("LOWER(CAST(" + _quote_identifier(status_column) + " AS TEXT)) = LOWER(?)")
            args.append(str(status))
        elif status and not status_column:
            return []
        started = self._expr(table, ("started_at", "start_time", "started", "created_at"))
        identifier = self._expr(table, ("id", "run_id", "crawl_run_id"), rowid=True)
        where = " AND ".join(clauses) if clauses else None
        rows = self._select_projection(
            "runs",
            self._run_projection(),
            where=where,
            args=args,
            order_by=(
                "CASE WHEN " + started + " IS NULL OR CAST(" + started + " AS TEXT) = '' THEN 1 ELSE 0 END, "
                + "CAST(" + started + " AS TEXT) DESC, " + identifier + " DESC"
            ),
        )
        records = [self._run_record(row) for row in rows]
        records.sort(
            key=lambda row: (
                _timestamp_score(row.get("started_at")),
                str(row.get("id") or ""),
            ),
            reverse=True,
        )
        return records

    def list_runs(
        self,
        status: Optional[str] = None,
        limit: Any = None,
        offset: Any = 0,
        *,
        now: Any = None,
        stale_after_seconds: Any = 300,
    ) -> List[Dict[str, Any]]:
        """Return all matching runs, newest first, without mutating SQLite."""

        requested_status = (
            str(status).strip().casefold()
            if status not in (None, "")
            else None
        )
        # Always expose the classified status.  Raw status filters remain
        # accepted for compatibility with the existing dashboard/API, but a
        # caller must never receive a stale ``running`` row as if it were
        # live merely because the persisted status string says so.
        raw_rows = self._run_rows()
        rows = [
            self._classify_run(
                row,
                now=now,
                stale_after_seconds=stale_after_seconds,
            )
            for row in raw_rows
        ]
        if requested_status in _CLASSIFIED_STATUSES:
            rows = [row for row in rows if row.get("status") == requested_status]
        elif requested_status in _LEGACY_TERMINAL_FILTERS:
            # ``finished`` was the old display bucket.  Keep it as a broad
            # terminal filter while exposing the more useful outcome classes.
            rows = [
                row
                for row in rows
                if row.get("terminal_status") not in (None, "")
                or row.get("status") in {"success", "warning", "failed", "budget_hit"}
            ]
        elif requested_status:
            rows = [
                row
                for row in rows
                if str(row.get("run_status") or row.get("status") or "").casefold()
                == requested_status
            ]
        return rows if limit is None else _page_slice(rows, limit, offset)

    get_runs = list_runs
    list_crawl_runs = list_runs

    def count_runs(
        self,
        status: Optional[str] = None,
        *,
        now: Any = None,
        stale_after_seconds: Any = 300,
    ) -> int:
        return len(
            self.list_runs(
                status=status,
                now=now,
                stale_after_seconds=stale_after_seconds,
            )
        )

    def get_run(self, run_id: Any) -> Optional[Dict[str, Any]]:
        wanted = _safe_text(run_id)
        if not wanted or self._table("runs") is None:
            return None
        row = next((row for row in self._run_rows() if row["id"] == wanted), None)
        return self._classify_run(row) if row is not None else None

    get_crawl_run = get_run

    def _lease_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "lease_id", "name"),
            "name": ("name", "lease_name"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "heartbeat_at": ("heartbeat_at", "last_heartbeat_at", "heartbeat", "last_seen_at"),
            "lease_until": ("lease_until", "lease_expires_at", "lease_expiry", "expires_at"),
            "status": ("status", "state"),
            "owner": ("owner", "worker", "worker_id"),
        }

    def _lease_records(self) -> List[Dict[str, Any]]:
        table = self._table("leases")
        if table is None:
            return []
        rows = self._select_projection(
            "leases",
            self._lease_projection(),
            order_by=self._expr(table, ("heartbeat_at", "last_heartbeat_at", "heartbeat", "last_seen_at"))
            + " DESC, "
            + self._expr(table, ("id", "lease_id", "name"), rowid=True)
            + " DESC",
        )
        return [
            {
                "id": _safe_text(_first(row, "id")),
                "name": _safe_text(_first(row, "name")),
                "run_id": _safe_text(_first(row, "run_id")),
                "heartbeat_at": _safe_value(_first(row, "heartbeat_at")),
                "lease_until": _safe_value(_first(row, "lease_until")),
                "status": _safe_text(_first(row, "status")),
                "owner": _safe_text(_first(row, "owner")),
            }
            for row in rows
        ]

    def _lease_for_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        leases = self._lease_records()
        matching = [row for row in leases if row.get("run_id") == str(run_id)]
        # A lease without an explicit run id is process-level evidence only. It
        # must not be attributed to an arbitrary run in a historical database.
        return matching[0] if matching else None

    def _classify_run(
        self,
        run: Mapping[str, Any],
        *,
        now: Any = None,
        stale_after_seconds: Any = 300,
    ) -> Dict[str, Any]:
        original_status = _safe_text(run.get("run_status") or run.get("status")) or "unknown"
        token = _status_token(original_status)
        terminal_evidence = (
            run.get("finished_at") not in (None, "")
            or run.get("terminal") is True
            or token in _TERMINAL_STATUSES
        )
        lease = self._lease_for_run(str(run.get("id") or ""))
        heartbeat_at = run.get("heartbeat_at") or (lease or {}).get("heartbeat_at")
        lease_until = run.get("lease_until") or (lease or {}).get("lease_until")
        now_value = _now_score(now)
        heartbeat_score = _timestamp_score(heartbeat_at)
        lease_score = _timestamp_score(lease_until)
        has_liveness_evidence = (
            heartbeat_score != float("-inf")
            or lease_score != float("-inf")
        )
        age = None
        if heartbeat_score != float("-inf"):
            age = max(0.0, now_value - heartbeat_score)
        parsed_stale_limit = _safe_int(stale_after_seconds)
        stale_limit = 300 if parsed_stale_limit is None else parsed_stale_limit
        if stale_limit < 0:
            stale_limit = 0
        reason: str
        failure_records: List[Mapping[str, Any]] = []
        run_id = _safe_text(run.get("id"))
        if run_id:
            # These are read-only evidence rows.  They let a structurally
            # completed run surface a source/zoo failure as Warning without
            # parsing an event or log message.
            failure_records.extend(self._stats_rows(run_id))
            failure_records.extend(self._zoo_result_rows(run_id))

        if not terminal_evidence and token in _RUNNING_STATUSES:
            state: str
            if not has_liveness_evidence:
                state = "unknown"
                reason = "no_liveness_evidence"
            else:
                lease_status = str((lease or {}).get("status") or "").casefold()
                if lease_status in {"released", "expired", "revoked", "inactive", "stopped"}:
                    state = "stale"
                    reason = "lease_inactive"
                elif lease_score != float("-inf") and lease_score <= now_value:
                    state = "stale"
                    reason = "lease_expired"
                elif age is not None and age >= stale_limit:
                    state = "stale"
                    reason = "heartbeat_expired"
                else:
                    state = "running"
                    reason = "heartbeat_fresh" if age is not None else "lease_fresh"
        elif terminal_evidence:
            # A row can retain the raw running value after a terminal write in
            # a legacy database.  The finished timestamp/terminal flag is
            # structured terminal evidence, but the outcome remains unknown
            # unless the raw status or failure evidence says more.
            classification_record = dict(run)
            if token in _RUNNING_STATUSES:
                classification_record["raw_status"] = "completed"
                classification_record["run_status"] = "completed"
            state = classify_status(
                classification_record,
                failure_records=failure_records,
            )
            reason = "terminal_state"
        else:
            state = "unknown"
            reason = "unrecognized_run_status"
        classified = dict(run)
        classified["raw_status"] = original_status
        progress = classified.get("progress")
        has_progress = isinstance(progress, Mapping) and bool(progress)
        has_activity_evidence = bool(
            has_progress
            or classified.get("current_phase")
            or classified.get("current_zoo_id")
            or classified.get("current_source_id")
        )
        # Current activity is an evidence-backed projection, not a placeholder
        # object.  In particular, a legacy running row with no heartbeat (or a
        # heartbeat with no phase/progress/scope evidence) must remain null.
        current_activity: Optional[Dict[str, Any]] = None
        if (
            state in {"running", "stale", "unknown"}
            and heartbeat_score != float("-inf")
            and has_activity_evidence
        ):
            current_activity = {
                "current_zoo_id": classified.get("current_zoo_id"),
                "current_source_id": classified.get("current_source_id"),
                "progress": progress if isinstance(progress, Mapping) else {},
                "status": state,
                "stop_reason": classified.get("stop_reason"),
            }
            if classified.get("current_phase") is not None:
                current_activity["current_phase"] = classified.get("current_phase")
            if classified.get("progress_at") is not None:
                current_activity["progress_at"] = classified.get("progress_at")
        classified = with_status_fields(
            {
                **classified,
                "run_status": original_status,
            },
            classified_status=state,
            failure_records=failure_records,
            terminal_status=original_status if terminal_evidence else None,
        )
        classified.update(
            {
                "heartbeat_at": _safe_value(heartbeat_at),
                "lease_until": _safe_value(lease_until),
                "heartbeat_age_seconds": int(age) if age is not None else None,
                "stale": state == "stale",
                "status_reason": reason,
                "status_observable": state != "unknown",
                "current_activity": current_activity,
            }
        )
        return classified

    def get_active_run(
        self, *, now: Any = None, stale_after_seconds: Any = 300
    ) -> Optional[Dict[str, Any]]:
        for row in self._run_rows():
            classified = self._classify_run(
                row, now=now, stale_after_seconds=stale_after_seconds
            )
            if classified.get("status") == "running":
                return classified
        return None

    current_run = get_active_run

    def get_current_status(
        self, *, now: Any = None, stale_after_seconds: Any = 300
    ) -> Dict[str, Any]:
        runs = self._run_rows()
        if not runs:
            return with_status_fields(
                {
                "status": "unknown",
                "active": False,
                "is_active": False,
                "run_id": None,
                "run": None,
                "active_run": None,
                "current_run": None,
                "current_activity": None,
                "heartbeat_at": None,
                "progress_at": None,
                "current_phase": None,
                "last_updated": None,
                "poll_hint": {
                    "enabled": False,
                    "interval_seconds": None,
                    "after_id": None,
                },
                "poll_after_seconds": None,
                "status_reason": "no_runs",
                "status_observable": False,
                "observability": {"observable": False, "reason": "no_runs"},
                },
                classified_status="unknown",
            )
        classified_runs = [
            self._classify_run(
                run, now=now, stale_after_seconds=stale_after_seconds
            )
            for run in runs
        ]
        current = next(
            (run for run in classified_runs if run.get("status") == "running"),
            next(
                (run for run in classified_runs if run.get("status") == "stale"),
                classified_runs[0],
            ),
        )
        is_active = current.get("status") == "running"
        observable = current.get("status") != "unknown"
        reason = current.get("status_reason") or "unknown"
        result = {
            "status": current.get("status") or "unknown",
            "raw_status": current.get("raw_status") or current.get("run_status") or "unknown",
            "active": is_active,
            "is_active": is_active,
            "run_id": current.get("id"),
            "run": current,
            "active_run": current if current.get("status") == "running" else None,
            "current_run": current,
            "current_activity": current.get("current_activity") if is_active else None,
            "heartbeat_at": current.get("heartbeat_at"),
            "progress_at": current.get("progress_at"),
            "current_phase": current.get("current_phase"),
            "last_updated": current.get("last_updated") or current.get("heartbeat_at") or current.get("progress_at"),
            "poll_hint": {
                "enabled": is_active,
                "interval_seconds": 2.5 if is_active else None,
                "after_id": None,
            },
            "poll_after_seconds": 2.5 if is_active else None,
            "status_reason": reason,
            "status_observable": observable,
            "observability": {"observable": observable, "reason": reason},
        }
        return with_status_fields(
            result,
            classified_status=current.get("status") or "unknown",
            terminal_status=current.get("terminal_status"),
        )

    current_status = get_current_status
    get_crawler_status = get_current_status

    current_run = get_active_run

    def _stats_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "stat_id", "run_stat_id"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "zoo_id": ("zoo_id", "zoo", "zoo_key"),
            "source_id": ("source_id", "source", "source_key"),
            "status": ("status", "state"),
            "discovered": ("discovered_count", "discovered", "discovered_urls"),
            "fetched": ("fetched_count", "fetched", "parsed_count", "parsed"),
            "stored": ("stored_count", "stored", "inserted", "new_count"),
            "already_known": ("already_known_count", "already_known", "known_count"),
            "duplicate": (
                "duplicate_candidate_count",
                "duplicate_candidates",
                "duplicate_filtered",
                "duplicates",
                "duplicate_count",
            ),
            "errors": ("error_count", "errors_count", "failed_count", "failed", "errors"),
            "started_at": ("started_at", "start_time", "started"),
            "finished_at": ("finished_at", "completed_at", "ended_at", "end_time"),
            "duration_ms": ("duration_ms", "elapsed_ms", "duration"),
            "error": ("error", "last_error", "error_message"),
            "http_status": ("http_status", "status_code", "last_http_status"),
            "error_category": ("error_category", "error_type"),
            "error_summary": ("error_summary", "error_message", "last_error"),
            "source_url": ("source_url", "url"),
            "errors_raw": ("errors_json", "errors", "error_json", "error_list"),
            "stop_reason": ("stop_reason", "termination_reason", "reason"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
        }

    def _stats_rows(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        table = self._table("stats")
        if table is None:
            return []
        run_column = self._column(table, "crawl_run_id", "run_id", "crawl_id")
        where: Optional[str] = None
        args: List[Any] = []
        if run_id is not None:
            if not run_column:
                return []
            where = _quote_identifier(run_column) + " = ?"
            args.append(str(run_id))
        started = self._expr(table, ("started_at", "start_time", "started"))
        identifier = self._expr(table, ("id", "stat_id", "run_stat_id"), rowid=True)
        rows = self._select_projection(
            "stats",
            self._stats_projection(),
            where=where,
            args=args,
            order_by="CAST(" + started + " AS TEXT) ASC, " + identifier + " ASC",
        )
        result: List[Dict[str, Any]] = []
        for row in rows:
            metadata = parse_metadata(_first(row, "metadata_raw"))
            errors_raw = _first(row, "errors_raw")
            errors: List[str] = []
            if isinstance(errors_raw, list):
                errors = [str(value) for value in errors_raw if value not in (None, "")]
            elif isinstance(errors_raw, str) and errors_raw.strip():
                try:
                    parsed_errors = json.loads(errors_raw)
                    if isinstance(parsed_errors, list):
                        errors = [str(value) for value in parsed_errors if value not in (None, "")]
                    elif parsed_errors not in (None, ""):
                        errors = [str(parsed_errors)]
                except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                    errors = [errors_raw]
            error_count = _safe_int(_first(row, "errors"))
            if error_count is None:
                error_count = len(errors)
            duration = _safe_int(_first(row, "duration_ms"))
            if duration is None:
                duration = _duration_ms(_first(row, "started_at"), _first(row, "finished_at"))
            result.append(
                {
                    "id": _safe_text(_first(row, "id")),
                    "run_id": _safe_text(_first(row, "run_id")),
                    "crawl_run_id": _safe_text(_first(row, "run_id")),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "source_id": _safe_text(_first(row, "source_id")),
                    "status": _safe_text(_first(row, "status")) or "unknown",
                    "discovered": _count(_first(row, "discovered")),
                    "discovered_count": _count(_first(row, "discovered")),
                    "fetched": _count(_first(row, "fetched")),
                    "fetched_count": _count(_first(row, "fetched")),
                    "stored": _count(_first(row, "stored")),
                    "stored_count": _count(_first(row, "stored")),
                    "already_known": _count(_first(row, "already_known")),
                    "already_known_count": _count(_first(row, "already_known")),
                    "duplicate": _count(_first(row, "duplicate")),
                    "duplicate_candidate_count": _count(_first(row, "duplicate")),
                    "errors": max(0, error_count),
                    "error_count": max(0, error_count),
                    "started_at": _safe_value(_first(row, "started_at")),
                    "finished_at": _safe_value(_first(row, "finished_at")),
                    "duration_ms": duration,
                    "error": _safe_text(_first(row, "error")),
                    "http_status": _safe_int(_first(row, "http_status")),
                    "error_category": _safe_text(_first(row, "error_category")),
                    "error_summary": _safe_text(_first(row, "error_summary")),
                    "source_url": _safe_text(_first(row, "source_url")),
                    "errors_list": errors,
                    "metadata": metadata,
                    "stop_reason": _safe_text(_first(row, "stop_reason"))
                    or _stop_reason(metadata),
                }
            )
        return result

    def list_run_stats(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._stats_rows(str(run_id) if run_id is not None else None)

    get_run_stats = list_run_stats

    def _zoo_result_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "result_id", "zoo_result_id"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "zoo_id": ("zoo_id", "zoo", "zoo_key"),
            "zoo_slug": ("zoo_slug", "slug"),
            "zoo_name": ("zoo_name", "name"),
            "status": ("status", "state"),
            "source_status": ("source_status", "source_state"),
            "discovered": ("discovered", "discovered_count", "discovered_urls"),
            "fetched": ("parsed", "parsed_count", "fetched", "fetched_count"),
            "stored": ("inserted", "inserted_count", "stored", "stored_count", "new_count"),
            "updated": ("updated", "updated_count"),
            "errors": ("failed", "failed_count", "error_count", "errors"),
            "duplicate": (
                "duplicate_filtered",
                "duplicate_filtered_count",
                "duplicate_candidate_count",
                "duplicates",
            ),
            "duration_ms": ("duration_ms", "elapsed_ms", "duration"),
            "source_url": ("source_url", "url"),
            "http_status": ("http_status", "status_code"),
            "error_category": ("error_category", "error_type"),
            "error_summary": ("error_summary", "error", "last_error", "error_message"),
            "stop_reason": ("stop_reason", "termination_reason", "reason"),
            "started_at": ("started_at", "start_time", "started"),
            "finished_at": ("finished_at", "completed_at", "ended_at", "end_time"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
        }

    def _zoo_result_rows(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        table = self._table("zoo_results")
        if table is None:
            return []
        run_column = self._column(table, "crawl_run_id", "run_id", "crawl_id")
        where: Optional[str] = None
        args: List[Any] = []
        if run_id is not None:
            if not run_column:
                return []
            where = _quote_identifier(run_column) + " = ?"
            args.append(str(run_id))
        started = self._expr(table, ("started_at", "start_time", "started"))
        identifier = self._expr(table, ("id", "result_id", "zoo_result_id"), rowid=True)
        rows = self._select_projection(
            "zoo_results",
            self._zoo_result_projection(),
            where=where,
            args=args,
            order_by="CAST(" + started + " AS TEXT) ASC, " + identifier + " ASC",
        )
        result: List[Dict[str, Any]] = []
        for row in rows:
            metadata = parse_metadata(_first(row, "metadata_raw"))
            duration = _safe_int(_first(row, "duration_ms"))
            if duration is None:
                duration = _duration_ms(_first(row, "started_at"), _first(row, "finished_at"))
            error_count = _count(_first(row, "errors"))
            result.append(
                {
                    "id": _safe_text(_first(row, "id")),
                    "run_id": _safe_text(_first(row, "run_id")),
                    "crawl_run_id": _safe_text(_first(row, "run_id")),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "zoo_slug": _safe_text(_first(row, "zoo_slug")),
                    "zoo_name": _safe_text(_first(row, "zoo_name")),
                    "status": _safe_text(_first(row, "status")) or "unknown",
                    "source_status": _safe_text(_first(row, "source_status")),
                    "discovered": _count(_first(row, "discovered")),
                    "discovered_count": _count(_first(row, "discovered")),
                    "fetched": _count(_first(row, "fetched")),
                    "fetched_count": _count(_first(row, "fetched")),
                    "stored": _count(_first(row, "stored")),
                    "stored_count": _count(_first(row, "stored")),
                    "updated": _count(_first(row, "updated")),
                    "errors": error_count,
                    "error_count": error_count,
                    "duplicate": _count(_first(row, "duplicate")),
                    "duplicate_filtered": _count(_first(row, "duplicate")),
                    "duration_ms": duration,
                    "source_url": _safe_text(_first(row, "source_url")),
                    "http_status": _safe_int(_first(row, "http_status")),
                    "error_category": _safe_text(_first(row, "error_category")),
                    "error_summary": _safe_text(_first(row, "error_summary")),
                    "started_at": _safe_value(_first(row, "started_at")),
                    "finished_at": _safe_value(_first(row, "finished_at")),
                    "metadata": metadata,
                    "stop_reason": _safe_text(_first(row, "stop_reason"))
                    or _stop_reason(metadata),
                }
            )
        return result

    def list_zoo_results(self, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._zoo_result_rows(str(run_id) if run_id is not None else None)

    get_zoo_results = list_zoo_results

    def _zoo_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "zoo_id"),
            "slug": ("slug", "zoo_slug"),
            "name": ("name", "zoo_name"),
            "website_url": ("website_url", "url", "website"),
            "country_code": ("country_code", "country"),
            "language": ("language", "lang"),
            "enabled": ("enabled", "is_enabled", "active"),
            "region": ("region",),
            "city": ("city",),
            "source_status": ("source_status", "status"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
            "created_at": ("created_at",),
            "updated_at": ("updated_at",),
        }

    def _zoo_records(self) -> List[Dict[str, Any]]:
        rows = self._select_projection("zoos", self._zoo_projection())
        result: List[Dict[str, Any]] = []
        for row in rows:
            zoo_id = _safe_text(_first(row, "id"))
            result.append(
                {
                    "id": zoo_id,
                    "zoo_id": zoo_id,
                    "slug": _safe_text(_first(row, "slug")),
                    "name": _safe_text(_first(row, "name")) or _safe_text(_first(row, "slug")) or zoo_id,
                    "website_url": _safe_text(_first(row, "website_url")),
                    "country_code": _safe_text(_first(row, "country_code")),
                    "language": _safe_text(_first(row, "language")),
                    "enabled": _safe_bool(_first(row, "enabled"), default=True),
                    "region": _safe_text(_first(row, "region")),
                    "city": _safe_text(_first(row, "city")),
                    "source_status": _safe_text(_first(row, "source_status")),
                    "metadata": parse_metadata(_first(row, "metadata_raw")),
                    "created_at": _safe_value(_first(row, "created_at")),
                    "updated_at": _safe_value(_first(row, "updated_at")),
                }
            )
        result.sort(key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("id") or "")))
        return result

    def list_zoos(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        rows = self._zoo_records()
        if enabled_only:
            rows = [row for row in rows if row.get("enabled")]
        return rows

    get_zoos = list_zoos

    def get_zoo(self, zoo_id: Any) -> Optional[Dict[str, Any]]:
        wanted = _safe_text(zoo_id)
        if not wanted:
            return None
        return next(
            (
                row
                for row in self._zoo_records()
                if row.get("id") == wanted or row.get("slug") == wanted
            ),
            None,
        )

    get_zoo_record = get_zoo

    def _source_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "source_id"),
            "zoo_id": ("zoo_id", "zoo"),
            "url": ("url", "source_url", "website_url"),
            "normalized_url": ("normalized_url",),
            "kind": ("kind", "source_type", "type"),
            "name": ("name", "label", "source_name"),
            "language": ("language", "lang"),
            "enabled": ("enabled", "is_enabled", "active"),
            "status": ("status", "state", "source_status"),
            "success": ("success", "last_successful"),
            "last_checked": ("last_checked", "checked_at", "last_check"),
            "last_success": ("last_success", "last_success_at"),
            "last_error": ("last_error", "error", "error_message"),
            "last_http_status": ("last_http_status", "http_status", "status_code"),
            "config_raw": ("config_json", "config", "metadata_json", "metadata"),
        }

    def _source_records(self) -> List[Dict[str, Any]]:
        rows = self._select_projection("sources", self._source_projection())
        result: List[Dict[str, Any]] = []
        for row in rows:
            source_id = _safe_text(_first(row, "id"))
            result.append(
                {
                    "id": source_id,
                    "source_id": source_id,
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "url": _safe_text(_first(row, "url")),
                    "normalized_url": _safe_text(_first(row, "normalized_url")),
                    "kind": _safe_text(_first(row, "kind")) or "unknown",
                    "name": _safe_text(_first(row, "name")) or source_id,
                    "language": _safe_text(_first(row, "language")),
                    "enabled": _safe_bool(_first(row, "enabled"), default=True),
                    "status": _safe_text(_first(row, "status")) or "unknown",
                    "success": _safe_bool(_first(row, "success"), default=False)
                    if _first(row, "success") is not None
                    else None,
                    "last_checked": _safe_value(_first(row, "last_checked")),
                    "last_success": _safe_value(_first(row, "last_success")),
                    "last_error": _safe_text(_first(row, "last_error")),
                    "last_http_status": _safe_int(_first(row, "last_http_status")),
                    "config": parse_metadata(_first(row, "config_raw")),
                }
            )
        result.sort(key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("id") or "")))
        return result

    def list_sources(self, zoo_id: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self._source_records()
        if zoo_id is not None:
            zoo = self.get_zoo(zoo_id)
            wanted = str(zoo.get("id") if zoo else zoo_id)
            rows = [row for row in rows if row.get("zoo_id") == wanted]
        return rows

    get_sources = list_sources

    def _source_map(self) -> Dict[str, Dict[str, Any]]:
        return {str(row.get("id")): row for row in self._source_records() if row.get("id") is not None}

    def _zoo_map(self) -> Dict[str, Dict[str, Any]]:
        return {str(row.get("id")): row for row in self._zoo_records() if row.get("id") is not None}

    @staticmethod
    def _metric(rows: Iterable[Mapping[str, Any]], name: str) -> int:
        return sum(_count(row.get(name)) for row in rows)

    def _source_result_record(
        self,
        stat: Mapping[str, Any],
        source_map: Mapping[str, Mapping[str, Any]],
        zoo_map: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        source_id = _safe_text(stat.get("source_id"))
        zoo_id = _safe_text(stat.get("zoo_id"))
        source = source_map.get(source_id or "", {})
        effective_zoo_id = zoo_id or _safe_text(source.get("zoo_id"))
        zoo = zoo_map.get(effective_zoo_id or "", {})
        record = {
            "id": stat.get("id"),
            "run_id": stat.get("run_id"),
            "zoo_id": effective_zoo_id,
            "zoo_name": zoo.get("name") or None,
            "source_id": source_id,
            "source_name": source.get("name") or source_id,
            "source_kind": source.get("kind"),
            "status": stat.get("status") or "unknown",
            "discovered": _count(stat.get("discovered")),
            "fetched": _count(stat.get("fetched")),
            "stored": _count(stat.get("stored")),
            "already_known": _count(stat.get("already_known")),
            "already_known_count": _count(stat.get("already_known")),
            "duplicate": _count(stat.get("duplicate")),
            "duplicate_candidate_count": _count(stat.get("duplicate")),
            "errors": _count(stat.get("errors")),
            "error_count": _count(stat.get("errors")),
            "duration_ms": stat.get("duration_ms"),
            "started_at": stat.get("started_at"),
            "finished_at": stat.get("finished_at"),
            "error": stat.get("error"),
            "http_status": stat.get("http_status")
            if stat.get("http_status") is not None
            else source.get("last_http_status"),
            "last_http_status": source.get("last_http_status"),
            "error_category": stat.get("error_category"),
            # ``last_error`` is source-health history, not evidence that this
            # run failed.  Keep it as its own audit field and classify from
            # the run-scoped stat fields only.
            "error_summary": stat.get("error_summary") or stat.get("error"),
            "last_success": source.get("last_success"),
            "last_error": source.get("last_error"),
            "enabled": source.get("enabled"),
            "source_status": source.get("status"),
            "source_url": stat.get("source_url") or source.get("url"),
            "errors_list": list(stat.get("errors_list") or []),
            "stop_reason": stat.get("stop_reason"),
            "metadata": parse_metadata(stat.get("metadata")),
        }
        raw_status = _safe_text(stat.get("status")) or "unknown"
        return with_status_fields(
            {**record, "raw_status": raw_status},
            terminal_status=raw_status if _status_token(raw_status) in _TERMINAL_STATUSES else None,
        )

    def list_run_source_results(
        self, run_id: str, zoo_id: Optional[str] = None, source_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        stats = self._stats_rows(str(run_id))
        source_map = self._source_map()
        zoo_map = self._zoo_map()
        rows = [self._source_result_record(stat, source_map, zoo_map) for stat in stats]
        if zoo_id is not None:
            zoo = self.get_zoo(zoo_id)
            wanted = str(zoo.get("id") if zoo else zoo_id)
            rows = [row for row in rows if row.get("zoo_id") == wanted]
        if source_id is not None:
            rows = [row for row in rows if row.get("source_id") == str(source_id)]
        rows.sort(
            key=lambda row: (
                str(row.get("zoo_name") or row.get("zoo_id") or "").casefold(),
                str(row.get("source_name") or row.get("source_id") or "").casefold(),
                str(row.get("id") or ""),
            )
        )
        return rows

    get_run_source_results = list_run_source_results
    get_run_zoo_source_results = list_run_source_results

    def _zoo_result_record(
        self,
        base: Mapping[str, Any],
        stats: Sequence[Mapping[str, Any]],
        zoo_map: Mapping[str, Mapping[str, Any]],
        source_results: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        zoo_id = _safe_text(base.get("zoo_id"))
        zoo = zoo_map.get(zoo_id or "", {})
        metadata = parse_metadata(base.get("metadata"))
        if not metadata and stats:
            metadata = parse_metadata(stats[0].get("metadata"))
        status = _safe_text(base.get("status"))
        if not status and stats:
            statuses = {str(item.get("status") or "unknown") for item in stats}
            status = ", ".join(sorted(statuses))
        stop_reason = _safe_text(base.get("stop_reason"))
        if not stop_reason:
            stop_reason = _stop_reason(metadata, *(stats[:1] or [{}]))
        raw_status = status or "unknown"
        record = {
            "id": base.get("id"),
            "run_id": base.get("run_id"),
            "zoo_id": zoo_id,
            "zoo_slug": base.get("zoo_slug") or zoo.get("slug"),
            "zoo_name": base.get("zoo_name") or zoo.get("name") or zoo_id,
            "status": status or "unknown",
            "source_status": base.get("source_status"),
            "discovered": _count(base.get("discovered")) if base.get("from_result") else self._metric(stats, "discovered"),
            "fetched": _count(base.get("fetched")) if base.get("from_result") else self._metric(stats, "fetched"),
            "parsed": _count(base.get("fetched")) if base.get("from_result") else self._metric(stats, "fetched"),
            "stored": _count(base.get("stored")) if base.get("from_result") else self._metric(stats, "stored"),
            "inserted": _count(base.get("stored")) if base.get("from_result") else self._metric(stats, "stored"),
            "updated": _count(base.get("updated")) if base.get("from_result") else 0,
            "already_known": self._metric(stats, "already_known"),
            "duplicate": _count(base.get("duplicate")) if base.get("from_result") else self._metric(stats, "duplicate"),
            "errors": _count(base.get("errors")) if base.get("from_result") else self._metric(stats, "errors"),
            "failed": _count(base.get("errors")) if base.get("from_result") else self._metric(stats, "errors"),
            "duplicate_filtered": _count(base.get("duplicate")) if base.get("from_result") else self._metric(stats, "duplicate"),
            "duration_ms": base.get("duration_ms") or _duration_ms(base.get("started_at"), base.get("finished_at")),
            "source_url": base.get("source_url"),
            "http_status": base.get("http_status"),
            "error_category": base.get("error_category"),
            "error_summary": base.get("error_summary") or next((item.get("error") for item in stats if item.get("error")), None),
            "started_at": base.get("started_at") or (stats[0].get("started_at") if stats else None),
            "finished_at": base.get("finished_at") or (stats[-1].get("finished_at") if stats else None),
            "stop_reason": stop_reason,
            "metadata": metadata,
            "sources": [dict(item) for item in source_results],
            "source_results": [dict(item) for item in source_results],
        }
        return with_status_fields(
            {**record, "raw_status": raw_status},
            failure_records=[*stats, *source_results],
            terminal_status=raw_status
            if _status_token(raw_status) in _TERMINAL_STATUSES
            or record.get("finished_at") not in (None, "")
            else None,
        )

    def list_run_zoo_results(self, run_id: str) -> List[Dict[str, Any]]:
        stats = self._stats_rows(str(run_id))
        persisted = self._zoo_result_rows(str(run_id))
        source_results = self.list_run_source_results(str(run_id))
        stats_by_zoo: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for stat in stats:
            stats_by_zoo[str(stat.get("zoo_id") or "")].append(stat)
        persisted_by_zoo: Dict[str, Dict[str, Any]] = {
            str(row.get("zoo_id") or ""): row for row in persisted
        }
        keys = set(stats_by_zoo) | set(persisted_by_zoo)
        zoo_map = self._zoo_map()
        rows: List[Dict[str, Any]] = []
        for key in keys:
            base = dict(persisted_by_zoo.get(key) or {})
            if base:
                base["from_result"] = True
            else:
                zoo_stats = stats_by_zoo.get(key, [])
                base = {
                    "id": None,
                    "run_id": str(run_id),
                    "zoo_id": key or (zoo_stats[0].get("zoo_id") if zoo_stats else None),
                    "status": None,
                    "from_result": False,
                }
            flat_sources = [row for row in source_results if row.get("zoo_id") == (key or None)]
            rows.append(self._zoo_result_record(base, stats_by_zoo.get(key, []), zoo_map, flat_sources))
        rows.sort(key=lambda row: (str(row.get("zoo_name") or "").casefold(), str(row.get("zoo_id") or "")))
        return rows

    get_run_zoo_results = list_run_zoo_results
    list_run_zoos = list_run_zoo_results

    def get_run_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return None
        stats = self._stats_rows(str(run_id))
        zoo_results = self.list_run_zoo_results(str(run_id))
        use_stats = bool(stats)
        metric_rows: Sequence[Mapping[str, Any]] = stats if use_stats else zoo_results
        metrics = {
            "discovered": self._metric(metric_rows, "discovered"),
            "fetched": self._metric(metric_rows, "fetched"),
            "stored": self._metric(metric_rows, "stored"),
            "already_known": self._metric(metric_rows, "already_known"),
            "duplicate": self._metric(metric_rows, "duplicate"),
            "errors": self._metric(metric_rows, "errors"),
        }
        zoo_ids = {str(row.get("zoo_id")) for row in metric_rows if row.get("zoo_id") not in (None, "")}
        source_ids = {str(row.get("source_id")) for row in stats if row.get("source_id") not in (None, "")}
        completed_statuses = {"completed", "complete", "finished", "success", "succeeded", "ok"}
        zoo_statuses: Dict[str, set[str]] = defaultdict(set)
        for row in metric_rows:
            if row.get("zoo_id") is not None:
                zoo_statuses[str(row.get("zoo_id"))].add(str(row.get("status") or "unknown").casefold())
        zoo_completed = sum(
            1
            for statuses in zoo_statuses.values()
            if statuses and all(status in completed_statuses for status in statuses)
        )
        source_completed = sum(
            1
            for row in stats
            if str(row.get("status") or "").casefold() in completed_statuses
        )
        metadata = parse_metadata(run.get("metadata"))
        configured_zoos = _safe_int(
            _first(metadata, "enabled_zoos_count", "total_zoos", "zoos_total", "zoos")
        )
        configured_sources = _safe_int(
            _first(metadata, "enabled_sources_count", "total_sources", "sources_total", "sources")
        )
        duration = run.get("duration_ms") or _duration_ms(run.get("started_at"), run.get("finished_at"))
        summary = {
            **run,
            "duration_ms": duration,
            "elapsed_ms": duration,
            "metrics": metrics,
            "summary": dict(metrics),
            **metrics,
            "zoos_total": configured_zoos if configured_zoos is not None else len(zoo_ids),
            "zoos_completed": zoo_completed,
            "sources_total": configured_sources if configured_sources is not None else len(source_ids),
            "sources_completed": source_completed,
            "zoo_count": configured_zoos if configured_zoos is not None else len(zoo_ids),
            "source_count": configured_sources if configured_sources is not None else len(source_ids),
            "stop_reason": run.get("stop_reason") or next(
                (
                    row.get("stop_reason")
                    for row in [*stats, *zoo_results]
                    if row.get("stop_reason")
                ),
                None,
            ),
        }
        return summary

    get_run_detail = get_run_summary

    def _article_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "article_id"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "zoo_id": ("zoo_id", "zoo"),
            "title": ("title", "headline", "name"),
            "canonical_url": ("canonical_url", "url"),
            "normalized_url": ("normalized_url",),
            "source_url": ("source_url", "original_url", "url"),
            "published_at": ("published_at", "publication_date", "date"),
            "updated_at_source": ("updated_at_source", "updated_at", "source_updated_at"),
            "author": ("author",),
            "summary": ("summary", "description", "excerpt"),
            "content": ("content", "body", "text"),
            "image_url": ("image_url", "image", "thumbnail_url"),
            "image_count_raw": ("image_count", "images_count"),
            "language": ("language", "lang"),
            "http_status": ("http_status", "status_code"),
            "crawl_status": ("crawl_status", "status", "state"),
            "parse_status": ("parse_status", "parser_status"),
            "last_fetched_at": ("last_fetched_at", "fetched_at", "retrieved_at"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
        }

    def _article_rows(self) -> List[Dict[str, Any]]:
        rows = self._select_projection("articles", self._article_projection())
        result: List[Dict[str, Any]] = []
        for row in rows:
            article_id = _safe_text(_first(row, "id"))
            result.append(
                {
                    "id": article_id,
                    "article_id": article_id,
                    "run_id": _safe_text(_first(row, "run_id")),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "title": _safe_text(_first(row, "title")) or "Untitled article",
                    "canonical_url": _safe_text(_first(row, "canonical_url")),
                    "normalized_url": _safe_text(_first(row, "normalized_url")),
                    "source_url": _safe_text(_first(row, "source_url")),
                    "published_at": _safe_value(_first(row, "published_at")),
                    "updated_at_source": _safe_value(_first(row, "updated_at_source")),
                    "author": _safe_text(_first(row, "author")),
                    "summary": _safe_text(_first(row, "summary")),
                    "content": _safe_text(_first(row, "content")),
                    "image_url": _safe_text(_first(row, "image_url")),
                    "image_count": _safe_int(_first(row, "image_count_raw")),
                    "language": _safe_text(_first(row, "language")),
                    "http_status": _safe_int(_first(row, "http_status")),
                    "crawl_status": _safe_text(_first(row, "crawl_status")),
                    "parse_status": _safe_text(_first(row, "parse_status")),
                    "last_fetched_at": _safe_value(_first(row, "last_fetched_at")),
                    "metadata": parse_metadata(_first(row, "metadata_raw")),
                }
            )
        return result

    def _discovery_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "discovery_id"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "article_id": ("article_id", "id_article"),
            "source_id": ("source_id", "source"),
            "discovered_at": ("discovered_at", "created_at", "found_at"),
            "last_discovered_at": ("last_discovered_at", "last_seen_at", "discovered_at"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
        }

    def _discovery_rows(self) -> List[Dict[str, Any]]:
        rows = self._select_projection("discoveries", self._discovery_projection())
        result: List[Dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": _safe_text(_first(row, "id")),
                    "run_id": _safe_text(_first(row, "run_id")),
                    "article_id": _safe_text(_first(row, "article_id")),
                    "source_id": _safe_text(_first(row, "source_id")),
                    "discovered_at": _safe_value(_first(row, "discovered_at")),
                    "last_discovered_at": _safe_value(_first(row, "last_discovered_at")),
                    "metadata": parse_metadata(_first(row, "metadata_raw")),
                }
            )
        return result

    def _run_article_relation_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "article_id": ("article_id", "id_article"),
            "zoo_id": ("zoo_id", "zoo"),
            "source_id": ("source_id", "source"),
            "outcome": ("outcome", "result", "article_outcome"),
            "created_at": ("created_at", "linked_at", "stored_at"),
            "metadata_raw": ("metadata_json", "metadata", "meta_json"),
        }

    def _run_article_relation_rows(
        self, run_id: str
    ) -> Tuple[List[Dict[str, Any]], bool, str]:
        """Return explicit run/article links and their observability state.

        A timestamp overlap is deliberately not a relationship.  The current
        schema predates run/article provenance, so an old database must say
        that the article view is unavailable instead of presenting a plausible
        but incorrect list.
        """

        table = self._table("run_articles")
        if table is not None:
            run_column = self._column(table, "crawl_run_id", "run_id", "crawl_id")
            article_column = self._column(table, "article_id", "id_article")
            if not run_column or not article_column:
                return [], False, "run_article_relation_missing_columns"
            if self._table("articles") is None:
                return [], False, "articles_table_unavailable"
            rows = self._select_projection(
                "run_articles",
                self._run_article_relation_projection(),
                where=_quote_identifier(run_column) + " = ?",
                args=(str(run_id),),
            )
            return [
                {
                    "run_id": _safe_text(_first(row, "run_id")) or str(run_id),
                    "article_id": _safe_text(_first(row, "article_id")),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "source_id": _safe_text(_first(row, "source_id")),
                    "outcome": _safe_text(_first(row, "outcome")) or "stored",
                    "created_at": _safe_value(_first(row, "created_at")),
                    "metadata": parse_metadata(_first(row, "metadata_raw")),
                }
                for row in rows
                if _first(row, "article_id") not in (None, "")
            ], True, "crawl_run_articles"

        # A direct run id on articles is also explicit provenance.  It is a
        # compatibility path for small hand-written/experimental schemas, not
        # a time-based fallback.
        article_table = self._table("articles")
        direct_column = (
            self._column(article_table, "crawl_run_id", "run_id", "crawl_id")
            if article_table
            else None
        )
        if direct_column:
            rows = self._select_projection(
                "articles",
                self._article_projection(),
                where=_quote_identifier(direct_column) + " = ?",
                args=(str(run_id),),
            )
            return [
                {
                    "run_id": _safe_text(_first(row, "run_id")) or str(run_id),
                    "article_id": _safe_text(_first(row, "id")),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "source_id": None,
                    "outcome": "stored",
                    "created_at": None,
                    "metadata": {},
                }
                for row in rows
                if _first(row, "id") not in (None, "")
            ], True, "articles.run_id"
        return [], False, "run_article_relation_unavailable"

    def get_run_articles_observability(self, run_id: str) -> Dict[str, Any]:
        if self.get_run(run_id) is None:
            return {
                "observable": False,
                "reason": "run_not_found",
                "relation": None,
            }
        _rows, observable, relation = self._run_article_relation_rows(str(run_id))
        return {
            "observable": observable,
            "reason": None if observable else relation,
            "relation": relation,
        }

    def _run_article_candidates(
        self,
        run_id: str,
        *,
        stored_only: bool = False,
        search: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        run = self.get_run(run_id)
        if run is None:
            return []
        relation_rows, observable, _relation = self._run_article_relation_rows(str(run_id))
        if not observable:
            return []
        article_ids = {str(row.get("article_id")) for row in relation_rows}
        relation_by_article: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for relation in relation_rows:
            relation_by_article[str(relation.get("article_id"))].append(relation)
        articles = [row for row in self._article_rows() if str(row.get("id")) in article_ids]
        discoveries_by_article: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for discovery in self._discovery_rows():
            discovery_run_id = discovery.get("run_id")
            if discovery.get("article_id") is not None and discovery_run_id in (None, "", str(run_id)):
                discoveries_by_article[str(discovery["article_id"])].append(discovery)
        sources = self._source_map()
        zoos = self._zoo_map()
        identities = self._select_projection(
            "article_identities",
            {"article_id": ("article_id",), "zoo_id": ("zoo_id",)},
        )
        identity_zoos: Dict[str, set[str]] = defaultdict(set)
        for identity in identities:
            if identity.get("article_id") is not None and identity.get("zoo_id") is not None:
                identity_zoos[str(identity["article_id"])].add(str(identity["zoo_id"]))

        result: List[Dict[str, Any]] = []
        for article in articles:
            article_id = str(article.get("id") or "")
            relations = relation_by_article.get(article_id, [])
            relation_outcomes = {
                str(item.get("outcome") or "stored").casefold()
                for item in relations
            }
            if outcome not in (None, "") and str(outcome).casefold() not in relation_outcomes:
                continue
            stored_relations = [
                item
                for item in relations
                if str(item.get("outcome") or "stored").casefold()
                in {"stored", "inserted", "created", "new"}
            ]
            if stored_only and not stored_relations:
                continue
            # Article Preview is explicitly the stored outcome for this run.
            # Use that relation subset for source/zoo provenance so a later
            # duplicate outcome cannot replace the source shown to operators.
            display_relations = stored_relations if stored_only else relations
            if search not in (None, ""):
                needle = str(search).casefold()
                haystack = " ".join(
                    str(article.get(name) or "")
                    for name in ("title", "canonical_url", "source_url", "summary", "content")
                ).casefold()
                if needle not in haystack:
                    continue
            related = discoveries_by_article.get(article_id, [])
            source_ids_for_article = {
                str(item.get("source_id"))
                for item in display_relations
                if item.get("source_id") not in (None, "")
            }
            if not source_ids_for_article and display_relations:
                source_ids_for_article.update(
                    str(item.get("source_id"))
                    for item in related
                    if item.get("source_id") not in (None, "")
                )
            source_rows = [sources[item] for item in source_ids_for_article if item in sources]
            zoo_ids_for_article = {
                str(item.get("zoo_id"))
                for item in display_relations
                if item.get("zoo_id") not in (None, "")
            }
            zoo_ids_for_article.update(identity_zoos.get(article_id, set()))
            if article.get("zoo_id"):
                zoo_ids_for_article.add(str(article["zoo_id"]))
            zoo_ids_for_article.update(
                str(source.get("zoo_id")) for source in source_rows if source.get("zoo_id")
            )
            zoo_rows = [zoos[item] for item in zoo_ids_for_article if item in zoos]
            timestamps = [
                item.get("discovered_at")
                for item in related
                if _timestamp_score(item.get("discovered_at")) != float("-inf")
            ]
            timestamps += [
                item.get("created_at")
                for item in display_relations
                if _timestamp_score(item.get("created_at")) != float("-inf")
            ]
            first_discovered = min(timestamps, key=_timestamp_score, default=None)
            metadata_value = parse_metadata(article.get("metadata"))
            images = metadata_value.get("images")
            image_count = article.get("image_count")
            if image_count is None:
                image_count = _safe_int(metadata_value.get("image_count"))
            if image_count is None and isinstance(images, list):
                image_count = len(images)
            if image_count is None:
                image_count = 1 if article.get("image_url") else 0
            source_values = [
                {
                    "id": source.get("id"),
                    "name": source.get("name"),
                    "kind": source.get("kind"),
                    "url": source.get("url"),
                    "zoo_id": source.get("zoo_id"),
                }
                for source in source_rows
            ]
            zoo_values = [dict(row) for row in zoo_rows]
            result.append(
                {
                    "id": article_id,
                    "article_id": article_id,
                    "title": article.get("title") or "Untitled article",
                    "canonical_url": article.get("canonical_url"),
                    "normalized_url": article.get("normalized_url"),
                    "source_url": article.get("source_url"),
                    "published_at": article.get("published_at"),
                    "updated_at_source": article.get("updated_at_source"),
                    "fetched_at": article.get("last_fetched_at"),
                    "last_fetched_at": article.get("last_fetched_at"),
                    "language": article.get("language"),
                    "author": article.get("author"),
                    "summary": article.get("summary"),
                    "content": article.get("content"),
                    "image_url": article.get("image_url"),
                    "image_count": image_count,
                    "http_status": article.get("http_status"),
                    "crawl_status": article.get("crawl_status"),
                    "parse_status": article.get("parse_status"),
                    "outcome": (
                        "stored"
                        if stored_relations
                        else next(
                            (item.get("outcome") for item in display_relations if item.get("outcome")),
                            "stored",
                        )
                    ),
                    "first_discovered_at": first_discovered,
                    "zoo_id": next((row.get("id") for row in zoo_rows), article.get("zoo_id")),
                    "zoo_name": next((row.get("name") for row in zoo_rows), None),
                    "zoos": zoo_values,
                    "zoo": zoo_values[0] if zoo_values else None,
                    "sources": source_values,
                    "source": source_values[0] if source_values else None,
                    "source_name": source_values[0].get("name") if source_values else None,
                    "metadata": metadata_value,
                }
            )
        result.sort(
            key=lambda row: (
                max(
                    _timestamp_score(row.get("published_at")),
                    _timestamp_score(row.get("first_discovered_at")),
                    _timestamp_score(row.get("fetched_at")),
                ),
                str(row.get("id") or ""),
            ),
            reverse=True,
        )
        return result

    def list_run_articles(
        self,
        run_id: str,
        limit: Any = None,
        offset: Any = 0,
        *,
        stored_only: bool = False,
        search: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = self._run_article_candidates(
            str(run_id), stored_only=stored_only, search=search, outcome=outcome
        )
        return rows if limit is None else _page_slice(rows, limit, offset)

    get_run_articles = list_run_articles

    def count_run_articles(
        self,
        run_id: str,
        *,
        stored_only: bool = False,
        search: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> int:
        return len(
            self._run_article_candidates(
                str(run_id), stored_only=stored_only, search=search, outcome=outcome
            )
        )

    def _event_projection(self) -> Dict[str, Tuple[str, ...]]:
        return {
            "id": ("id", "event_id"),
            "run_id": ("crawl_run_id", "run_id", "crawl_id"),
            "zoo_id": ("zoo_id", "zoo"),
            "source_id": ("source_id", "source"),
            "created_at": ("created_at", "occurred_at", "timestamp", "event_at"),
            "level": ("level", "severity", "log_level"),
            "component": ("component", "module", "area"),
            "event_type": ("event_type", "type", "kind", "event_name"),
            "message": ("message", "text", "description", "error"),
            "metadata_raw": ("metadata_json", "metadata", "data_json", "details_json"),
        }

    def _event_rows(self, run_id: str) -> List[Dict[str, Any]]:
        table = self._table("events")
        if table is None:
            return []
        run_column = self._column(table, "crawl_run_id", "run_id", "crawl_id")
        if not run_column:
            return []
        rows = self._select_projection(
            "events",
            self._event_projection(),
            where=_quote_identifier(run_column) + " = ?",
            args=(str(run_id),),
        )
        result: List[Dict[str, Any]] = []
        for row in rows:
            raw_event_id = _first(row, "id")
            event_id: Union[int, str, None] = _safe_int(raw_event_id)
            if event_id is None:
                event_id = _safe_text(raw_event_id)
            result.append(
                {
                    "id": event_id,
                    "event_id": event_id,
                    "run_id": _safe_text(_first(row, "run_id")) or str(run_id),
                    "zoo_id": _safe_text(_first(row, "zoo_id")),
                    "source_id": _safe_text(_first(row, "source_id")),
                    "created_at": _safe_value(_first(row, "created_at")),
                    "level": _safe_text(_first(row, "level")) or "INFO",
                    "component": _safe_text(_first(row, "component")),
                    "event_type": _safe_text(_first(row, "event_type")),
                    "message": _safe_text(_first(row, "message")),
                    "metadata": parse_metadata(_first(row, "metadata_raw")),
                }
            )
        return result

    def _canonical_event_table(self) -> Optional[str]:
        table = self._table("events")
        if table != "crawl_run_events":
            return None
        required = {
            "id", "run_id", "zoo_id", "source_id", "created_at", "level",
            "component", "event_type", "message", "metadata_json",
        }
        info = self._column_info(table)
        if not required.issubset(info) or info.get("id") != "INTEGER":
            return None
        return table

    @staticmethod
    def _dimension_ids(
        requested: Optional[str],
        records: Mapping[str, Mapping[str, Any]],
        fields: Sequence[str],
    ) -> Optional[List[str]]:
        if requested in (None, ""):
            return None
        token = str(requested).casefold()
        matches = {str(requested)}
        for record_id, record in records.items():
            values = (record_id, *(record.get(field) for field in fields))
            if any(value is not None and str(value).casefold() == token for value in values):
                matches.add(str(record_id))
        return sorted(matches)

    @staticmethod
    def _canonical_event_record(
        row: Any,
        run_id: str,
        zoo_map: Mapping[str, Mapping[str, Any]],
        source_map: Mapping[str, Mapping[str, Any]],
    ) -> Dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            values = {str(key): row[key] for key in row.keys()}
        elif isinstance(row, Mapping):
            values = {str(key): value for key, value in row.items()}
        else:
            values = {}
        zoo_id = _safe_text(values.get("zoo_id"))
        source_id = _safe_text(values.get("source_id"))
        zoo_record = zoo_map.get(zoo_id or "", {})
        source_record = source_map.get(source_id or "", {})
        event_id = _safe_int(values.get("id"))
        return {
            "id": event_id,
            "event_id": event_id,
            "run_id": _safe_text(values.get("run_id")) or run_id,
            "zoo_id": zoo_id,
            "source_id": source_id,
            "created_at": _safe_value(values.get("created_at")),
            "level": _safe_text(values.get("level")) or "INFO",
            "component": _safe_text(values.get("component")),
            "event_type": _safe_text(values.get("event_type")),
            "message": _safe_text(values.get("message")),
            "metadata": parse_metadata(values.get("metadata_json")),
            "zoo_name": _safe_text(zoo_record.get("name")),
            "source_name": _safe_text(source_record.get("name")),
            "source_kind": _safe_text(source_record.get("kind")),
        }

    @staticmethod
    def _event_search_matches(row: Mapping[str, Any], search: str) -> bool:
        haystack = " ".join(
            str(row.get(name) or "")
            for name in (
                "message", "event_type", "component", "level", "zoo_id",
                "zoo_name", "source_id", "source_name",
            )
        ).casefold()
        try:
            haystack += " " + json.dumps(
                row.get("metadata") or {}, ensure_ascii=False
            ).casefold()
        except (TypeError, ValueError, RecursionError):
            pass
        return search in haystack

    def _query_canonical_events(
        self,
        run_id: str,
        *,
        level: Optional[str],
        requested_zoo: Optional[str],
        requested_source: Optional[str],
        component: Optional[str],
        event_type: Optional[str],
        text: Optional[str],
        after_id: Any,
        limit: Any,
        offset: Any,
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        table = self._canonical_event_table()
        after_value = _safe_int(after_id) if after_id not in (None, "") else None
        if (
            table is None
            or limit is None
            or (after_id not in (None, "") and after_value is None)
        ):
            return None
        zoo_map = self._zoo_map()
        source_map = self._source_map()
        zoo_ids = self._dimension_ids(requested_zoo, zoo_map, ("name", "slug"))
        source_ids = self._dimension_ids(
            requested_source, source_map, ("name", "kind")
        )
        clauses = ["run_id = ?"]
        args: List[Any] = [str(run_id)]
        if after_value is not None:
            clauses.append("id > ?")
            args.append(after_value)
        for column, value in (
            ("level", level), ("component", component), ("event_type", event_type)
        ):
            if value not in (None, ""):
                clauses.append(f"{column} = ? COLLATE NOCASE")
                args.append(str(value))
        for column, values in (("zoo_id", zoo_ids), ("source_id", source_ids)):
            if values is not None:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{column} COLLATE NOCASE IN ({placeholders})")
                args.extend(values)
        where = " WHERE " + " AND ".join(clauses)
        projection = (
            "id, run_id, zoo_id, source_id, created_at, level, component, "
            "event_type, message, metadata_json"
        )
        safe_limit, safe_offset = _page_values(limit, offset)
        search = str(text).casefold() if text not in (None, "") else None
        if search is None:
            null_projection = ", ".join("NULL" for _ in range(10))
            sql = (
                f"WITH filtered AS (SELECT {projection} "
                f"FROM {_quote_identifier(table)}{where}), "
                "page AS (SELECT * FROM filtered ORDER BY id ASC LIMIT ? OFFSET ?) "
                f"SELECT {projection}, (SELECT COUNT(*) FROM filtered) AS __total, "
                "0 AS __sentinel FROM page "
                f"UNION ALL SELECT {null_projection}, "
                "(SELECT COUNT(*) FROM filtered), 1 "
                "WHERE NOT EXISTS (SELECT 1 FROM page) "
                "ORDER BY __sentinel ASC, id ASC"
            )
            cursor = self._execute(
                sql,
                (*args, safe_limit, safe_offset),
            )
            result_rows = [
                _row_dict(row, cursor.description or ()) for row in self._fetchall(cursor)
            ]
            total = int(result_rows[0].get("__total") or 0) if result_rows else 0
            page_rows = [row for row in result_rows if not row.get("__sentinel")]
            return [
                self._canonical_event_record(row, str(run_id), zoo_map, source_map)
                for row in page_rows
            ], total

        # Unicode casefold, catalog names and parsed JSON metadata cannot be
        # expressed equivalently by SQLite LIKE.  Stream the already narrowed
        # ordered cursor and retain only the requested page while counting.
        cursor = self._execute(
            f"SELECT {projection} FROM {_quote_identifier(table)}{where} ORDER BY id ASC",
            args,
        )
        page: List[Dict[str, Any]] = []
        total = 0
        while True:
            chunk = self._fetchmany(cursor, 256)
            if not chunk:
                break
            for raw_row in chunk:
                row = self._canonical_event_record(
                    _row_dict(raw_row, cursor.description or ()),
                    str(run_id),
                    zoo_map,
                    source_map,
                )
                if not self._event_search_matches(row, search):
                    continue
                if total >= safe_offset and len(page) < safe_limit:
                    page.append(row)
                total += 1
        return page, total

    @staticmethod
    def _event_id_key(value: Any) -> Tuple[int, Union[int, str]]:
        text = "" if value is None else str(value)
        integer = _safe_int(text)
        if integer is not None:
            return (0, integer)
        return (1, text)

    @staticmethod
    def _matches_dimension(
        requested: Optional[str], value: Any, *extra: Any
    ) -> bool:
        if requested in (None, ""):
            return True
        candidate = str(requested).casefold()
        return any(value is not None and str(value).casefold() == candidate for value in (value, *extra))

    def query_run_events(
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
        after_id: Any = None,
        limit: Any = DEFAULT_PAGE_SIZE,
        offset: Any = 0,
    ) -> Tuple[List[Dict[str, Any]], int]:
        requested_zoo = zoo_id if zoo_id not in (None, "") else zoo
        requested_source = source_id if source_id not in (None, "") else source
        canonical = self._query_canonical_events(
            str(run_id),
            level=level,
            requested_zoo=requested_zoo,
            requested_source=requested_source,
            component=component,
            event_type=event_type,
            text=text,
            after_id=after_id,
            limit=limit,
            offset=offset,
        )
        if canonical is not None:
            return canonical
        rows = self._event_rows(str(run_id))
        source_map = self._source_map()
        zoo_map = self._zoo_map()
        search = str(text).casefold() if text not in (None, "") else None
        filtered: List[Dict[str, Any]] = []
        after_key = self._event_id_key(after_id) if after_id not in (None, "") else None
        for row in rows:
            zoo_record = zoo_map.get(str(row.get("zoo_id") or ""), {})
            source_record = source_map.get(str(row.get("source_id") or ""), {})
            row["zoo_name"] = _safe_text(zoo_record.get("name"))
            row["source_name"] = _safe_text(source_record.get("name"))
            row["source_kind"] = _safe_text(source_record.get("kind"))
            if not self._matches_dimension(level, row.get("level")):
                continue
            if not self._matches_dimension(
                requested_zoo,
                row.get("zoo_id"),
                zoo_record.get("name"),
                zoo_record.get("slug"),
            ):
                continue
            if not self._matches_dimension(
                requested_source,
                row.get("source_id"),
                source_record.get("name"),
                source_record.get("kind"),
            ):
                continue
            if not self._matches_dimension(component, row.get("component")):
                continue
            if not self._matches_dimension(event_type, row.get("event_type")):
                continue
            if search is not None:
                if not self._event_search_matches(row, search):
                    continue
            if after_key is not None and self._event_id_key(row.get("id")) <= after_key:
                continue
            filtered.append(row)
        filtered.sort(key=lambda row: self._event_id_key(row.get("id")))
        total = len(filtered)
        return (
            filtered if limit is None else _page_slice(filtered, limit, offset),
            total,
        )

    def list_run_events(self, run_id: str, **filters: Any) -> List[Dict[str, Any]]:
        rows, _ = self.query_run_events(str(run_id), **filters)
        return rows

    get_run_events = list_run_events

    def count_run_events(self, run_id: str, **filters: Any) -> int:
        _, total = self.query_run_events(str(run_id), **filters)
        return total

    def _zoo_history_rows(self, zoo_id: str) -> List[Dict[str, Any]]:
        zoo = self.get_zoo(zoo_id)
        # When the zoo catalog exists, an unknown zoo must not surface orphan
        # stats as if they belonged to a real configured zoo.  Legacy DBs may
        # omit the catalog entirely; in that case the explicit zoo_id in
        # stats/results is still useful history evidence.
        if zoo is None and self._table("zoos") is not None:
            return []
        wanted = str(zoo.get("id") if zoo else zoo_id)
        persisted = [row for row in self._zoo_result_rows() if row.get("zoo_id") == wanted]
        stats = [row for row in self._stats_rows() if row.get("zoo_id") == wanted]
        runs = {str(row.get("id")): row for row in self._run_rows() if row.get("id") is not None}
        by_run_stats: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in stats:
            by_run_stats[str(row.get("run_id") or "")].append(row)
        by_run_result: Dict[str, Dict[str, Any]] = {
            str(row.get("run_id") or ""): row for row in persisted
        }
        keys = set(by_run_stats) | set(by_run_result)
        zoo_map = self._zoo_map()
        source_map = self._source_map()
        result: List[Dict[str, Any]] = []
        for run_key in keys:
            stat_rows = by_run_stats.get(run_key, [])
            base = dict(by_run_result.get(run_key) or {})
            if base:
                metrics = base
                from_result = True
            else:
                metrics = {
                    "discovered": self._metric(stat_rows, "discovered"),
                    "fetched": self._metric(stat_rows, "fetched"),
                    "stored": self._metric(stat_rows, "stored"),
                    "already_known": self._metric(stat_rows, "already_known"),
                    "duplicate": self._metric(stat_rows, "duplicate"),
                    "errors": self._metric(stat_rows, "errors"),
                    "status": ", ".join(sorted({str(row.get("status") or "unknown") for row in stat_rows})) or "unknown",
                    "started_at": stat_rows[0].get("started_at") if stat_rows else None,
                    "finished_at": stat_rows[-1].get("finished_at") if stat_rows else None,
                    "duration_ms": None,
                }
                from_result = False
            run = runs.get(run_key, {})
            metadata = parse_metadata(run.get("metadata"))
            source_results = [
                self._source_result_record(stat, source_map, zoo_map)
                for stat in stat_rows
            ]
            duration = run.get("duration_ms") or metrics.get("duration_ms") or _duration_ms(
                run.get("started_at") or metrics.get("started_at"),
                run.get("finished_at") or metrics.get("finished_at"),
            )
            raw_status = metrics.get("status") or run.get("status") or "unknown"
            history_record = {
                    "run_id": run_key or run.get("id"),
                    "status": raw_status,
                    "run_status": run.get("status") or "unknown",
                    "run_started_at": run.get("started_at") or metrics.get("started_at"),
                    "run_finished_at": run.get("finished_at") or metrics.get("finished_at"),
                    "duration_ms": duration,
                    "zoo_id": wanted,
                    "zoo_name": zoo_map.get(wanted, {}).get("name"),
                    "discovered": _count(metrics.get("discovered")),
                    "fetched": _count(metrics.get("fetched")),
                    "stored": _count(metrics.get("stored")),
                    "already_known": self._metric(stat_rows, "already_known"),
                    "duplicate": _count(metrics.get("duplicate")),
                    "errors": _count(metrics.get("errors")),
                    "stop_reason": _stop_reason(metadata, metrics, *(stat_rows[:1] or [{}])),
                    "error": run.get("error") or metrics.get("error_summary"),
                    "metadata": parse_metadata(metrics.get("metadata")),
                    "sources": source_results,
                    "from_zoo_result": from_result,
                }
            result.append(
                with_status_fields(
                    {**history_record, "raw_status": raw_status},
                    failure_records=[run, *stat_rows, *source_results],
                    terminal_status=raw_status
                    if _status_token(raw_status) in _TERMINAL_STATUSES
                    or history_record.get("run_finished_at") not in (None, "")
                    else None,
                )
            )
        result.sort(
            key=lambda row: (
                _timestamp_score(row.get("run_started_at") or row.get("run_finished_at")),
                str(row.get("run_id") or ""),
            ),
            reverse=True,
        )
        return result

    def list_zoo_history(self, zoo_id: str, limit: Any = None, offset: Any = 0) -> List[Dict[str, Any]]:
        rows = self._zoo_history_rows(str(zoo_id))
        return rows if limit is None else _page_slice(rows, limit, offset)

    get_zoo_history = list_zoo_history
    list_zoo_crawl_history = list_zoo_history

    def count_zoo_history(self, zoo_id: str) -> int:
        return len(self._zoo_history_rows(str(zoo_id)))

    def get_zoo_detail(self, zoo_id: str) -> Optional[Dict[str, Any]]:
        zoo = self.get_zoo(zoo_id)
        if zoo is None:
            return None
        sources = self.list_sources(str(zoo.get("id")))
        history = self._zoo_history_rows(str(zoo.get("id")))
        return {
            **zoo,
            "sources": sources,
            "source_count": len(sources),
            "enabled_source_count": sum(1 for source in sources if source.get("enabled")),
            "history": history,
            "last_run": history[0] if history else None,
        }


__all__ = [
    "ConsoleRepository",
    "ConsoleRepositoryError",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "parse_metadata",
    "safe_metadata",
]
