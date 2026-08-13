"""Fresh, live end-to-end acceptance workflow for ZooFanCrawler.

The endgoal intentionally owns orchestration only.  Crawling, parsing,
storage, and dashboard behavior remain in their normal modules; this module
does not provide fixtures or mock a live crawl.  Every acceptance invocation
starts with a fresh, explicitly named SQLite database and emits a durable
JSON report, even when one of the gates fails.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "zoos.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "acceptance.db"
ARTIFACTS_DIR = ROOT / "artifacts"
REPORT_PATH = ARTIFACTS_DIR / "endgoal-report.json"
SCREENSHOT_PATH = ARTIFACTS_DIR / "dashboard-acceptance.png"

PYTEST_TIMEOUT = 600
CRAWL_TIMEOUT = 600
DASHBOARD_TIMEOUT = 90
INSTALL_TIMEOUT = 300
READINESS_TIMEOUT = 30

LOGGER = logging.getLogger("zoofan.endgoal")

_NAVIGATION_PATTERNS = (
    re.compile(
        r"/(?:ticket|tickets|shop|restaurant|gastronomie|contact|kontakt|privacy|"
        r"datenschutz|terms|term|cookie|cookies|jobs?|karriere)(?:/|$)",
        re.IGNORECASE,
    ),
    re.compile(r"/(?:events?|veranstaltungen|kalender|calendar|navigation)(?:/|$)", re.IGNORECASE),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _human_banner(status: str) -> str:
    return "ZOOFAN CRAWLER PROTOTYPE: PASS" if status == "PASS" else "ZOOFAN CRAWLER PROTOTYPE: FAIL"


_DASHBOARD_REQUIRED_FIELDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "/": (
        ("Sources", ("sources",)),
        ("Articles", ("articles",)),
        ("Last crawl", ("last crawl",)),
        ("Stored", ("stored", "stored new")),
        ("Already known / skipped", ("already known", "already known skipped")),
        ("Duplicate candidates", ("duplicate candidates",)),
        ("Errors", ("errors",)),
    ),
    "/articles": (
        ("Title", ("title",)),
        ("Zoo", ("zoo",)),
        ("Published at source", ("published at source",)),
        ("Updated at source", ("updated at source",)),
        ("Discovered at", ("discovered at", "first discovered")),
        ("Source", ("source", "source id")),
        ("Language", ("language",)),
        ("Crawl status", ("crawl status",)),
        ("Article HTTP status", ("article http status",)),
        ("Canonical URL", ("canonical url",)),
    ),
    "/sources": (
        ("Zoo", ("zoo",)),
        ("Source", ("source", "source id")),
        ("Kind", ("kind", "crawl method")),
        ("Language", ("language",)),
        ("Status", ("status",)),
        ("Last check", ("last check", "last checked")),
        ("HTTP status", ("http status",)),
        ("Last error", ("last error",)),
    ),
    "/runs": (
        ("Start", ("start", "started")),
        ("End", ("end", "finished")),
        ("Duration", ("duration",)),
        ("Status", ("status",)),
        ("Discovered", ("discovered",)),
        ("Fetched", ("fetched",)),
        ("Stored", ("stored", "stored new")),
        ("Already known / skipped", ("already known", "already known skipped")),
        ("Duplicate candidates", ("duplicate candidates",)),
        ("Errors", ("errors",)),
    ),
}

_DETAIL_REQUIRED_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Article ID", ("article id",)),
    ("Title", ("title",)),
    ("Zoo", ("zoo",)),
    ("Source", ("source", "sources")),
    ("Original URL", ("original url",)),
    ("Canonical URL", ("canonical url",)),
    ("Language", ("language",)),
    ("Article HTTP status", ("article http status",)),
    ("Crawl status", ("crawl status",)),
    ("HTML hash", ("html hash",)),
    ("Published at source", ("published at source",)),
    ("Updated at source", ("updated at source",)),
    ("First discovered", ("first discovered",)),
    ("Last discovered", ("last discovered",)),
    ("Fetched at", ("fetched at",)),
    ("Stored at", ("stored at",)),
    ("Storage updated at", ("storage updated at",)),
)

_REQUIRED_DATABASE_MATCHES: tuple[str, ...] = (
    "articles.title",
    "articles.zoo",
    "articles.published_at_source",
    "articles.updated_at_source",
    "articles.discovered_at",
    "articles.source",
    "articles.language",
    "articles.crawl_status",
    "articles.article_http_status",
    "articles.canonical_url",
    "articles.access_link",
    "detail.article_id",
    "detail.title",
    "detail.zoo",
    "detail.sources",
    "detail.original_url",
    "detail.canonical_url",
    "detail.language",
    "detail.article_http_status",
    "detail.crawl_status",
    "detail.html_hash",
    "detail.published_at_source",
    "detail.updated_at_source",
    "detail.first_discovered",
    "detail.last_discovered",
    "detail.fetched_at",
    "detail.stored_at",
    "detail.storage_updated_at",
    "source.zoo",
    "source.id",
    "source.kind",
    "source.language",
    "source.status",
    "source.last_checked",
    "source.http_status",
    "source.last_error",
    "run.duration",
    "run.start",
    "run.end",
    "run.status",
    "run.discovered",
    "run.fetched",
    "run.stored",
    "run.already_known",
    "run.duplicate_candidates",
    "run.errors",
    "home.stored",
    "home.already_known",
    "home.duplicate_candidates",
    "home.errors",
    "metrics.home_runs_consistent",
)


def _normalise_semantic_label(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _has_semantic_field(route: Mapping[str, Any], aliases: Sequence[str]) -> bool:
    labels = {
        _normalise_semantic_label(label)
        for label in route.get("labels", ())
        if _normalise_semantic_label(label)
    }
    return any(_normalise_semantic_label(alias) in labels for alias in aliases)


def evaluate_dashboard_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Purely evaluate a browser observation against Dashboard semantics.

    The browser collector is intentionally separate.  This function performs
    no I/O, does not trust HTTP 200 alone, and returns every named failure so a
    caller cannot accidentally turn a partial smoke test into acceptance.
    """

    failures: list[str] = []
    if observation.get("screenshot_written_this_run") is not True:
        failures.append("dashboard screenshot: not written during current browser run")
    routes = observation.get("routes", {})
    routes = routes if isinstance(routes, Mapping) else {}
    for path, requirements in _DASHBOARD_REQUIRED_FIELDS.items():
        route = routes.get(path)
        if not isinstance(route, Mapping):
            failures.append(f"route {path}: missing mandatory route observation")
            continue
        if route.get("status") != 200:
            failures.append(f"route {path}: expected HTTP 200, got {route.get('status')!r}")
        if not str(route.get("body") or "").strip():
            failures.append(f"route {path}: page is blank")
        for display_name, aliases in requirements:
            if not _has_semantic_field(route, aliases):
                failures.append(f"route {path}: missing mandatory field '{display_name}'")

    detail_routes = [
        (path, value)
        for path, value in routes.items()
        if re.fullmatch(r"/articles/[^/]+", str(path)) and isinstance(value, Mapping)
    ]
    if not detail_routes:
        failures.append("article detail: missing real /articles/<article_id> route observation")
    else:
        path, detail = detail_routes[0]
        if detail.get("status") != 200:
            failures.append(f"article detail {path}: expected HTTP 200, got {detail.get('status')!r}")
        if not str(detail.get("body") or "").strip():
            failures.append(f"article detail {path}: page is blank")
        for display_name, aliases in _DETAIL_REQUIRED_FIELDS:
            if not _has_semantic_field(detail, aliases):
                failures.append(f"article detail {path}: missing mandatory field '{display_name}'")

    database_matches = observation.get("database_matches")
    database_matches = database_matches if isinstance(database_matches, Mapping) else {}
    for name in _REQUIRED_DATABASE_MATCHES:
        if name not in database_matches:
            failures.append(f"database evidence: missing required match '{name}'")
        elif database_matches[name] is not True:
            failures.append(f"database evidence: rendered value does not match '{name}'")

    null_date = observation.get("null_publication")
    if not isinstance(null_date, Mapping):
        failures.append("NULL publication: no database-backed NULL-date observation")
    else:
        rendered = str(null_date.get("rendered") or "").strip()
        created_at = str(null_date.get("storage_created") or "").strip()
        if _normalise_semantic_label(rendered) != "unknown" and rendered != "—":
            failures.append("NULL publication: Published at source must render Unknown or —")
        if created_at and rendered == created_at:
            failures.append("NULL publication: storage created time was rendered as source publication")

    for error in observation.get("browser_assertion_errors", ()) or ():
        failures.append(f"browser assertion: {error}")
    for error in observation.get("page_errors", ()) or ():
        failures.append(f"browser page error: {error}")
    for error in observation.get("console_errors", ()) or ():
        failures.append(f"browser console error: {error}")

    return {"status": "PASS" if not failures else "FAIL", "failures": failures}


def finalize_endgoal_report(report: Mapping[str, Any]) -> tuple[int, list[str]]:
    """Return the only authoritative exit code and final human-report lines."""

    failures: list[str] = []
    gate_names = ("unit_tests", "live", "dedup", "failure_isolation", "dashboard")
    for name in gate_names:
        gate = report.get(name)
        if not isinstance(gate, Mapping) or gate.get("status") != "PASS":
            failures.append(f"gate failed: {name}")
            if isinstance(gate, Mapping) and gate.get("error"):
                failures.append(f"{name}: {gate['error']}")
    dashboard = report.get("dashboard")
    if isinstance(dashboard, Mapping):
        failures.extend(str(item) for item in dashboard.get("failures", ()) or ())
    live = report.get("live")
    if isinstance(live, Mapping):
        failures.extend(str(item) for item in live.get("failures", ()) or ())
    failures.extend(str(item) for item in report.get("errors", ()) or ())
    if failures:
        return 1, [*(f"ENDGOAL FAILED ITEM: {item}" for item in failures), _human_banner("FAIL")]
    return 0, [_human_banner("PASS")]


def emit_final_endgoal_result(report: Mapping[str, Any]) -> int:
    """Print named failures and the single final banner, returning its exit code."""

    exit_code, lines = finalize_endgoal_report(report)
    for line in lines:
        print(line, flush=True)
    return exit_code


def _print_lines(prefix: str, value: str | None) -> None:
    if not value:
        return
    for line in value.rstrip().splitlines():
        print(f"{prefix} {line}", flush=True)


def _run_subprocess(
    args: Sequence[str], *, label: str, timeout: float, cwd: Path = ROOT
) -> dict[str, Any]:
    """Run one external command with argument-list semantics and trace output."""

    command = [str(item) for item in args]
    print(f"ENDGOAL {label} START: {command!r}", flush=True)
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    timed_out = False
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            process.kill()
            output, _ = process.communicate()
            output = (output or "") + f"\nTIMEOUT after {timeout:g}s: {exc}"
    except Exception as exc:  # surfaced in the report; never silently swallowed
        output = f"{type(exc).__name__}: {exc}"
        return {
            "command": command,
            "returncode": None,
            "output": output,
            "duration_seconds": round(time.monotonic() - started, 3),
            "timed_out": False,
            "status": "FAIL",
        }
    returncode = process.returncode if process is not None else None
    _print_lines(f"ENDGOAL {label} OUTPUT:", output)
    success = returncode == 0 and not timed_out
    result = {
        "command": command,
        "returncode": returncode,
        "output": output,
        "duration_seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "status": "PASS" if success else "FAIL",
    }
    print(f"ENDGOAL {label} {'PASS' if success else 'FAIL'}", flush=True)
    return result


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write JSON beside the destination, then atomically replace it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _url_ok(value: Any) -> bool:
    if not value:
        return False
    parsed = urlparse(str(value).strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _config_value(source: Mapping[str, Any], *keys: str) -> Any:
    config = source.get("config", source.get("config_json", {}))
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (TypeError, ValueError, json.JSONDecodeError):
            config = {}
    config = config if isinstance(config, Mapping) else {}
    for key in keys:
        if key in config:
            return config[key]
    return None


def _patterns(source: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("allow_patterns", "article_allow", "allow_regex"):
        value = _config_value(source, key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Iterable):
            values.extend(str(item) for item in value if str(item).strip())
    return tuple(values)


def _explicitly_allowed(url: str, source: Mapping[str, Any]) -> bool:
    for pattern in _patterns(source):
        try:
            if re.search(pattern, url, re.IGNORECASE):
                return True
        except re.error:
            if pattern.lower() in url.lower():
                return True
    return False


def navigation_false_positive(url: str, source: Mapping[str, Any] | None = None) -> str | None:
    """Return a rejection reason for obvious navigation URLs, else ``None``.

    Configured article allow rules are considered deliberately: an explicit
    allow can authorize an otherwise generic path such as ``/events/story``.
    The source URL itself and a bare homepage remain navigation regardless.
    """

    source = source or {}
    candidate = str(url or "").strip()
    if not _url_ok(candidate):
        return "canonical URL is not an http(s) URL"
    parsed = urlparse(candidate)
    path = parsed.path.rstrip("/").lower()
    source_url = str(source.get("url") or "").strip()
    if source_url and candidate.rstrip("/") == source_url.rstrip("/"):
        return "article canonical URL is the configured source page"
    if not path:
        return "homepage/root URL"
    matched = next((pattern.pattern for pattern in _NAVIGATION_PATTERNS if pattern.search(parsed.path)), None)
    if matched and not _explicitly_allowed(candidate, source):
        return f"navigation false positive matched {matched}"
    return None


def _db_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    return connection


def _article_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Join article, discovery, source, zoo and run status for acceptance checks."""

    rows = connection.execute(
        """
        SELECT a.id AS article_id, a.canonical_url, a.normalized_url, a.title,
               a.published_at,
               a.content_hash, a.html_hash, a.raw_html, a.metadata_json,
               d.id AS discovery_id, d.discovered_url, d.discovered_at,
               s.id AS source_id, s.url AS source_url, s.kind AS source_kind,
               s.status AS source_status, s.success AS source_success,
               s.last_error AS source_error,
               z.id AS zoo_id, z.slug AS zoo_slug, z.name AS zoo_name
          FROM articles a
          LEFT JOIN article_discoveries d ON d.article_id = a.id
          LEFT JOIN sources s ON s.id = d.source_id
          LEFT JOIN zoos z ON z.id = s.zoo_id
         ORDER BY z.slug, a.published_at DESC, a.created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _article_canonical_sets(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        """
        SELECT DISTINCT z.slug AS zoo_slug, a.canonical_url
          FROM articles a
          JOIN article_discoveries d ON d.article_id = a.id
          JOIN sources s ON s.id = d.source_id
          JOIN zoos z ON z.id = s.zoo_id
         WHERE a.canonical_url IS NOT NULL AND a.canonical_url <> ''
        """
    ).fetchall()
    values: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        values[str(row["zoo_slug"])].add(str(row["canonical_url"]))
    return dict(values)


def _article_total(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM articles").fetchone()
    return int(row[0]) if row else 0


def _duplicate_groups(connection: sqlite3.Connection, column: str) -> list[dict[str, Any]]:
    if column not in {"canonical_url", "normalized_url", "content_hash"}:
        raise ValueError(f"unsupported duplicate column: {column}")
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(articles)")
    }
    if column not in columns:
        return []
    rows = connection.execute(
        f"""SELECT {column} AS value, COUNT(*) AS count
               FROM articles
              WHERE {column} IS NOT NULL AND {column} <> ''
              GROUP BY {column}
             HAVING COUNT(*) > 1
              ORDER BY count DESC, value"""
    ).fetchall()
    return [{"value": row["value"], "count": int(row["count"])} for row in rows]


def _dedup_report(
    connection: sqlite3.Connection,
    *,
    before_total: int,
    after_run1_total: int,
    after_run2_total: int,
    run1_canonicals: set[str],
    run2_canonicals: set[str],
) -> dict[str, Any]:
    run1_new = sorted(run1_canonicals)
    run2_new = sorted(run2_canonicals - run1_canonicals)
    canonical_duplicates = _duplicate_groups(connection, "canonical_url")
    normalized_duplicates = _duplicate_groups(connection, "normalized_url")
    content_hash_duplicates = _duplicate_groups(connection, "content_hash")
    net_run2_increase = max(0, after_run2_total - after_run1_total)
    return {
        "before_total": before_total,
        "after_run1_total": after_run1_total,
        "after_run2_total": after_run2_total,
        "run1_new": len(run1_new),
        "run2_new": len(run2_new),
        "run1_new_canonicals": run1_new,
        "run2_new_canonicals": run2_new,
        "canonical_duplicates": canonical_duplicates,
        "normalized_duplicates": normalized_duplicates,
        "content_hash_duplicates": content_hash_duplicates,
        "canonical_sql": "GROUP BY canonical_url HAVING COUNT(*) > 1",
        "normalized_sql": "GROUP BY normalized_url HAVING COUNT(*) > 1",
        "content_hash_sql": "GROUP BY content_hash HAVING COUNT(*) > 1",
        "increase_only_new_distinct_canonical": net_run2_increase == len(run2_new),
        "no_duplicate_canonical": not canonical_duplicates,
        "no_duplicate_normalized": not normalized_duplicates,
        "no_duplicate_content_hash": not content_hash_duplicates,
        "status": "PASS"
        if not canonical_duplicates
        and not normalized_duplicates
        and not content_hash_duplicates
        and net_run2_increase == len(run2_new)
        else "FAIL",
    }


def _validate_articles(
    connection: sqlite3.Connection,
    config: Any,
    *,
    minimum_per_zoo: int = 3,
) -> dict[str, Any]:
    """Validate live rows without relaxing any domain gate."""

    from zoofan.discovery import URLPolicy

    zoos = {str(z.slug): z for z in getattr(config, "zoos", ()) if getattr(z, "enabled", True)}
    source_models = {str(source.id): source for source in getattr(config, "sources", ())}
    rows = _article_rows(connection)
    source_by_id = {str(row["source_id"]): row for row in rows if row.get("source_id")}
    valid_ids: set[str] = set()
    invalid: list[dict[str, Any]] = []
    valid_by_zoo: Counter[str] = Counter()
    latest_by_zoo: dict[str, dict[str, Any]] = {}
    row_by_article: dict[str, dict[str, Any]] = {}
    for row in rows:
        article_id = str(row.get("article_id") or "")
        if not article_id:
            continue
        row_by_article.setdefault(article_id, row)
        reasons: list[str] = []
        if not str(row.get("title") or "").strip():
            reasons.append("missing title")
        canonical = str(row.get("canonical_url") or "").strip()
        if not _url_ok(canonical):
            reasons.append("canonical URL is not http(s)")
        source_id = str(row.get("source_id") or "")
        source = source_models.get(source_id)
        if source is None:
            reasons.append("discovery has no configured source")
        zoo_slug = str(row.get("zoo_slug") or "")
        zoo = zoos.get(zoo_slug)
        if zoo is None:
            reasons.append("discovery has no valid configured zoo")
        if not row.get("discovery_id") or not row.get("discovered_url"):
            reasons.append("missing article discovery")
        content_hash = str(row.get("content_hash") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            reasons.append("missing or invalid content_hash identity")
        html_hash = str(row.get("html_hash") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", html_hash):
            reasons.append("missing or invalid html_hash SHA-256")
        if not row.get("raw_html"):
            reasons.append("missing raw_html evidence")
        source_status = str(row.get("source_status") or "").lower()
        if not bool(row.get("source_success")) or source_status in {"error", "failed", "failure"}:
            reasons.append("source crawl status is not success")
        if source is not None:
            source_mapping = {
                "url": source.url,
                "config": dict(source.config or {}),
            }
            nav_reason = navigation_false_positive(canonical, source_mapping)
            if nav_reason:
                reasons.append(nav_reason)
            if zoo is not None:
                policy = URLPolicy.from_zoo_source(zoo, source)
                if not policy.accepts(canonical, source_url=source.url):
                    reasons.append("canonical URL violates official/allow policy")
        if reasons:
            invalid.append({"article_id": article_id, "canonical_url": canonical, "reasons": reasons})
            continue
        if article_id in valid_ids:
            continue
        valid_ids.add(article_id)
        valid_by_zoo[zoo_slug] += 1
        latest_by_zoo.setdefault(
            zoo_slug,
            {
                "article_id": article_id,
                "title": str(row.get("title") or ""),
                "canonical_url": canonical,
                "published_at": row.get("published_at"),
            },
        )
    zoo_results: dict[str, dict[str, Any]] = {}
    for slug, zoo in zoos.items():
        zoo_sources = [source for source in getattr(config, "sources", ()) if str(source.zoo_id) == str(zoo.id or slug)]
        source_rows = [
            row for row in source_by_id.values()
            if str(row.get("zoo_id") or "") in {str(zoo.id or slug), slug}
        ]
        official_success = []
        for source in zoo_sources:
            source_url = str(source.url or "")
            host = urlparse(source_url).hostname
            if _url_ok(source_url) and host and any(
                str(host).lower() == str(item).lower().lstrip(".")
                or str(host).lower().endswith("." + str(item).lower().lstrip("."))
                for item in (
                    (source.config or {}).get("official_hosts")
                    or [(source.config or {}).get("official_host")]
                    or [urlparse(str(zoo.website_url or "")).hostname]
                )
                if item
            ):
                matching = [row for row in source_rows if str(row.get("source_id")) == str(source.id)]
                if any(bool(row.get("source_success")) and str(row.get("source_status") or "").lower() not in {"error", "failed"} for row in matching):
                    official_success.append(str(source.id))
        zoo_results[slug] = {
            "source_count": len(zoo_sources),
            "successful_official_sources": official_success,
            "article_count": int(valid_by_zoo.get(slug, 0)),
            "minimum": minimum_per_zoo,
            "status": "PASS" if official_success and valid_by_zoo.get(slug, 0) >= minimum_per_zoo else "FAIL",
            "latest": latest_by_zoo.get(slug),
        }
    return {
        "status": "PASS" if not invalid and all(item["status"] == "PASS" for item in zoo_results.values()) else "FAIL",
        "total_rows": len(row_by_article),
        "valid_articles": len(valid_ids),
        "invalid_articles": invalid,
        "zoos": zoo_results,
    }


def _source_findings(config: Any, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
    configured = []
    by_kind: Counter[str] = Counter()
    for source in getattr(config, "sources", ()):
        kind = str(source.kind or "unknown").lower()
        by_kind[kind] += 1
        configured.append(
            {
                "id": source.id,
                "zoo_id": source.zoo_id,
                "kind": kind,
                "url": source.url,
                "official_http": _url_ok(source.url),
                "enabled": bool(source.enabled),
                "ssr": kind in {"archive", "html", "news_archive"},
                "allow_patterns": list(_patterns({"config": dict(source.config or {})})),
            }
        )
    live = []
    if connection is not None:
        for row in connection.execute("SELECT id, url, kind, status, success, last_http_status, last_error FROM sources ORDER BY id"):
            live.append(dict(row))
    return {
        "configured": configured,
        "by_kind": dict(by_kind),
        "ssr_archive_sources": sum(1 for item in configured if item["ssr"]),
        "rss_sources": by_kind.get("rss", 0) + by_kind.get("atom", 0),
        "sitemap_sources": by_kind.get("sitemap", 0),
        "playwright": {
            "used_for": "dashboard acceptance smoke",
            "conclusion": "crawler source discovery remains HTTP/parser based; browser smoke validates rendered dashboard routes",
        },
        "live": live,
    }


def _validate_enabled_sources(config: Any, connection: sqlite3.Connection) -> dict[str, Any]:
    """Require fresh successful operational evidence for every enabled source."""

    rows = {
        str(row["id"]): dict(row)
        for row in connection.execute(
            "SELECT id, status, success, last_checked, last_success, last_error, last_http_status FROM sources"
        )
    }
    failures: list[str] = []
    sources: dict[str, dict[str, Any]] = {}
    for source in getattr(config, "sources", ()):
        if not getattr(source, "enabled", True):
            continue
        source_id = str(source.id)
        row = rows.get(source_id)
        reasons: list[str] = []
        if row is None:
            reasons.append("missing persisted source evidence")
        else:
            status = str(row.get("status") or "").lower()
            if row.get("success") not in {1, True} or status not in {"completed", "success", "healthy"}:
                reasons.append(f"current status is not successful ({status or 'unknown'})")
            if not row.get("last_checked"):
                reasons.append("missing last_checked")
            if not row.get("last_success"):
                reasons.append("missing last_success")
            if row.get("last_error"):
                reasons.append(f"last_error is set ({row['last_error']})")
            if row.get("last_http_status") is None:
                reasons.append("missing root HTTP status")
        sources[source_id] = {"status": "FAIL" if reasons else "PASS", "reasons": reasons}
        failures.extend(f"enabled source {source_id}: {reason}" for reason in reasons)
    return {"status": "FAIL" if failures else "PASS", "sources": sources, "failures": failures}


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _persisted_errors(value: Any) -> list[str]:
    if value in (None, "", "[]"):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def _validate_run_source_evidence(
    config: Any,
    connection: sqlite3.Connection,
    *,
    run_id: str,
    started_at: Any,
    finished_at: Any,
) -> dict[str, Any]:
    """Bind every enabled source's stat and health evidence to one crawl run."""

    enabled_ids = {
        str(source.id) for source in getattr(config, "sources", ())
        if getattr(source, "enabled", True)
    }
    stat_rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM crawl_run_stats WHERE crawl_run_id=?", (run_id,)
        )
    ]
    stat_ids = [str(row.get("source_id") or "") for row in stat_rows]
    failures: list[str] = []
    for source_id in sorted(enabled_ids - set(stat_ids)):
        failures.append(f"run {run_id}: missing stat for enabled source {source_id}")
    for source_id in sorted(set(stat_ids) - enabled_ids):
        failures.append(f"run {run_id}: unexpected stat source {source_id or 'NULL'}")
    for source_id, count in sorted(Counter(stat_ids).items()):
        if count != 1:
            failures.append(f"run {run_id}: source {source_id or 'NULL'} has {count} stats, expected 1")
    for row in stat_rows:
        source_id = str(row.get("source_id") or "")
        errors = _persisted_errors(row.get("errors_json"))
        if str(row.get("status") or "") != "completed":
            failures.append(
                f"run {run_id}: source {source_id or 'NULL'} stat status is "
                f"{row.get('status')!r}, expected 'completed'"
            )
        if int(row.get("error_count") or 0) != 0 or row.get("error") or errors:
            failures.append(f"run {run_id}: source {source_id or 'NULL'} stat contains errors")

    source_rows = {
        str(row["id"]): dict(row)
        for row in connection.execute(
            "SELECT id, status, success, last_checked, last_success, last_error, last_http_status FROM sources"
        )
    }
    start = _parse_utc_timestamp(started_at)
    finish = _parse_utc_timestamp(finished_at)
    # SQLite timestamps and run objects may differ at microsecond precision;
    # one second permits serialization rounding, not evidence from an old run.
    tolerance_seconds = 1.0
    for source_id in sorted(enabled_ids):
        row = source_rows.get(source_id)
        if row is None:
            failures.append(f"run {run_id}: missing current source row {source_id}")
            continue
        if str(row.get("status") or "") != "completed" or row.get("success") not in {1, True}:
            failures.append(f"run {run_id}: source {source_id} current status is not successful completed")
        if row.get("last_error"):
            failures.append(f"run {run_id}: source {source_id} current last_error is set")
        http_status = row.get("last_http_status")
        if not isinstance(http_status, int) or not 200 <= http_status < 400:
            failures.append(f"run {run_id}: source {source_id} missing successful root HTTP status")
        for field in ("last_checked", "last_success"):
            evidence_time = _parse_utc_timestamp(row.get(field))
            if start is None or finish is None or evidence_time is None:
                failures.append(f"run {run_id}: source {source_id} has invalid {field}")
            elif (
                evidence_time.timestamp() < start.timestamp() - tolerance_seconds
                or evidence_time.timestamp() > finish.timestamp() + tolerance_seconds
            ):
                failures.append(f"run {run_id}: source {source_id} {field} is outside current run window")
    return {
        "status": "FAIL" if failures else "PASS",
        "run_id": run_id,
        "enabled_source_ids": sorted(enabled_ids),
        "stat_source_ids": sorted(stat_ids),
        "failures": failures,
    }


def _failure_isolation() -> dict[str, Any]:
    result = _run_subprocess(
        [sys.executable, "-m", "pytest", "-q", "tests/test_failure_isolation.py"],
        label="failure-isolation",
        timeout=PYTEST_TIMEOUT,
    )
    result.pop("output", None)
    result["test"] = "tests/test_failure_isolation.py"
    return result


def _reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_dashboard(url: str, process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate(timeout=2)
            raise RuntimeError(f"dashboard exited with {process.returncode}: {output}")
        try:
            request = Request(url, headers={"User-Agent": "ZooFanCrawler-endgoal"})
            with urlopen(request, timeout=2) as response:
                if int(response.status) < 500:
                    return
        except Exception as exc:  # continue until deadline; final error is explicit
            last_error = exc
        time.sleep(0.2)
    raise TimeoutError(f"dashboard readiness timeout for {url}: {last_error}")


def _start_dashboard(db_path: Path, port: int) -> subprocess.Popen[str]:
    command = [
        sys.executable,
        "-c",
        "from zoofan.dashboard import main; main()",
    ]
    # The dashboard's normal CLI defaults to another DB/port, so pass values
    # through a tiny, still argument-list based Python launcher.
    command[2] = (
        "from zoofan.dashboard import main; "
        f"main({str(db_path)!r}, '127.0.0.1', {int(port)})"
    )
    print(f"ENDGOAL dashboard START: {command!r}", flush=True)
    return subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _terminate_process(process: subprocess.Popen[str]) -> str:
    if process.poll() is None:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate(timeout=5)
    else:
        output, _ = process.communicate()
    _print_lines("ENDGOAL dashboard OUTPUT:", output)
    return output or ""


def _install_chromium_once() -> dict[str, Any]:
    return _run_subprocess(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        label="playwright-install-chromium",
        timeout=INSTALL_TIMEOUT,
    )


def _close_playwright_handles(page: Any, context: Any, browser: Any) -> None:
    """Close page, context, and browser exactly once while Playwright is live."""

    for handle in (page, context, browser):
        if handle is None:
            continue
        try:
            handle.close()
        except Exception as exc:  # cleanup must not mask the actual smoke result
            LOGGER.debug("Playwright handle cleanup failed: %s", exc)


def _prepare_dashboard_screenshot(path: Path = SCREENSHOT_PATH) -> None:
    """Remove only the exact previous acceptance screenshot before this run."""

    if path.exists():
        path.unlink()


def _rendered_timestamp(value: Any) -> str:
    if value in (None, ""):
        return "—"
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _rendered_exact(rendered: Any, expected: Any, *, timestamp: bool = False) -> bool:
    actual = str(rendered or "").strip()
    if expected in (None, ""):
        return actual in {"—", "Unknown"}
    wanted = _rendered_timestamp(expected) if timestamp else str(expected).strip()
    return actual == wanted


def _discovery_bounds(
    discoveries: Iterable[Mapping[str, Any]], *, has_last_discovered_at: bool
) -> tuple[Any, Any]:
    """Return true first/last discovery evidence, including legacy schemas."""

    rows = list(discoveries)
    first_values = [row.get("discovered_at") for row in rows if row.get("discovered_at")]
    last_values = [
        (row.get("last_discovered_at") if has_last_discovered_at else None)
        or row.get("discovered_at")
        for row in rows
    ]
    last_values = [value for value in last_values if value]
    return min(first_values, default=None), max(last_values, default=None)


def _expected_duplicate_candidates(
    stats: Iterable[Mapping[str, Any]], *, has_persisted_column: bool
) -> int | None:
    """Require persisted counts in current schema; allow unknown for legacy DBs."""

    if not has_persisted_column:
        return None
    return sum(int(row.get("duplicate_candidate_count") or 0) for row in stats)


def _playwright_definition_fields(page: Any) -> dict[str, str]:
    fields: dict[str, str] = {}
    for term in page.locator("dt").all():
        label = _normalise_semantic_label(term.inner_text())
        if label:
            fields[label] = term.locator("xpath=following-sibling::dd[1]").inner_text().strip()
    return fields


def _playwright_table_rows(page: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for table in page.locator("table").all():
        headers = [_normalise_semantic_label(item) for item in table.locator("thead th").all_inner_texts()]
        for row in table.locator("tbody tr").all():
            cells = row.locator("td").all()
            values = [item.inner_text().strip() for item in cells]
            if headers and len(values) == len(headers):
                record = dict(zip(headers, values))
                for header, cell in zip(headers, cells):
                    links = cell.locator("a[href]")
                    if links.count():
                        record[f"{header} href"] = str(links.first.get_attribute("href") or "").strip()
                result.append(record)
    return result


def _dashboard_smoke_in_context(
    playwright: Any,
    base_url: str,
    db_path: Path,
    config: Any,
    console_errors: list[str],
    page_errors: list[str],
    statuses: dict[str, int | None],
    route_bodies: dict[str, str],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Run browser checks and close all browser handles before context exit."""

    browser = None
    context = None
    page = None
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            # Only an executable-missing launch error may trigger the one
            # permitted install attempt. Other Playwright failures are real
            # acceptance failures and must remain visible.
            text = str(exc)
            if not any(token in text.lower() for token in ("executable", "browser", "chromium")):
                raise
            state["playwright_install"] = _install_chromium_once()
            if state["playwright_install"].get("status") != "PASS":
                raise RuntimeError(f"Playwright Chromium install failed: {state['playwright_install']}")
            browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        route_observations: dict[str, dict[str, Any]] = {}
        route_tables: dict[str, list[dict[str, str]]] = {}
        route_fields: dict[str, dict[str, str]] = {}
        run_card_observations: dict[str, dict[str, Any]] = {}
        for route in ("/", "/articles", "/sources", "/runs"):
            response = page.goto(base_url + route, wait_until="networkidle", timeout=60000)
            statuses[route] = response.status if response is not None else None
            body = page.locator("body").inner_text(timeout=10000)
            route_bodies[route] = body
            route_observations[route] = {
                "status": statuses[route],
                "body": body,
                "labels": page.locator("th, dt, label, .metric span").all_inner_texts(),
            }
            route_tables[route] = _playwright_table_rows(page)
            route_fields[route] = _playwright_definition_fields(page)
            if route == "/runs":
                for card in page.locator("article.run-card").all():
                    heading = card.locator("h3").first.inner_text().strip() if card.locator("h3").count() else ""
                    run_id = heading.removeprefix("Run ").strip()
                    run_card_observations[run_id] = {
                        "fields": _playwright_definition_fields(card),
                        "tables": _playwright_table_rows(card),
                        "status": card.locator(".status").first.inner_text().strip() if card.locator(".status").count() else "",
                    }
            if "Internal Server Error" in body or "Traceback (most recent call last)" in body:
                page_errors.append(f"{route}: server error text in body")
            if route == "/":
                Path(SCREENSHOT_PATH).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
                state["screenshot_written_this_run"] = SCREENSHOT_PATH.is_file()
        expected_zoo_counts = _zoo_article_counts(db_path)
        card_names: dict[str, dict[str, str]] = {}
        # Return to overview after route loop for rendered card inspection.
        page.goto(base_url + "/", wait_until="networkidle", timeout=60000)
        home_cards = page.locator("article.status-card")
        for index in range(home_cards.count()):
            card = home_cards.nth(index)
            name = card.locator("h3").inner_text().strip()
            labels = card.locator("dt").all_inner_texts()
            values = card.locator("dd").all_inner_texts()
            card_names[name] = dict(zip(labels, values))
        zoo_checks = {}
        for zoo in getattr(config, "zoos", ()):
            expected = expected_zoo_counts.get(str(zoo.slug), 0)
            panel = card_names.get(str(zoo.name))
            actual = None
            if panel is not None:
                try:
                    actual = int(panel.get("Articles", ""))
                except ValueError:
                    actual = None
            zoo_checks[str(zoo.slug)] = {
                "expected_db_articles": expected,
                "rendered_articles": actual,
                "name_rendered": panel is not None,
                "status": "PASS" if panel is not None and actual == expected else "FAIL",
            }
        valid_articles = _live_article_urls(db_path)
        page.goto(base_url + "/articles", wait_until="networkidle", timeout=60000)
        article_links = page.locator('a[target="_blank"]').evaluate_all(
            "elements => elements.map(element => element.href)"
        )
        official_article_visible = any(url in valid_articles for url in article_links)
        database_matches: dict[str, bool] = {name: False for name in _REQUIRED_DATABASE_MATCHES}
        null_publication = None
        with _db_connection(db_path) as connection:
            discovery_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(article_discoveries)")
            }
            stat_columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(crawl_run_stats)")
            }
            article = connection.execute(
                "SELECT * FROM articles ORDER BY (published_at IS NOT NULL), created_at LIMIT 1"
            ).fetchone()
            run = connection.execute("SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT 1").fetchone()
            discovery_rows = [] if article is None else connection.execute(
                "SELECT * FROM article_discoveries WHERE article_id=? ORDER BY discovered_at",
                (article["id"],),
            ).fetchall()
            source_rows = []
            zoo_rows = []
            if discovery_rows:
                source_ids = [str(item["source_id"]) for item in discovery_rows]
                placeholders = ",".join("?" for _ in source_ids)
                source_rows = connection.execute(
                    f"SELECT * FROM sources WHERE id IN ({placeholders}) ORDER BY id", source_ids
                ).fetchall()
                zoo_ids = sorted({str(item["zoo_id"]) for item in source_rows})
                if zoo_ids:
                    placeholders = ",".join("?" for _ in zoo_ids)
                    zoo_rows = connection.execute(
                        f"SELECT * FROM zoos WHERE id IN ({placeholders}) ORDER BY id", zoo_ids
                    ).fetchall()
            run_stats = [] if run is None else connection.execute(
                "SELECT * FROM crawl_run_stats WHERE crawl_run_id=? ORDER BY zoo_id, source_id",
                (run["id"],),
            ).fetchall()
        if article is not None:
            article_row = dict(article)
            article_id = str(article_row["id"])
            detail_path = f"/articles/{quote(article_id, safe='')}"
            response = page.goto(base_url + detail_path, wait_until="networkidle", timeout=60000)
            detail_body = page.locator("body").inner_text(timeout=10000)
            statuses[detail_path] = response.status if response is not None else None
            route_bodies[detail_path] = detail_body
            route_observations[detail_path] = {
                "status": statuses[detail_path],
                "body": detail_body,
                "labels": page.locator("th, dt, label, .label, .eyebrow").all_inner_texts(),
            }
            detail_fields = _playwright_definition_fields(page)
            eyebrow = page.locator(".eyebrow").first.inner_text().strip() if page.locator(".eyebrow").count() else ""
            title = page.locator("h1").first.inner_text().strip() if page.locator("h1").count() else ""
            sources = [dict(item) for item in source_rows]
            zoos = [dict(item) for item in zoo_rows]
            discoveries = [dict(item) for item in discovery_rows]
            source_names = [str(item.get("name") or item.get("id") or "") for item in sources]
            source_ids = [str(item.get("id") or "") for item in sources]
            zoo_names = [str(item.get("name") or item.get("slug") or item.get("id") or "") for item in zoos]
            first_discovered, last_discovered = _discovery_bounds(
                discoveries,
                has_last_discovered_at="last_discovered_at" in discovery_columns,
            )
            article_list_row = next(
                (
                    item for item in route_tables.get("/articles", [])
                    if item.get("title") == str(article_row.get("title") or "")
                ),
                {},
            )
            rendered_list_source = article_list_row.get("source", article_list_row.get("source id", ""))
            rendered_list_zoo = article_list_row.get("zoo", "")
            canonical_cell = article_list_row.get("canonical url", "")
            canonical_href = article_list_row.get("canonical url href", "")
            canonical = str(article_row.get("canonical_url") or "")
            database_matches.update(
                {
                    "articles.title": article_list_row.get("title") == str(article_row.get("title") or ""),
                    "articles.zoo": rendered_list_zoo in {", ".join(zoo_names), " / ".join(zoo_names)} and bool(zoo_names),
                    "articles.published_at_source": _rendered_exact(article_list_row.get("published at source"), article_row.get("published_at"), timestamp=True),
                    "articles.updated_at_source": _rendered_exact(article_list_row.get("updated at source"), article_row.get("updated_at_source"), timestamp=True),
                    "articles.discovered_at": _rendered_exact(article_list_row.get("discovered at", article_list_row.get("first discovered")), first_discovered, timestamp=True),
                    "articles.source": rendered_list_source in {", ".join(source_names), ", ".join(source_ids)} and bool(source_ids),
                    "articles.language": _rendered_exact(article_list_row.get("language"), article_row.get("language")),
                    "articles.crawl_status": _rendered_exact(article_list_row.get("crawl status"), article_row.get("crawl_status")),
                    "articles.article_http_status": _rendered_exact(article_list_row.get("article http status"), article_row.get("http_status")),
                    "articles.canonical_url": bool(canonical) and canonical in {canonical_cell, canonical_href},
                    "articles.access_link": bool(canonical) and canonical_href == canonical,
                }
            )
            source_rendered = detail_fields.get("source", detail_fields.get("sources", ""))
            zoo_rendered = detail_fields.get("zoo", "")
            database_matches.update(
                {
                    "detail.article_id": detail_fields.get("article id", "") == article_id or eyebrow == f"Article {article_id}",
                    "detail.title": title == str(article_row.get("title") or ""),
                    "detail.zoo": zoo_rendered in {", ".join(zoo_names), " / ".join(zoo_names)} and bool(zoo_names),
                    "detail.sources": source_rendered in {", ".join(source_names), ", ".join(source_ids)} and bool(source_ids),
                    "detail.original_url": _rendered_exact(detail_fields.get("original url"), article_row.get("source_url")),
                    "detail.canonical_url": _rendered_exact(detail_fields.get("canonical url"), article_row.get("canonical_url")),
                    "detail.language": _rendered_exact(detail_fields.get("language"), article_row.get("language")),
                    "detail.article_http_status": _rendered_exact(detail_fields.get("article http status"), article_row.get("http_status")),
                    "detail.crawl_status": _rendered_exact(detail_fields.get("crawl status"), article_row.get("crawl_status")),
                    "detail.html_hash": _rendered_exact(detail_fields.get("html hash"), article_row.get("html_hash")),
                    "detail.published_at_source": _rendered_exact(detail_fields.get("published at source"), article_row.get("published_at"), timestamp=True),
                    "detail.updated_at_source": _rendered_exact(detail_fields.get("updated at source"), article_row.get("updated_at_source"), timestamp=True),
                    "detail.first_discovered": _rendered_exact(detail_fields.get("first discovered"), first_discovered, timestamp=True),
                    "detail.last_discovered": _rendered_exact(detail_fields.get("last discovered"), last_discovered, timestamp=True),
                    "detail.fetched_at": _rendered_exact(detail_fields.get("fetched at"), article_row.get("last_fetched_at"), timestamp=True),
                    "detail.stored_at": _rendered_exact(detail_fields.get("stored at"), article_row.get("created_at"), timestamp=True),
                    "detail.storage_updated_at": _rendered_exact(detail_fields.get("storage updated at"), article_row.get("updated_at"), timestamp=True),
                }
            )
            if article_row.get("published_at") is None:
                null_publication = {
                    "rendered": detail_fields.get("published at source", ""),
                    "storage_created": str(article_row.get("created_at") or ""),
                }
        selected_source = dict(source_rows[0]) if source_rows else None
        if selected_source is not None:
            source_table = next(
                (item for item in route_tables.get("/sources", []) if item.get("source id") == str(selected_source.get("id"))),
                {},
            )
            zoo = next((dict(item) for item in zoo_rows if str(item["id"]) == str(selected_source.get("zoo_id"))), {})
            database_matches.update(
                {
                    "source.zoo": _rendered_exact(source_table.get("zoo"), zoo.get("name")),
                    "source.id": _rendered_exact(source_table.get("source id"), selected_source.get("id")),
                    "source.kind": _rendered_exact(source_table.get("kind", source_table.get("crawl method")), selected_source.get("kind")),
                    "source.language": _rendered_exact(source_table.get("language"), selected_source.get("language")),
                    "source.status": _rendered_exact(source_table.get("status"), selected_source.get("status")),
                    "source.last_checked": _rendered_exact(source_table.get("last check", source_table.get("last checked")), selected_source.get("last_checked"), timestamp=True),
                    "source.http_status": _rendered_exact(source_table.get("http status"), selected_source.get("last_http_status")),
                    "source.last_error": _rendered_exact(source_table.get("last error"), selected_source.get("last_error")),
                }
            )
        if run is not None and selected_source is not None:
            run_row = dict(run)
            zoo_id = str(selected_source.get("zoo_id"))
            relevant_stats = [dict(item) for item in run_stats if str(item["zoo_id"]) == zoo_id]
            expected = {
                "discovered": sum(int(item.get("discovered_count") or 0) for item in relevant_stats),
                "fetched": sum(int(item.get("fetched_count") or 0) for item in relevant_stats),
                "stored": sum(int(item.get("stored_count") or 0) for item in relevant_stats),
                "already known": sum(int(item.get("already_known_count") or 0) for item in relevant_stats),
                "duplicate candidates": _expected_duplicate_candidates(
                    relevant_stats,
                    has_persisted_column="duplicate_candidate_count" in stat_columns,
                ),
                "errors": sum(int(item.get("error_count") or 0) for item in relevant_stats),
            }
            zoo = next((dict(item) for item in zoo_rows if str(item["id"]) == zoo_id), {})
            run_card = run_card_observations.get(str(run_row.get("id")), {})
            run_table = next((item for item in run_card.get("tables", []) if item.get("zoo") == str(zoo.get("name") or "")), {})
            home_panel = card_names.get(str(zoo.get("name") or ""), {})
            duration_value = run_card.get("fields", {}).get("duration", "")
            database_matches.update(
                {
                    "run.duration": run_row.get("duration_ms") is not None and duration_value in {str(run_row.get("duration_ms")), f"{run_row.get('duration_ms')} ms"},
                    "run.start": _rendered_exact(run_card.get("fields", {}).get("start", run_card.get("fields", {}).get("started")), run_row.get("started_at"), timestamp=True),
                    "run.end": _rendered_exact(run_card.get("fields", {}).get("end", run_card.get("fields", {}).get("finished")), run_row.get("finished_at"), timestamp=True),
                    "run.status": _rendered_exact(run_card.get("status"), run_row.get("status")),
                    "run.discovered": _rendered_exact(run_table.get("discovered"), expected["discovered"]),
                    "run.fetched": _rendered_exact(run_table.get("fetched"), expected["fetched"]),
                    "run.stored": _rendered_exact(run_table.get("stored", run_table.get("stored new")), expected["stored"]),
                    "run.already_known": _rendered_exact(run_table.get("already known skipped", run_table.get("already known")), expected["already known"]),
                    "run.duplicate_candidates": _rendered_exact(run_table.get("duplicate candidates"), expected["duplicate candidates"]),
                    "run.errors": _rendered_exact(run_table.get("errors"), expected["errors"]),
                    "home.stored": _rendered_exact(home_panel.get("Stored", home_panel.get("Stored/new")), expected["stored"]),
                    "home.already_known": _rendered_exact(home_panel.get("Already known / skipped", home_panel.get("Already known")), expected["already known"]),
                    "home.duplicate_candidates": _rendered_exact(home_panel.get("Duplicate candidates"), expected["duplicate candidates"]),
                    "home.errors": _rendered_exact(home_panel.get("Errors"), expected["errors"]),
                }
            )
            database_matches["metrics.home_runs_consistent"] = all(
                database_matches[name]
                for name in (
                    "run.stored", "run.already_known", "run.duplicate_candidates", "run.errors",
                    "home.stored", "home.already_known", "home.duplicate_candidates", "home.errors",
                )
            )

        browser_assertions = []
        if len(getattr(config, "zoos", ())) != 3:
            browser_assertions.append("configured zoo count is not three")
        if not all(item["status"] == "PASS" for item in zoo_checks.values()):
            browser_assertions.append("overview zoo article counts do not match database")
        if not valid_articles or not official_article_visible:
            browser_assertions.append("no database-backed official article link is visible")
        if state.get("screenshot_written_this_run") is not True:
            browser_assertions.append("overview screenshot was not created")
        semantic = evaluate_dashboard_observation(
            {
                "routes": route_observations,
                "database_matches": database_matches,
                "null_publication": null_publication,
                "browser_assertion_errors": browser_assertions,
                "console_errors": console_errors,
                "page_errors": page_errors,
                "screenshot_written_this_run": state.get("screenshot_written_this_run", False),
            }
        )
        return {
            "status": semantic["status"],
            "failures": semantic["failures"],
            "base_url": base_url,
            "routes": statuses,
            "three_zoos": len(getattr(config, "zoos", ())) == 3,
            "zoo_article_counts": zoo_checks,
            "db_live_article_count": len(valid_articles),
            "official_article_visible": official_article_visible,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "observed_routes": route_observations,
            "database_matches": database_matches,
            "null_publication": null_publication,
            "screenshot": str(SCREENSHOT_PATH),
            "screenshot_written_this_run": state.get("screenshot_written_this_run", False),
            "playwright_install": state.get("playwright_install"),
        }
    finally:
        # This finally executes inside ``with sync_playwright()``. Never close
        # a browser after that context exits: it causes "Event loop is closed"
        # on Playwright's sync driver.
        _close_playwright_handles(page, context, browser)


def _dashboard_browser_smoke(base_url: str, db_path: Path, config: Any) -> dict[str, Any]:
    """Exercise all dashboard routes in a real Playwright browser."""

    _prepare_dashboard_screenshot()

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"status": "FAIL", "error": f"Playwright import failed: {type(exc).__name__}: {exc}"}

    console_errors: list[str] = []
    page_errors: list[str] = []
    statuses: dict[str, int | None] = {}
    route_bodies: dict[str, str] = {}
    state: dict[str, Any] = {"playwright_install": None, "screenshot_written_this_run": False}
    try:
        with sync_playwright() as playwright:
            return _dashboard_smoke_in_context(
                playwright,
                base_url,
                db_path,
                config,
                console_errors,
                page_errors,
                statuses,
                route_bodies,
                state,
            )
    except Exception as exc:
        return {
            "status": "FAIL",
            "base_url": base_url,
            "routes": statuses,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "playwright_install": state.get("playwright_install"),
            "error": f"{type(exc).__name__}: {exc}",
        }


def _zoo_article_counts(db_path: Path) -> dict[str, int]:
    with _db_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT z.slug, COUNT(DISTINCT a.id) AS count
              FROM articles a
              JOIN article_discoveries d ON d.article_id = a.id
              JOIN sources s ON s.id = d.source_id
              JOIN zoos z ON z.id = s.zoo_id
             GROUP BY z.slug
            """
        ).fetchall()
        return {str(row["slug"]): int(row["count"]) for row in rows}


def _live_article_urls(db_path: Path) -> set[str]:
    with _db_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT a.canonical_url
              FROM articles a
              JOIN article_discoveries d ON d.article_id = a.id
              JOIN sources s ON s.id = d.source_id
             WHERE a.title IS NOT NULL AND TRIM(a.title) <> ''
               AND a.canonical_url LIKE 'http%'
               AND a.content_hash IS NOT NULL
               AND a.html_hash IS NOT NULL
               AND a.raw_html IS NOT NULL
               AND COALESCE(s.success, 0) = 1
            """
        ).fetchall()
        return {str(row["canonical_url"]) for row in rows}


def _empty_zoo_report(config: Any) -> dict[str, dict[str, Any]]:
    return {
        str(zoo.slug): {
            "name": str(zoo.name),
            "sources": [],
            "source_count": 0,
            "articles": 0,
            "latest": None,
            "crawl1_new": 0,
            "crawl2_new": 0,
            "errors": [],
            "status": "FAIL",
        }
        for zoo in getattr(config, "zoos", ())
    }


def _run_live_acceptance(config: Any, connection: sqlite3.Connection) -> dict[str, Any]:
    from zoofan.crawler import Crawler

    crawler = Crawler(config, storage=None)
    # Bind the fresh storage explicitly so the same DB is used for both runs.
    from zoofan.storage import SQLiteStorage

    storage = SQLiteStorage(DB_PATH)
    crawler.storage = storage
    try:
        before_total = _article_total(connection)
        before_sets = _article_canonical_sets(connection)
        print("ENDGOAL LIVE CRAWL 1 START", flush=True)
        result1 = crawler.crawl("all")
        run1_source_validation = _validate_run_source_evidence(
            config,
            connection,
            run_id=str(getattr(result1.run, "id", "") or ""),
            started_at=getattr(result1.run, "started_at", None),
            finished_at=getattr(result1.run, "finished_at", None),
        )
        after_run1_total = _article_total(connection)
        run1_sets = _article_canonical_sets(connection)
        print(f"ENDGOAL LIVE CRAWL 1 {result1.status}", flush=True)
        print("ENDGOAL LIVE CRAWL 2 START", flush=True)
        result2 = crawler.crawl("all")
        run2_source_validation = _validate_run_source_evidence(
            config,
            connection,
            run_id=str(getattr(result2.run, "id", "") or ""),
            started_at=getattr(result2.run, "started_at", None),
            finished_at=getattr(result2.run, "finished_at", None),
        )
        after_run2_total = _article_total(connection)
        run2_sets = _article_canonical_sets(connection)
        print(f"ENDGOAL LIVE CRAWL 2 {result2.status}", flush=True)
        all_canonicals_before = set().union(*before_sets.values()) if before_sets else set()
        all_canonicals_run1 = set().union(*run1_sets.values()) if run1_sets else set()
        all_canonicals_run2 = set().union(*run2_sets.values()) if run2_sets else set()
        validation = _validate_articles(connection, config)
        source_validation = _validate_enabled_sources(config, connection)
        dedup = _dedup_report(
            connection,
            before_total=before_total,
            after_run1_total=after_run1_total,
            after_run2_total=after_run2_total,
            run1_canonicals=all_canonicals_run1 - all_canonicals_before,
            run2_canonicals=all_canonicals_run2,
        )
        zoos = _empty_zoo_report(config)
        for slug, item in validation["zoos"].items():
            per_zoo_run1 = run1_sets.get(slug, set()) - before_sets.get(slug, set())
            per_zoo_run2 = run2_sets.get(slug, set()) - run1_sets.get(slug, set())
            source_rows = [
                source for source in getattr(config, "sources", ()) if str(source.zoo_id) in {slug, str(next((z.id for z in config.zoos if z.slug == slug), slug))}
            ]
            errors = []
            for stat in (result1.stats + result2.stats):
                if str(stat.zoo_id) in {slug, str(next((z.id for z in config.zoos if z.slug == slug), slug))}:
                    errors.extend(stat.errors or ([stat.error] if stat.error else []))
            zoos[slug].update(
                {
                    "name": next((z.name for z in config.zoos if z.slug == slug), slug),
                    "sources": [
                        {
                            "id": source.id,
                            "kind": source.kind,
                            "url": source.url,
                        }
                        for source in source_rows
                    ],
                    "source_count": item["source_count"],
                    "articles": item["article_count"],
                    "latest": item["latest"],
                    "crawl1_new": len(per_zoo_run1),
                    "crawl2_new": len(per_zoo_run2),
                    "errors": errors,
                    "status": item["status"],
                }
            )
        run_failures: list[str] = []
        for label, result in (("run1", result1), ("run2", result2)):
            if result.status != "completed":
                run_failures.append(f"{label}: status is {result.status}, expected completed")
            for stat in result.stats:
                if stat.error_count or stat.error or stat.errors:
                    run_failures.append(
                        f"{label}: source {stat.source_id or 'unknown'} recorded errors"
                    )
        failures = [
            *source_validation["failures"],
            *run1_source_validation["failures"],
            *run2_source_validation["failures"],
            *run_failures,
        ]
        return {
            "status": "PASS" if not failures and validation["status"] == "PASS" and dedup["status"] == "PASS" else "FAIL",
            "failures": failures,
            "run1": result1.as_dict(),
            "run2": result2.as_dict(),
            "validation": validation,
            "source_validation": source_validation,
            "run_source_validation": {
                "run1": run1_source_validation,
                "run2": run2_source_validation,
            },
            "dedup": dedup,
            "zoos": zoos,
        }
    finally:
        storage.close()


def main() -> int:
    started_at = _now()
    report: dict[str, Any] = {
        "status": "FAIL",
        "started_at": started_at,
        "finished_at": None,
        "database": str(DB_PATH),
        "artifacts": {"report": str(REPORT_PATH), "dashboard_screenshot": str(SCREENSHOT_PATH)},
        "unit_tests": None,
        "zoos": {},
        "dedup": None,
        "failure_isolation": None,
        "dashboard": None,
        "sources_findings": None,
        "errors": [],
    }
    config = None
    connection = None
    try:
        report["unit_tests"] = _run_subprocess(
            [sys.executable, "-m", "pytest", "-q"], label="unit-tests", timeout=PYTEST_TIMEOUT
        )
        from zoofan.config import load_config

        config = load_config(CONFIG_PATH)
        report["zoos"] = _empty_zoo_report(config)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        # This is intentionally the sole destructive operation in this module:
        # only the acceptance database and its two explicit SQLite sidecars.
        for database_path in (
            DB_PATH,
            Path(f"{DB_PATH}-wal"),
            Path(f"{DB_PATH}-shm"),
        ):
            if database_path.exists():
                database_path.unlink()
        from zoofan.storage import SQLiteStorage

        with SQLiteStorage(DB_PATH):
            pass
        connection = _db_connection(DB_PATH)
        live = _run_live_acceptance(config, connection)
        report.update(
            {
                "zoos": live["zoos"],
                "dedup": live["dedup"],
                "live": live,
                "sources_findings": _source_findings(config, connection),
            }
        )
        report["failure_isolation"] = _failure_isolation()
        port = _reserve_port()
        process = _start_dashboard(DB_PATH, port)
        dashboard_result: dict[str, Any]
        try:
            base_url = f"http://127.0.0.1:{port}"
            _wait_for_dashboard(base_url + "/", process, READINESS_TIMEOUT)
            dashboard_result = _dashboard_browser_smoke(base_url, DB_PATH, config)
        except Exception as exc:
            dashboard_result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            _terminate_process(process)
        report["dashboard"] = dashboard_result
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        report["errors"].append(message)
        print(f"ENDGOAL ERROR {message}", flush=True)
    finally:
        if connection is not None:
            connection.close()
        report["finished_at"] = _now()
        exit_code, final_lines = finalize_endgoal_report(report)
        report["status"] = "PASS" if exit_code == 0 else "FAIL"
        try:
            _atomic_write_json(REPORT_PATH, report)
        except Exception as exc:
            report["status"] = "FAIL"
            report["errors"].append(f"report write failed: {type(exc).__name__}: {exc}")
            print(f"ENDGOAL ERROR report write failed: {exc}", flush=True)
            exit_code, final_lines = finalize_endgoal_report(report)
        print("ENDGOAL HUMAN REPORT", flush=True)
        print(f"status={report['status']} database={DB_PATH}", flush=True)
        for slug, zoo in report.get("zoos", {}).items():
            print(
                f"zoo={slug} sources={zoo.get('source_count', 0)} articles={zoo.get('articles', 0)} "
                f"crawl1_new={zoo.get('crawl1_new', 0)} crawl2_new={zoo.get('crawl2_new', 0)} "
                f"errors={len(zoo.get('errors', []))}",
                flush=True,
            )
        exit_code = emit_final_endgoal_result(report)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "ARTIFACTS_DIR",
    "DB_PATH",
    "REPORT_PATH",
    "SCREENSHOT_PATH",
    "_atomic_write_json",
    "_dedup_report",
    "_human_banner",
    "_validate_articles",
    "evaluate_dashboard_observation",
    "emit_final_endgoal_result",
    "finalize_endgoal_report",
    "navigation_false_positive",
    "main",
]
