"""Small, server-rendered inspection dashboard for a crawler database.

The dashboard deliberately reads the SQLite database through a narrow, read-only
view of the crawler tables.  Keeping this layer independent from the crawler's
write API makes it useful while a crawl is running and also keeps the web UI from
depending on implementation details of the ingestion code.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote, urlparse

from flask import Flask, abort, g, render_template, request, send_from_directory


_TABLE_ALIASES: dict[str, tuple[str, ...]] = {
    "discoveries": ("article_discoveries", "discoveries", "article_sources"),
    "stats": ("crawl_run_stats", "run_stats", "crawl_stats"),
}


def create_app(
    db_path: str | os.PathLike[str], control_url: str | None = None
) -> Flask:
    """Create the crawler inspection application.

    ``db_path`` is normally a filesystem path to a database initialized by
    :class:`zoofan.storage.SQLiteStorage`.  A persistent connection is used for
    ``:memory:`` databases so a test can create its schema before making client
    requests.
    """

    app = Flask(
        __name__,
        template_folder=str(Path(__file__).with_name("templates")),
        static_folder=str(Path(__file__).with_name("static")),
    )
    app.config["DB_PATH"] = os.fspath(db_path)
    app.config["CONTROL_URL"] = _safe_external_url(control_url)

    if app.config["DB_PATH"] == ":memory:":
        app.extensions["dashboard_memory_db"] = _connect(app.config["DB_PATH"])

    @app.template_filter("timestamp")
    def timestamp_filter(value: Any) -> str:
        return _format_timestamp(value)

    @app.context_processor
    def control_navigation() -> dict[str, str | None]:
        """Expose the optional, already-validated control UI link to templates."""

        return {"control_url": app.config["CONTROL_URL"]}

    @app.teardown_appcontext
    def close_db(_exception: BaseException | None = None) -> None:
        connection = g.pop("dashboard_db", None)
        if connection is not None and connection is not app.extensions.get("dashboard_memory_db"):
            connection.close()

    @app.get("/")
    def index() -> str:
        data = _load_data(get_db())
        dashboard = _build_dashboard(data)
        return render_template("index.html", **dashboard)

    @app.get("/articles")
    def articles() -> str:
        data = _load_data(get_db())
        zoo_filter = request.args.get("zoo", "").strip()
        source_type_filter = request.args.get("source_type", "").strip()
        article_rows = _article_list(data, zoo_filter, source_type_filter)
        return render_template(
            "articles.html",
            articles=article_rows,
            zoos=data["zoos"],
            source_types=sorted(
                {_source_type(source) for source in data["sources"] if _source_type(source)}
            ),
            selected_zoo=zoo_filter,
            selected_source_type=source_type_filter,
        )

    @app.get("/articles/<article_id>")
    def article_detail(article_id: str) -> str:
        data = _load_data(get_db())
        article = _article_detail(data, article_id)
        if article is None:
            abort(404)
        return render_template("article_detail.html", article=article)

    @app.get("/sources")
    def sources() -> str:
        data = _load_data(get_db())
        return render_template("sources.html", sources=_source_list(data))

    @app.get("/runs")
    def runs() -> str:
        data = _load_data(get_db())
        return render_template("runs.html", run_groups=_run_groups(data))

    @app.get("/favicon.ico")
    def favicon() -> Any:
        static_folder = app.static_folder
        if static_folder is None:
            abort(404)
        return send_from_directory(static_folder, "favicon.svg", mimetype="image/svg+xml")

    return app


def main(
    db_path: str | os.PathLike[str] = "data/zoofan.db",
    host: str = "127.0.0.1",
    port: int = 8000,
    control_url: str | None = None,
) -> None:
    """Run the dashboard's development server.

    The explicit defaults keep this callable from a CLI subcommand while also
    making ``python -m zoofan.dashboard`` useful for local inspection.
    """

    app = create_app(db_path, control_url=control_url)
    app.run(host=host, port=port)


def _connect(db_path: str) -> sqlite3.Connection:
    if db_path == ":memory:":
        connection = sqlite3.connect(db_path)
    else:
        path = Path(db_path).expanduser().resolve()
        if path.is_file():
            uri = f"file:{quote(str(path), safe='/')}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            # Missing databases are represented as an empty read-only view.
            # Crucially, opening the dashboard never creates the configured path.
            connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def get_db() -> sqlite3.Connection:
    """Return the request-scoped dashboard connection."""

    if "dashboard_db" not in g:
        from flask import current_app

        app = current_app
        memory_db = app.extensions.get("dashboard_memory_db")
        if memory_db is not None:
            g.dashboard_db = memory_db
        else:
            g.dashboard_db = _connect(app.config["DB_PATH"])
    return g.dashboard_db


def _table_rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """Read a table, returning an empty list when an optional table is absent."""

    try:
        rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.OperationalError:
        return set()


def _first(value: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return default


def _table_name(connection: sqlite3.Connection, logical_name: str) -> str:
    candidates = _TABLE_ALIASES.get(logical_name, (logical_name,))
    for candidate in candidates:
        if _table_columns(connection, candidate):
            return candidate
    return candidates[0]


def _load_data(connection: sqlite3.Connection) -> dict[str, Any]:
    """Load the small set of records needed by the five dashboard pages."""

    return {
        "zoos": _table_rows(connection, "zoos"),
        "sources": _table_rows(connection, "sources"),
        "articles": _table_rows(connection, "articles"),
        "discoveries": _table_rows(connection, _table_name(connection, "discoveries")),
        "runs": _table_rows(connection, "crawl_runs"),
        "stats": _table_rows(connection, _table_name(connection, "stats")),
    }


def _key(value: Any) -> str:
    return "" if value is None else str(value)


def _zoo_name(zoo: Mapping[str, Any] | None) -> str:
    if zoo is None:
        return "Unknown zoo"
    return str(_first(zoo, "name", "slug", "id", default="Unknown zoo"))


def _zoo_slug(zoo: Mapping[str, Any] | None) -> str:
    if zoo is None:
        return ""
    return str(_first(zoo, "slug", "name", "id", default=""))


def _source_type(source: Mapping[str, Any] | None) -> str:
    if source is None:
        return ""
    return str(_first(source, "source_type", "kind", "type", default=""))


def _source_map(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {_key(_first(row, "id", "source_id")): row for row in data["sources"]}


def _zoo_map(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {_key(_first(row, "id", "zoo_id")): row for row in data["zoos"]}


def _article_zoo_ids(data: Mapping[str, Any]) -> dict[str, set[str]]:
    source_by_id = _source_map(data)
    zoo_ids: dict[str, set[str]] = defaultdict(set)
    for article in data["articles"]:
        article_id = _key(_first(article, "id", "article_id"))
        direct_zoo = _first(article, "zoo_id")
        if direct_zoo is not None:
            zoo_ids[article_id].add(_key(direct_zoo))
    for discovery in data["discoveries"]:
        article_id = _key(_first(discovery, "article_id"))
        source = source_by_id.get(_key(_first(discovery, "source_id")))
        zoo_id = _first(source or {}, "zoo_id")
        if zoo_id is not None:
            zoo_ids[article_id].add(_key(zoo_id))
    return zoo_ids


def _safe_external_url(value: Any) -> str | None:
    if not value:
        return None
    url = str(value).strip()
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError, AttributeError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _article_url(article: Mapping[str, Any]) -> str | None:
    return _safe_external_url(_first(article, "canonical_url"))


def _article_relationships(
    data: Mapping[str, Any], article_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Mapping[str, Any]]]:
    source_by_id = _source_map(data)
    zoo_by_id = _zoo_map(data)
    discoveries = [
        row for row in data["discoveries"]
        if _key(_first(row, "article_id")) == article_id
    ]
    source_ids = {
        _key(_first(row, "source_id")) for row in discoveries
        if _first(row, "source_id") is not None
    }
    sources = [source_by_id[item] for item in source_ids if item in source_by_id]
    sources.sort(key=lambda source: _key(_first(source, "id", "source_id")))
    zoo_ids = {
        _key(_first(source, "zoo_id")) for source in sources
        if _first(source, "zoo_id") is not None
    }
    article: Mapping[str, Any] = next(
        (row for row in data["articles"] if _key(_first(row, "id", "article_id")) == article_id),
        {},
    )
    if _first(article, "zoo_id") is not None:
        zoo_ids.add(_key(_first(article, "zoo_id")))
    zoos = [zoo_by_id[item] for item in zoo_ids if item in zoo_by_id]
    zoos.sort(key=_zoo_name)
    return sources, zoos, discoveries


def _discovery_bounds(discoveries: Iterable[Mapping[str, Any]]) -> tuple[Any, Any]:
    discoveries = list(discoveries)
    first_values = [_first(row, "discovered_at") for row in discoveries]
    last_values = [_first(row, "last_discovered_at", "discovered_at") for row in discoveries]
    first_values = [value for value in first_values if value not in (None, "")]
    last_values = [value for value in last_values if value not in (None, "")]
    first = min(first_values, key=_sort_timestamp) if first_values else None
    last = max(last_values, key=_sort_timestamp) if last_values else None
    return first, last


def _article_list(
    data: Mapping[str, Any], zoo_filter: str = "", source_type_filter: str = ""
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for article in data["articles"]:
        article_id = _key(_first(article, "id", "article_id"))
        source_rows, zoo_rows, discoveries = _article_relationships(data, article_id)
        first_discovered, _ = _discovery_bounds(discoveries)
        source_types = sorted({_source_type(source) for source in source_rows if _source_type(source)})
        zoo_slugs = {_zoo_slug(zoo) for zoo in zoo_rows}
        if zoo_filter and zoo_filter not in zoo_slugs and zoo_filter not in {
            _key(_first(zoo, "id")) for zoo in zoo_rows
        }:
            continue
        if source_type_filter and source_type_filter not in source_types:
            continue
        selected.append(
            {
                "id": article_id,
                "title": _first(article, "title", default="Untitled article") or "Untitled article",
                "published_at": _first(article, "published_at"),
                "updated_at_source": _first(article, "updated_at_source"),
                "first_discovered": first_discovered,
                "url": _article_url(article),
                "canonical_url": _first(article, "canonical_url"),
                "zoo_names": [_zoo_name(zoo) for zoo in zoo_rows],
                "zoo_slugs": [_zoo_slug(zoo) for zoo in zoo_rows],
                "source_type": ", ".join(source_types),
                "source_types": source_types,
                "source_names": [str(_first(source, "name", "id", default="")) for source in source_rows],
                "language": _first(article, "language"),
                "crawl_status": _first(article, "crawl_status"),
                "http_status": _first(article, "http_status"),
            }
        )
    selected.sort(
        key=lambda article: _sort_timestamp(article["published_at"] or article["first_discovered"]),
        reverse=True,
    )
    return selected


def _article_detail(data: Mapping[str, Any], article_id: str) -> dict[str, Any] | None:
    article = next(
        (
            row
            for row in data["articles"]
            if _key(_first(row, "id", "article_id")) == _key(article_id)
        ),
        None,
    )
    if article is None:
        return None
    source_rows, zoo_rows, discoveries = _article_relationships(data, _key(article_id))
    first_discovered, last_discovered = _discovery_bounds(discoveries)
    original_url = _first(article, "source_url")
    canonical_url = _first(article, "canonical_url")
    return {
        "id": _key(_first(article, "id", "article_id")),
        "title": _first(article, "title", default="Untitled article") or "Untitled article",
        "zoo_names": [_zoo_name(zoo) for zoo in zoo_rows],
        "sources": [str(_first(source, "name", "id", default="")) for source in source_rows],
        "original_url": original_url,
        "original_link": _safe_external_url(original_url),
        "canonical_url": canonical_url,
        "canonical_link": _safe_external_url(canonical_url),
        "language": _first(article, "language"),
        "http_status": _first(article, "http_status"),
        "crawl_status": _first(article, "crawl_status"),
        "html_hash": _first(article, "html_hash"),
        "published_at": _first(article, "published_at"),
        "updated_at_source": _first(article, "updated_at_source"),
        "first_discovered": first_discovered,
        "last_discovered": last_discovered,
        "fetched_at": _first(article, "last_fetched_at"),
        "stored_at": _first(article, "created_at"),
        "storage_updated_at": _first(article, "updated_at"),
        "author": _first(article, "author"),
        "summary": _first(article, "summary"),
        "content": _first(article, "content"),
    }


def _source_list(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    zoo_by_id = _zoo_map(data)
    result = []
    for source in data["sources"]:
        config = _first(source, "config_json", "config", default={})
        zoo = zoo_by_id.get(_key(_first(source, "zoo_id")))
        result.append(
            {
                "id": _key(_first(source, "id", "source_id")),
                "name": _first(source, "name", default="") or _first(source, "url", default=""),
                "url": _safe_external_url(_first(source, "url")),
                "raw_url": _first(source, "url"),
                "zoo_name": _zoo_name(zoo),
                "zoo_country": _first(zoo or {}, "country_code"),
                "zoo_language": _first(zoo or {}, "language"),
                "source_type": _source_type(source),
                "language": _first(source, "language"),
                "config": _pretty_json(config),
                "enabled": bool(_first(source, "enabled", default=True)),
                "status": _first(source, "status", default="unknown") or "unknown",
                "last_checked": _first(source, "last_checked", "checked_at"),
                "last_success": _first(source, "last_success", "last_crawled_at"),
                "last_error": _first(source, "last_error", "error"),
                "last_http_status": _first(source, "last_http_status", "http_status"),
            }
        )
    result.sort(key=lambda source: (source["zoo_name"], source["name"]))
    return result


def _build_dashboard(data: Mapping[str, Any]) -> dict[str, Any]:
    article_rows = _article_list(data)
    article_by_id = {
        _key(_first(article, "id", "article_id")): article for article in data["articles"]
    }
    article_zoos = _article_zoo_ids(data)
    panels = []
    for zoo in sorted(data["zoos"], key=_zoo_name):
        zoo_id = _key(_first(zoo, "id", "zoo_id"))
        zoo_sources = [
            source for source in data["sources"] if _key(_first(source, "zoo_id")) == zoo_id
        ]
        zoo_articles = [
            article
            for article_id, article_zoo_ids in article_zoos.items()
            if zoo_id in article_zoo_ids
            for article in [article_by_id.get(article_id)]
            if article is not None
        ]
        zoo_stats = [
            stat
            for stat in data["stats"]
            if _key(_first(stat, "zoo_id")) == zoo_id
        ]
        latest_run, latest_stats = _latest_zoo_run(data["runs"], zoo_stats)
        errors = _error_count(None, latest_stats)
        new_count = _metric(latest_stats, "new_count", "new_articles", "stored_count")
        already_known_count = _metric(latest_stats, "already_known_count")
        duplicate_count = _optional_metric(latest_stats, "duplicate_candidate_count")
        latest_article = _latest_article_for_zoo(zoo_articles)
        panels.append(
            {
                "id": zoo_id,
                "name": _zoo_name(zoo),
                "slug": _zoo_slug(zoo),
                "website_url": _safe_external_url(_first(zoo, "website_url", "url")),
                "country": _first(zoo, "country_code"),
                "language": _first(zoo, "language"),
                "status": _zoo_status(latest_run, latest_stats, zoo_sources),
                "sources": len(zoo_sources),
                "last_crawl": _first(latest_run or {}, "finished_at", "started_at"),
                "articles": len(zoo_articles),
                "stored": new_count,
                "already_known": already_known_count,
                "duplicate_candidates": duplicate_count,
                "errors": errors,
                "latest_article": _article_summary(latest_article) if latest_article else None,
            }
        )

    totals = {
        "sources": len(data["sources"]),
        "articles": len(data["articles"]),
        "stored": sum(int(panel["stored"]) for panel in panels),
        "already_known": sum(int(panel["already_known"]) for panel in panels),
        "duplicate_candidates": _sum_known(panel["duplicate_candidates"] for panel in panels),
        "errors": sum(int(panel["errors"]) for panel in panels),
        "last_crawl": _latest_run_value(data["runs"], "finished_at", "started_at"),
    }
    return {"panels": panels, "totals": totals, "articles": article_rows[:10]}


def _latest_zoo_run(
    runs: Iterable[Mapping[str, Any]], stats: list[Mapping[str, Any]]
) -> tuple[Mapping[str, Any] | None, list[Mapping[str, Any]]]:
    run_by_id = {
        _key(_first(run, "id", "run_id", "crawl_run_id")): run for run in runs
    }
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for stat in stats:
        run_id = _key(_first(stat, "crawl_run_id", "run_id"))
        grouped[run_id].append(stat)
    if not grouped:
        return None, []
    latest_run_id = max(
        grouped,
        key=lambda run_id: _sort_timestamp(
            _first(run_by_id.get(run_id, {}), "finished_at", "started_at")
        ),
    )
    return run_by_id.get(latest_run_id), grouped[latest_run_id]


def _zoo_status(
    run: Mapping[str, Any] | None,
    stats: list[Mapping[str, Any]],
    sources: list[Mapping[str, Any]],
) -> str:
    if run is None:
        return "NEVER CRAWLED"
    run_status = str(_first(run, "status", default="")).lower()
    error_count = _error_count(run, stats)
    if run_status in {"running", "in_progress", "started"}:
        return "WARNING"
    if run_status in {"failed", "failure", "error", "errored"}:
        return "FAILED"
    if error_count or run_status in {"partial", "warning", "warn", "completed_with_errors"}:
        return "WARNING"
    if any(str(_first(source, "status", default="")).lower() in {"failed", "error"} for source in sources):
        return "WARNING"
    return "HEALTHY"


def _error_count(run: Mapping[str, Any] | None, stats: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for stat in stats:
        explicit = _first(stat, "error_count", "errors_count")
        explicit_count: int | None = None
        if explicit is not None:
            try:
                explicit_count = int(explicit)
            except (TypeError, ValueError):
                pass
        errors = _parse_errors(_first(stat, "errors_json", "errors", "error_json"))
        implicit_count = len(errors) or (1 if _first(stat, "error", "last_error") else 0)
        count += max(explicit_count if explicit_count is not None else 0, implicit_count)
    if run and _first(run, "error", "last_error"):
        count += 1
    return count


def _metric(
    stats: Iterable[Mapping[str, Any]], *names: str, default: int = 0
) -> int:
    total = 0
    found = False
    for stat in stats:
        for name in names:
            value = _first(stat, name)
            if value is not None:
                try:
                    total += int(value)
                except (TypeError, ValueError):
                    pass
                found = True
                break
    return total if found else default


def _optional_metric(stats: Iterable[Mapping[str, Any]], name: str) -> int | None:
    """Sum a modern persisted metric, preserving absence as unknown."""

    total = 0
    found = False
    for stat in stats:
        if name not in stat:
            continue
        value = stat[name]
        if value is None:
            continue
        try:
            total += int(value)
        except (TypeError, ValueError):
            continue
        found = True
    return total if found else None


def _sum_known(values: Iterable[Any]) -> int | None:
    known = [int(value) for value in values if value is not None]
    return sum(known) if known else None


def _latest_article_for_zoo(articles: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    articles = [article for article in articles if _first(article, "published_at") not in (None, "")]
    if not articles:
        return None
    return max(articles, key=lambda article: _sort_timestamp(_first(article, "published_at")))


def _article_summary(article: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": _key(_first(article, "id", "article_id")),
        "title": _first(article, "title", default="Untitled article") or "Untitled article",
        "url": _article_url(article),
        "published_at": _first(article, "published_at"),
    }


def _run_groups(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    stats_by_run: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for stat in data["stats"]:
        stats_by_run[_key(_first(stat, "crawl_run_id", "run_id"))].append(stat)
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in data["runs"]:
        batch_id = _key(_first(run, "batch_id")) or _key(_first(run, "id", "run_id"))
        groups[batch_id].append(run)
    result: list[dict[str, Any]] = []
    for batch_id, runs in groups.items():
        runs = sorted(runs, key=lambda run: _sort_timestamp(_first(run, "started_at")), reverse=True)
        run_views: list[dict[str, Any]] = []
        for run in runs:
            run_id = _key(_first(run, "id", "run_id"))
            run_stats = stats_by_run.get(run_id, [])
            stats_by_zoo: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
            for stat in run_stats:
                stats_by_zoo[_key(_first(stat, "zoo_id"))].append(stat)
            zoo_views: list[dict[str, Any]] = []
            for zoo in sorted(data["zoos"], key=_zoo_name):
                zoo_id = _key(_first(zoo, "id", "zoo_id"))
                stats = stats_by_zoo.get(zoo_id, [])
                zoo_views.append(
                    {
                        "name": _zoo_name(zoo),
                        "status": ", ".join(
                            sorted({str(_first(stat, "status", default="unknown")) for stat in stats})
                        )
                        or "—",
                        "discovered": _metric(stats, "discovered_count"),
                        "fetched": _metric(stats, "fetched_count"),
                        "stored": _metric(stats, "stored_count", "new_count"),
                        "already_known": _metric(stats, "already_known_count"),
                        "duplicate_candidates": _optional_metric(stats, "duplicate_candidate_count"),
                        "errors": _error_count(None, stats),
                        "error_summary": _error_summary(stats),
                    }
                )
            run_views.append(
                {
                    "id": run_id,
                    "status": _first(run, "status", default="unknown"),
                    "started_at": _first(run, "started_at"),
                    "finished_at": _first(run, "finished_at"),
                    "duration_ms": _first(run, "duration_ms"),
                    "error": _first(run, "error"),
                    "zoos": zoo_views,
                }
            )
        result.append({"batch_id": batch_id, "runs": run_views})
    result.sort(
        key=lambda group: _sort_timestamp(
            _first(group["runs"][0], "started_at") if group["runs"] else None
        ),
        reverse=True,
    )
    return result


def _error_summary(stats: Iterable[Mapping[str, Any]]) -> list[str]:
    values: list[str] = []
    for stat in stats:
        error = _first(stat, "error", "last_error")
        if error:
            values.append(str(error))
        values.extend(_parse_errors(_first(stat, "errors_json", "errors", "error_json")))
    return list(dict.fromkeys(values))


def _parse_errors(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item]
        if parsed:
            return [str(parsed)]
    return [str(value)]


def _pretty_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    if value in (None, ""):
        return "—"
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _sort_timestamp(value: Any) -> float:
    if value is None:
        return float("-inf")
    if isinstance(value, datetime):
        return value.timestamp()
    text = str(value).strip()
    if not text:
        return float("-inf")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")


def _format_timestamp(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()):
        return text.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _latest_run_value(runs: Iterable[Mapping[str, Any]], *keys: str) -> Any:
    runs = list(runs)
    if not runs:
        return None
    latest = max(runs, key=lambda run: _sort_timestamp(_first(run, "finished_at", "started_at")))
    return _first(latest, *keys)


__all__ = ["create_app", "main"]


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI itself
    main()
