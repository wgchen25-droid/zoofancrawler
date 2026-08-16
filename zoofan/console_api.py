"""Read-only HTTP and HTML boundary for the crawler operations console.

The console deliberately depends on a small service contract rather than on
SQLite.  ``zoofan.console_service`` is the intended provider of that contract
and its read DTOs.  The module is optional while the repository/service layer
is being assembled: in that case a null, empty read model keeps the existing
dashboard usable and makes the UI render its normal empty state.

The route layer is responsible for HTTP concerns only: validating query
parameters, mapping missing records to 404, serialising DTOs, and selecting
templates.  It does not build SQL, derive crawler statistics, or persist
events.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
import math
import re
from datetime import date, datetime
from types import ModuleType
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, jsonify, render_template, request


logger = logging.getLogger(__name__)


class ConsoleServiceProtocol(Protocol):
    """The read-only service surface consumed by this blueprint.

    Concrete DTOs intentionally live in ``console_repository`` /
    ``console_service``.  Return values may be dataclasses, mappings, or
    collection/page DTOs exposing ``items``.  The API adapter keeps those
    details out of route handlers.
    """

    def get_current_status(self) -> Any: ...

    def list_runs(self, **filters: Any) -> Any: ...

    def get_run_summary(self, run_id: str) -> Any: ...

    def get_run_zoo_results(self, run_id: str, **filters: Any) -> Any: ...

    def get_run_source_results(self, run_id: str, **filters: Any) -> Any: ...

    def list_run_articles(self, run_id: str, **filters: Any) -> Any: ...

    def list_run_events(self, run_id: str, **filters: Any) -> Any: ...

    def get_zoos(self, **filters: Any) -> Any: ...

    def get_zoo(self, zoo_id: str) -> Any: ...

    def get_zoo_history(self, zoo_id: str, **filters: Any) -> Any: ...


class ConsoleNotFoundError(LookupError):
    """Optional service-layer exception for a missing run or zoo."""


class ConsoleValidationError(ValueError):
    """Optional service-layer exception for invalid read filters."""


class ConsoleServiceUnavailable(RuntimeError):
    """Raised internally when an injected service lacks a required method."""


_MISSING = object()
_DROP = object()

_SERVICE_METHODS: dict[str, tuple[str, ...]] = {
    "status": (
        "get_crawler_status",
        "get_current_status",
        "get_status",
        "crawler_status",
        "status",
    ),
    "runs": ("list_crawl_runs", "list_runs", "get_crawl_runs", "crawl_runs"),
    "run": (
        "get_crawl_run",
        "get_run_summary",
        "get_run_detail",
        "get_run",
        "crawl_run",
    ),
    "run_zoos": (
        "get_run_zoo_results",
        "list_run_zoo_results",
        "list_run_zoos",
        "get_run_zoos",
        "list_crawl_run_zoos",
        "get_crawl_run_zoos",
    ),
    "run_sources": (
        "get_run_source_results",
        "list_run_source_results",
        "get_run_zoo_source_results",
        "list_run_zoo_source_results",
        "list_crawl_run_sources",
        "get_crawl_run_sources",
    ),
    "run_articles": (
        "list_run_articles",
        "get_run_articles",
        "list_crawl_run_articles",
        "get_crawl_run_articles",
    ),
    "run_events": (
        "list_run_events",
        "get_run_events",
        "list_crawl_run_events",
        "get_crawl_run_events",
    ),
    "zoos": ("list_zoos", "get_zoos", "zoos"),
    "zoo": ("get_zoo", "zoo"),
    "zoo_history": (
        "get_zoo_history",
        "list_zoo_history",
        "list_zoo_crawl_history",
        "get_zoo_crawl_history",
        "list_crawl_history",
        "zoo_crawl_history",
    ),
}

_URL_KEYS = {
    "url",
    "href",
    "link",
    "canonical_url",
    "source_url",
    "website_url",
    "article_url",
}
_SENSITIVE_KEYS = {
    "raw_html",
    "raw_html_json",
    "raw_html_text",
    "rawhtml",
    "rawhtmljson",
    "rawhtmltext",
}
_MISSING_SERVICE_MESSAGE = "Crawler console service is not configured"

_STATUS_LABELS = {
    "running": "Running", "stale": "Stale", "unknown": "Unknown",
    "success": "Success", "warning": "Warning", "failed": "Failed",
    "budget_hit": "Budget Hit",
}
_STATUS_ALIASES = {
    "finished": "success", "completed": "success", "complete": "success",
    "success": "success", "succeeded": "success", "ok": "success", "done": "success",
    "completed_with_errors": "warning", "partial": "warning", "warning": "warning",
    "failed": "failed", "failure": "failed", "error": "failed", "aborted": "failed",
    "cancelled": "failed", "canceled": "failed", "stopped": "failed",
    "running": "running", "active": "running", "in_progress": "running",
    "stale": "stale", "unknown": "unknown", "budget_hit": "budget_hit",
}
_BUDGET_REASONS = {
    "article_limit", "page_limit", "runtime_limit", "url_discovery_limit",
    "candidate_limit", "batch_limit", "max_candidates_per_source", "max_pages",
    "max_sitemaps", "archive_page_limit", "sitemap_page_limit", "no_new_urls",
}


def _project_status_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project legacy run/status DTOs onto the stable transport contract."""

    result = dict(value)
    # Source and event statuses are evidence, not run classifications.
    if "source_id" in result or "event_type" in result:
        return result
    fields = {
        "status", "run_status", "raw_status", "classified_status", "status_key",
        "display_status", "terminal_status", "stale", "status_observable",
    }
    if not any(field in result for field in fields):
        return result

    def key(candidate: Any) -> Optional[str]:
        if candidate is None:
            return None
        token = str(candidate).strip().casefold().replace("-", "_").replace(" ", "_")
        return _STATUS_ALIASES.get(token)

    raw = result.get("raw_status") or result.get("run_status") or result.get("status") or "unknown"
    raw = str(raw)
    classified = (
        key(result.get("classified_status")) or key(result.get("status_key"))
        or key(result.get("display_status")) or key(result.get("status"))
        or key(result.get("run_status")) or "unknown"
    )
    reason = result.get("stop_reason")
    reason_token = str(reason).strip().casefold().replace("-", "_").replace(" ", "_") if reason else ""
    if reason_token in _BUDGET_REASONS or reason_token in {"budget", "budget_hit", "crawl_budget_hit"}:
        classified = "budget_hit"
    if classified == "running" and (
        result.get("stale") is True or result.get("status_observable") is False
        or result.get("active") is False or result.get("is_active") is False
    ):
        classified = "stale" if result.get("stale") is True else "unknown"
    terminal = result.get("terminal_status")
    if terminal is None and classified in {"success", "warning", "failed", "budget_hit"}:
        terminal = raw
    result.update({
        "status": classified,
        "classified_status": classified,
        "display_status": classified if result.get("display_status") in (None, "") else result["display_status"],
        "status_key": classified,
        "status_label": _STATUS_LABELS[classified] if result.get("status_label") in (None, "") else result["status_label"],
        "raw_status": raw,
        "run_status": result.get("run_status", raw),
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


class _EmptyConsoleService:
    """An intentionally boring read model used before the service is present."""

    def get_crawler_status(self) -> dict[str, Any]:
        return {
            "status": "unknown",
            "active": False,
            "is_active": False,
            "active_run": None,
            "current_run": None,
            "current_activity": None,
            "heartbeat_at": None,
            "progress_at": None,
            "current_phase": None,
            "last_updated": None,
            "poll_hint": {"enabled": False, "interval_seconds": None, "after_id": None},
            "poll_after_seconds": None,
        }

    def list_crawl_runs(self, **_filters: Any) -> list[Any]:
        return []

    def get_crawl_run(self, _run_id: str) -> None:
        return None

    def list_run_zoos(self, _run_id: str, **_filters: Any) -> list[Any]:
        return []

    def list_run_sources(self, _run_id: str, **_filters: Any) -> list[Any]:
        return []

    def list_run_articles(self, _run_id: str, **_filters: Any) -> list[Any]:
        return []

    def list_run_events(self, _run_id: str, **_filters: Any) -> list[Any]:
        return []

    def list_zoos(self, **_filters: Any) -> list[Any]:
        return []

    def get_zoo(self, _zoo_id: str) -> None:
        return None

    def list_zoo_crawl_history(self, _zoo_id: str, **_filters: Any) -> list[Any]:
        return []


def _safe_external_url(value: Any) -> Optional[str]:
    """Allow only absolute HTTP(S) URLs in JSON and HTML link attributes."""

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return text


def _serialise(value: Any, seen: Optional[set[int]] = None, key: str = "") -> Any:
    """Convert a read DTO to JSON-safe data without exposing raw HTML.

    DTOs remain owned by the repository/service layer.  This function is only
    a transport projection and recursively drops the one field that must
    never cross the console presentation boundary: ``raw_html``.
    """

    if seen is None:
        seen = set()
    if key.casefold() in _SENSITIVE_KEYS:
        return _DROP
    if value is None or isinstance(value, (str, int, bool)):
        if key.casefold() in _URL_KEYS or key.casefold().endswith("_url"):
            return _safe_external_url(value)
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if key.casefold() in _URL_KEYS or key.casefold().endswith("_url"):
            return _safe_external_url(value)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    marker = id(value)
    if marker in seen:
        return None
    seen.add(marker)
    try:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            # Re-enter the mapping branch after conversion.  Returning
            # ``str(dataclass)`` here would make list DTOs look like opaque
            # strings while top-level DTOs happened to work.
            return _serialise(dataclasses.asdict(value), seen, key)
        elif isinstance(value, Mapping):
            result: dict[str, Any] = {}
            projected = value if key == "current_activity" else _project_status_mapping(value)
            for raw_key, raw_value in projected.items():
                item_key = str(raw_key)
                item = _serialise(raw_value, seen, item_key)
                if item is not _DROP:
                    result[item_key] = item
            return result
        elif isinstance(value, (list, tuple, set, frozenset)):
            result_list = []
            for item in value:
                serialised = _serialise(item, seen)
                if serialised is not _DROP:
                    result_list.append(serialised)
            return result_list
        elif hasattr(value, "model_dump") and callable(value.model_dump):
            return _serialise(value.model_dump(), seen, key)
        elif hasattr(value, "dict") and callable(value.dict):
            return _serialise(value.dict(), seen, key)
        elif hasattr(value, "to_dict") and callable(value.to_dict):
            return _serialise(value.to_dict(), seen, key)
        elif hasattr(value, "_asdict") and callable(value._asdict):
            return _serialise(value._asdict(), seen, key)
        elif hasattr(value, "__dict__"):
            return _serialise(vars(value), seen, key)
        return str(value)
    finally:
        seen.discard(marker)


def _as_mapping(value: Any) -> Optional[dict[str, Any]]:
    serialised = _serialise(value)
    return serialised if isinstance(serialised, dict) else None


def _collection_items(value: Any, names: Sequence[str]) -> tuple[list[Any], dict[str, Any]]:
    """Extract items while preserving pagination metadata from a page DTO."""

    serialised = _serialise(value)
    if serialised is None:
        return [], {}
    if isinstance(serialised, list):
        return serialised, {}
    if not isinstance(serialised, dict):
        return [], {"value": serialised}

    for name in names:
        candidate = serialised.get(name)
        if isinstance(candidate, list):
            return candidate, serialised
    nested = serialised.get("data")
    if isinstance(nested, list):
        return nested, serialised
    if isinstance(nested, dict):
        for name in names:
            candidate = nested.get(name)
            if isinstance(candidate, list):
                return candidate, serialised
    return [], serialised


def _collection_body(
    value: Any,
    names: Sequence[str],
    *,
    limit: Optional[int] = None,
    after_id: Optional[int] = None,
) -> dict[str, Any]:
    items, metadata = _collection_items(value, names)
    body = dict(metadata)
    body["items"] = items
    primary_name = names[0] if names else "items"
    body.setdefault(primary_name, items)
    body.setdefault("count", len(items))
    pagination = body.get("pagination")
    if "total" not in body and isinstance(pagination, Mapping) and pagination.get("total") is not None:
        body["total"] = pagination.get("total")
    body.setdefault("total", len(items))
    if limit is not None:
        body.setdefault("limit", limit)
    if after_id is not None:
        body.setdefault("after_id", after_id)
    if items and "events" in names:
        last = items[-1]
        if isinstance(last, Mapping):
            for key in ("id", "event_id"):
                if last.get(key) is not None:
                    body.setdefault("next_after_id", last[key])
                    break
    return body


def _detail_body(name: str, value: Any) -> dict[str, Any]:
    payload = _serialise(value)
    body: dict[str, Any] = {"data": payload, name: payload}
    if isinstance(payload, dict):
        body.update(payload)
        body[name] = payload
        body["data"] = payload
    return body


def _json_response(payload: Mapping[str, Any], status: int = 200) -> Response:
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _error_response(code: str, message: str, status: int) -> Response:
    return _json_response({"error": {"code": code, "message": message}}, status)


def _query_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name)
    if raw is None or raw.strip() == "":
        return default
    if not re.fullmatch(r"\+?\d+", raw.strip()):
        raise ConsoleValidationError(f"{name} must be an integer")
    value = int(raw)
    if value < minimum or value > maximum:
        raise ConsoleValidationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _optional_after_id() -> Optional[int]:
    raw = request.args.get("after_id")
    if raw is None:
        return None
    if not re.fullmatch(r"\+?\d+", raw.strip()):
        raise ConsoleValidationError("after_id must be a non-negative integer")
    return int(raw)


def _query_text(*names: str) -> Optional[str]:
    for name in names:
        value = request.args.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _invoke_factory(factory: Callable[..., Any], db_path: Any) -> Any:
    """Construct a service with either ``db_path`` keyword or positional API."""

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(db_path)
    parameters = signature.parameters
    if "db_path" in parameters:
        return factory(db_path=db_path)
    if "database_path" in parameters:
        return factory(database_path=db_path)
    if "database" in parameters:
        return factory(database=db_path)
    if "path" in parameters:
        return factory(path=db_path)
    required = [
        parameter
        for parameter in parameters.values()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return factory(db_path) if required else factory()


def _default_service(db_path: Any) -> Any:
    """Resolve the optional service without making the route import it eagerly."""

    module: Optional[ModuleType] = None
    try:
        module = importlib.import_module("zoofan.console_service")
    except ModuleNotFoundError as error:
        if error.name not in {"zoofan.console_service", "zoofan.console_repository"}:
            logger.warning("Crawler console service import failed: %s", error)
        return _EmptyConsoleService()
    except ImportError as error:
        logger.warning("Crawler console service is unavailable: %s", error)
        return _EmptyConsoleService()

    for factory_name in ("get_console_service", "create_console_service", "build_console_service"):
        factory = getattr(module, factory_name, None)
        if callable(factory):
            try:
                return _invoke_factory(factory, db_path)
            except Exception:  # pragma: no cover - defensive integration boundary
                logger.exception("Unable to construct crawler console service")
                return _EmptyConsoleService()
    service_class = getattr(module, "ConsoleService", None)
    if callable(service_class):
        try:
            return _invoke_factory(service_class, db_path)
        except Exception:  # pragma: no cover - defensive integration boundary
            logger.exception("Unable to construct ConsoleService")
            return _EmptyConsoleService()
    if any(callable(getattr(module, name, None)) for names in _SERVICE_METHODS.values() for name in names):
        return module
    logger.warning("zoofan.console_service has no supported read interface")
    return _EmptyConsoleService()


def _service_for_request(captured: Any = None) -> Any:
    configured = current_app.config.get("CONSOLE_SERVICE")
    if configured is None:
        configured = current_app.extensions.get("console_service")
    if configured is None:
        configured = captured
    if configured is not None:
        if any(callable(getattr(configured, name, None)) for names in _SERVICE_METHODS.values() for name in names):
            return configured
        if callable(configured):
            cache_key = "_crawler_console_configured_service"
            if cache_key not in current_app.extensions:
                current_app.extensions[cache_key] = _invoke_factory(
                    configured, current_app.config.get("DB_PATH")
                )
            return current_app.extensions[cache_key]
        return configured
    cache_key = "_crawler_console_default_service"
    if cache_key not in current_app.extensions:
        current_app.extensions[cache_key] = _default_service(current_app.config.get("DB_PATH"))
    return current_app.extensions[cache_key]


def _invoke_method(method: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
    """Call a service method while tolerating narrower read-only signatures."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(**dict(kwargs))
    parameters = signature.parameters
    effective_kwargs = dict(kwargs)
    # The public HTTP vocabulary calls this filter ``search`` while the
    # service DTO contract uses ``text``.  Translate only when the concrete
    # method explicitly asks for the latter; a test/service accepting **kwargs
    # continues to receive the stable HTTP-facing name.
    if "search" in effective_kwargs and "search" not in parameters and "text" in parameters:
        effective_kwargs["text"] = effective_kwargs.pop("search")
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return method(**effective_kwargs)
    accepted = {
        name: value
        for name, value in effective_kwargs.items()
        if name in parameters and parameters[name].kind
        in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
    }
    positional: list[Any] = []
    for parameter in parameters.values():
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY and parameter.name in effective_kwargs:
            positional.append(effective_kwargs[parameter.name])
    return method(*positional, **accepted)


def _call(service: Any, role: str, **kwargs: Any) -> Any:
    for name in _SERVICE_METHODS[role]:
        method = getattr(service, name, None)
        if callable(method):
            try:
                return _invoke_method(method, kwargs)
            except ConsoleNotFoundError:
                raise
            except ConsoleValidationError:
                raise
            except KeyError as error:
                raise ConsoleNotFoundError(str(error)) from error
    raise ConsoleServiceUnavailable(f"{_MISSING_SERVICE_MESSAGE}: method for {role}")


def _read(role: str, *, default: Any = _MISSING, **kwargs: Any) -> Any:
    try:
        return _call(_service_for_request(), role, **kwargs)
    except ConsoleServiceUnavailable:
        if default is not _MISSING:
            return default
        raise


def _api_call(role: str, *, default: Any = _MISSING, **kwargs: Any) -> Any:
    try:
        return _read(role, default=default, **kwargs)
    except ConsoleNotFoundError:
        raise
    except ConsoleValidationError:
        raise
    except (ValueError, TypeError) as error:
        raise ConsoleValidationError(str(error)) from error


def _require_parent(role: str, identifier: str) -> None:
    try:
        result = _call(
            _service_for_request(),
            role,
            **({"run_id": identifier} if role == "run" else {"zoo_id": identifier}),
        )
    except ConsoleServiceUnavailable:
        # A narrowly injected fake may implement only the child collection;
        # let that collection return its documented empty state.
        return
    if result is None:
        raise ConsoleNotFoundError(identifier)


def _handle_api_errors(function: Callable[..., Response]) -> Callable[..., Response]:
    """Wrap a route with consistent JSON error semantics."""

    def wrapped(*args: Any, **kwargs: Any) -> Response:
        try:
            return function(*args, **kwargs)
        except LookupError as error:
            return _error_response("not_found", str(error) or "Record not found", 404)
        except ConsoleValidationError as error:
            return _error_response("invalid_request", str(error) or "Invalid request", 400)
        except ConsoleServiceUnavailable as error:
            logger.warning("Crawler console endpoint unavailable: %s", error)
            return _error_response("service_unavailable", _MISSING_SERVICE_MESSAGE, 503)

    wrapped.__name__ = function.__name__
    return wrapped


def _page(name: str, **context: Any) -> str:
    return render_template(name, **context)


def create_console_blueprint(service: Any = None) -> Blueprint:
    """Create the read-only Console API and HTML blueprint.

    ``service`` is optional dependency injection for tests and for the future
    ``console_service`` implementation.  The Flask app may also set
    ``CONSOLE_SERVICE`` or the ``console_service`` extension after blueprint
    creation.
    """

    blueprint = Blueprint("crawler_console", __name__)

    @blueprint.before_app_request
    def _install_injected_service() -> None:
        # ``create_console_blueprint(service=...)`` is useful for isolated
        # API/UI tests.  App config/extensions still win when an application
        # provides its own service after blueprint construction.
        if service is not None and current_app.config.get("CONSOLE_SERVICE") is None:
            current_app.extensions.setdefault("console_service", service)

    @blueprint.get("/api/crawler/status")
    @_handle_api_errors
    def api_status() -> Response:
        result = _api_call("status", default={"status": "idle", "active_run": None})
        if result is None:
            result = {"status": "idle", "active_run": None}
        return _json_response(_detail_body("status", result))

    @blueprint.get("/api/crawl-runs")
    @_handle_api_errors
    def api_runs() -> Response:
        limit = _query_int("limit", default=50, minimum=1, maximum=200)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        status = _query_text("status")
        result = _api_call("runs", default=[], status=status, limit=limit, offset=offset)
        return _json_response(
            _collection_body(result, ("runs", "items", "results"), limit=limit)
        )

    @blueprint.get("/api/crawl-runs/<run_id>")
    @_handle_api_errors
    def api_run(run_id: str) -> Response:
        result = _api_call("run", default=None, run_id=run_id)
        if result is None:
            raise ConsoleNotFoundError(run_id)
        return _json_response(_detail_body("run", result))

    @blueprint.get("/api/crawl-runs/<run_id>/zoos")
    @_handle_api_errors
    def api_run_zoos(run_id: str) -> Response:
        _require_parent("run", run_id)
        limit = _query_int("limit", default=200, minimum=1, maximum=500)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        result = _api_call(
            "run_zoos", default=[], run_id=run_id, limit=limit, offset=offset
        )
        return _json_response(
            _collection_body(result, ("zoos", "items", "results"), limit=limit)
        )

    @blueprint.get("/api/crawl-runs/<run_id>/sources")
    @blueprint.get("/api/crawl-runs/<run_id>/source-results")
    @_handle_api_errors
    def api_run_sources(run_id: str) -> Response:
        _require_parent("run", run_id)
        limit = _query_int("limit", default=500, minimum=1, maximum=500)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        zoo_id = _query_text("zoo", "zoo_id")
        source_id = _query_text("source", "source_id")
        result = _api_call(
            "run_sources",
            default=[],
            run_id=run_id,
            zoo_id=zoo_id,
            source_id=source_id,
            limit=limit,
            offset=offset,
        )
        return _json_response(
            _collection_body(result, ("sources", "items", "results"), limit=limit)
        )

    @blueprint.get("/api/crawl-runs/<run_id>/articles")
    @_handle_api_errors
    def api_run_articles(run_id: str) -> Response:
        _require_parent("run", run_id)
        limit = _query_int("limit", default=100, minimum=1, maximum=500)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        search = _query_text("search", "q")
        result = _api_call(
            "run_articles",
            default=[],
            run_id=run_id,
            stored_only=True,
            search=search,
            limit=limit,
            offset=offset,
        )
        return _json_response(
            _collection_body(result, ("articles", "items", "results"), limit=limit)
        )

    @blueprint.get("/api/crawl-runs/<run_id>/events")
    @_handle_api_errors
    def api_run_events(run_id: str) -> Response:
        _require_parent("run", run_id)
        limit = _query_int("limit", default=100, minimum=1, maximum=500)
        after_id = _optional_after_id()
        filters = {
            "run_id": run_id,
            "after_id": after_id,
            "level": _query_text("level"),
            "zoo": _query_text("zoo", "zoo_id"),
            "source": _query_text("source", "source_id"),
            "component": _query_text("component"),
            "event_type": _query_text("event_type", "type"),
            "search": _query_text("search", "q"),
            "limit": limit,
        }
        result = _api_call("run_events", default=[], **filters)
        return _json_response(
            _collection_body(
                result, ("events", "items", "results"), limit=limit, after_id=after_id
            )
        )

    @blueprint.get("/api/zoos")
    @_handle_api_errors
    def api_zoos() -> Response:
        limit = _query_int("limit", default=200, minimum=1, maximum=500)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        result = _api_call("zoos", default=[], limit=limit, offset=offset)
        return _json_response(
            _collection_body(result, ("zoos", "items", "results"), limit=limit)
        )

    @blueprint.get("/api/zoos/<zoo_id>")
    @_handle_api_errors
    def api_zoo(zoo_id: str) -> Response:
        result = _api_call("zoo", default=None, zoo_id=zoo_id)
        if result is None:
            raise ConsoleNotFoundError(zoo_id)
        return _json_response(_detail_body("zoo", result))

    @blueprint.get("/api/zoos/<zoo_id>/crawl-history")
    @_handle_api_errors
    def api_zoo_history(zoo_id: str) -> Response:
        _require_parent("zoo", zoo_id)
        limit = _query_int("limit", default=100, minimum=1, maximum=500)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        result = _api_call(
            "zoo_history", default=[], zoo_id=zoo_id, limit=limit, offset=offset
        )
        return _json_response(
            _collection_body(result, ("history", "items", "results"), limit=limit)
        )

    @blueprint.get("/console")
    @blueprint.get("/crawler-console")
    @blueprint.get("/console/overview")
    def console_overview() -> str:
        return _page("console_overview.html", page="overview")

    @blueprint.get("/console/runs")
    @blueprint.get("/console/runs/")
    def console_runs() -> str:
        return _page("console_runs.html", page="runs")

    @blueprint.get("/console/runs/<run_id>")
    def console_run_detail(run_id: str) -> str:
        return _page("console_run_detail.html", page="run-detail", run_id=run_id)

    @blueprint.get("/console/zoos/<zoo_id>")
    def console_zoo_detail(zoo_id: str) -> str:
        return _page("console_zoo_detail.html", page="zoo-detail", zoo_id=zoo_id)

    @blueprint.get("/console/articles")
    def console_articles() -> str:
        return _page("console_articles.html", page="articles", run_id=request.args.get("run_id", ""))

    @blueprint.get("/console/events")
    @blueprint.get("/console/runs/<run_id>/events")
    def console_events(run_id: str = "") -> str:
        return _page("console_events.html", page="events", run_id=run_id or request.args.get("run_id", ""))

    blueprint.console_service = service  # type: ignore[attr-defined]
    return blueprint


__all__ = [
    "ConsoleNotFoundError",
    "ConsoleServiceProtocol",
    "ConsoleValidationError",
    "create_console_blueprint",
]
