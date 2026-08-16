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
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "zoos.yaml"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "acceptance.db"
ARTIFACTS_DIR = ROOT / "artifacts"
REPORT_PATH = ARTIFACTS_DIR / "endgoal-report.json"
SCREENSHOT_PATH = ARTIFACTS_DIR / "dashboard-acceptance.png"
REPORTS_DIR = ROOT / "reports"
STATIC_REPORT_SCREENSHOT_NAME = "static-acceptance.png"

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

# These labels are part of the self-contained report's acceptance contract.
# Keep the check here (rather than coupling it to a template implementation)
# so a browser observation can be evaluated deterministically in unit tests.
_STATIC_REPORT_REQUIRED_METRICS: tuple[str, ...] = (
    "Configured zoos",
    "Enabled zoos",
    "Configured sources",
    "Enabled source checks",
    "Unique articles (cumulative)",
    "Source/article associations",
    "Discovered (latest run)",
    "Fetched (latest run)",
    "Parsed (latest run)",
    "Stored (latest run)",
    "Inserted (latest run)",
    "Updated (latest run)",
    "Already known (latest run)",
    "Duplicate filtered (latest run)",
    "Errors (latest run)",
    "Warnings",
    "Latest-run result coverage",
    "Wall duration (latest run)",
)
_STATIC_REPORT_REQUIRED_CATEGORIES: tuple[str, ...] = (
    "SUPPORTED",
    "SUPPORTED_NO_CURRENT_NEWS",
    "PARTIALLY_SUPPORTED",
    "SOURCE_NOT_FOUND",
    "ROBOTS_DISALLOWED",
    "JAVASCRIPT_REQUIRED",
    "BLOCKED",
    "UNSUPPORTED",
    "FAILED",
)
_STATIC_REPORT_REQUIRED_TABLE_COLUMNS: tuple[str, ...] = (
    "Name",
    "Country",
    "Region",
    "Groups",
    "Official site",
    "News source URL",
    "Adapter",
    "Completion",
    "Run status",
    "HTTP",
    "Discovered",
    "Parsed",
    "Inserted",
    "Failed",
    "Latest errors",
    "Latest news date",
    "Duration",
    "Error summary",
)
_STATIC_REPORT_REQUIRED_CONTROLS: tuple[str, ...] = (
    "country-filter",
    "group-filter",
    "status-filter",
    "name-filter",
    "sort-filter",
)

# This is the public vocabulary emitted by both the live endgoal report and
# the backend-free acceptance report.  Keep the mapping here independent of
# presentation code: the live gate must classify persisted raw crawl evidence
# even when the report builder is unavailable.
PUBLIC_COMPLETION_CLASSIFICATIONS: tuple[str, ...] = _STATIC_REPORT_REQUIRED_CATEGORIES

# These three configured zoos are the deliberately small historical golden
# regression set.  External limitations may relax minimum article quantity
# for the expanded roster, but never for these regression fixtures.
_GOLDEN_ZOO_IDS = frozenset({"zoo-berlin", "koeln-zoo", "gaiazoo"})

_PUBLIC_CLASSIFICATION_BY_RAW: dict[str, str] = {
    "success": "SUPPORTED",
    "success_no_items": "SUPPORTED_NO_CURRENT_NEWS",
    "partial": "PARTIALLY_SUPPORTED",
    "source_not_found": "SOURCE_NOT_FOUND",
    "robots_disallowed": "ROBOTS_DISALLOWED",
    "javascript_required": "JAVASCRIPT_REQUIRED",
    "blocked": "BLOCKED",
    "unsupported": "UNSUPPORTED",
    "failed": "FAILED",
    "parse_error": "FAILED",
    "date_parse_error": "FAILED",
    "content_parse_error": "FAILED",
    "configuration_error": "FAILED",
    "http_error": "FAILED",
    "timeout": "FAILED",
    "dns_error": "FAILED",
    "tls_error": "FAILED",
}

# A known partial result is an honest, nonblocking outcome in its own right.
# Unknown categories and parser/config/date/content failures remain blocking.
_EXTERNAL_RAW_CATEGORIES = frozenset(
    {
        "source_not_found",
        "robots_disallowed",
        "javascript_required",
        "blocked",
        "unsupported",
    }
)
_BLOCKING_RAW_CATEGORIES = frozenset(
    {
        "failed",
        "parse_error",
        "date_parse_error",
        "content_parse_error",
        "configuration_error",
        "http_error",
        "timeout",
        "dns_error",
        "tls_error",
    }
)

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


def _static_interaction_ok(value: Any) -> bool:
    """Return whether one static-report interaction produced valid evidence."""

    if isinstance(value, Mapping):
        return value.get("ok") is True
    return value is True


def evaluate_static_report_observation(
    observation: Mapping[str, Any],
    *,
    expected_zoo_count: int | None = None,
    expected_scope_label: str | None = None,
) -> dict[str, Any]:
    """Evaluate browser evidence for the backend-free acceptance report.

    The browser collector intentionally does not decide whether an incomplete
    page is acceptable.  It records concrete values and interaction outcomes;
    this pure function turns that evidence into named failures suitable for
    deterministic tests and the durable endgoal report.
    """

    failures: list[str] = []
    title = str(observation.get("title") or "").strip()
    if title != "ZooFanCrawler acceptance":
        failures.append(f"static report title: expected 'ZooFanCrawler acceptance', got {title!r}")

    scope_banner = str(observation.get("scope_banner") or "").strip()
    scope_normalized = _normalise_semantic_label(scope_banner)
    if "scope" not in scope_normalized:
        failures.append("static report scope banner: configured registry scope is not visible")
    expanded_roster_available = observation.get("expanded_roster_available")
    if not isinstance(expanded_roster_available, bool):
        failures.append(
            "static report scope banner: expanded-roster availability evidence is unavailable"
        )
    elif expanded_roster_available:
        if "expanded roster available" not in scope_normalized:
            failures.append(
                "static report scope banner: expected expanded-roster availability is not visible"
            )
    else:
        if "configured" not in scope_normalized:
            failures.append("static report scope banner: configured registry scope is not visible")
        if "no expanded roster" not in scope_normalized:
            failures.append(
                "static report scope banner: configured report does not state that no expanded roster is implied"
            )
    scope_label = expected_scope_label or observation.get("expected_scope_label")
    if scope_label and _normalise_semantic_label(scope_label) not in scope_normalized:
        failures.append(
            f"static report scope banner: missing configured scope label {str(scope_label)!r}"
        )
    expected_count_value = expected_zoo_count
    if expected_count_value is None:
        raw_expected = observation.get("expected_zoo_count")
        try:
            expected_count_value = int(raw_expected) if raw_expected is not None else None
        except (TypeError, ValueError):
            expected_count_value = None
    row_count = observation.get("row_count", observation.get("initial_row_count"))
    if expected_count_value is None:
        failures.append("static report table: expected enabled-zoo row count is unavailable")
    else:
        try:
            actual_count = int(row_count)
        except (TypeError, ValueError):
            actual_count = None
        if actual_count != expected_count_value:
            failures.append(
                f"static report table: expected {expected_count_value} enabled-zoo rows, got {row_count!r}"
            )

    controls = observation.get("controls")
    controls = controls if isinstance(controls, Mapping) else {}
    for control in _STATIC_REPORT_REQUIRED_CONTROLS:
        if controls.get(control) is not True:
            failures.append(f"static report controls: missing required control '{control}'")

    interactions = {
        "search": observation.get("search"),
        "filters": observation.get("filters"),
        "sort": observation.get("sort"),
        "detail": observation.get("detail"),
    }
    for name, evidence in interactions.items():
        if not _static_interaction_ok(evidence):
            failures.append(f"static report interaction: {name} did not produce valid evidence")

    metric_labels = {
        _normalise_semantic_label(label)
        for label in observation.get("metric_labels", ()) or ()
        if _normalise_semantic_label(label)
    }
    missing_metrics = [
        label
        for label in _STATIC_REPORT_REQUIRED_METRICS
        if _normalise_semantic_label(label) not in metric_labels
    ]
    if missing_metrics:
        failures.append("static report metrics: missing " + ", ".join(missing_metrics))

    category_values = observation.get("category_labels", ())
    if isinstance(category_values, str):
        category_text = _normalise_semantic_label(category_values)
    else:
        category_text = " ".join(
            _normalise_semantic_label(value)
            for value in category_values or ()
            if _normalise_semantic_label(value)
        )
    missing_categories = [
        category
        for category in _STATIC_REPORT_REQUIRED_CATEGORIES
        if _normalise_semantic_label(category) not in category_text
    ]
    if missing_categories:
        failures.append("static report categories: missing " + ", ".join(missing_categories))

    table_columns = {
        _normalise_semantic_label(label)
        for label in observation.get("table_columns", ()) or ()
        if _normalise_semantic_label(label)
    }
    missing_table_columns = [
        label
        for label in _STATIC_REPORT_REQUIRED_TABLE_COLUMNS
        if _normalise_semantic_label(label) not in table_columns
    ]
    if missing_table_columns:
        failures.append(
            "static report table columns: missing " + ", ".join(missing_table_columns)
        )

    if observation.get("raw_html_found") is True:
        failures.append("static report safety: raw_html is present in the rendered report")
    for error in observation.get("browser_assertion_errors", ()) or ():
        failures.append(f"static report assertion: {error}")
    for error in observation.get("page_errors", ()) or ():
        failures.append(f"static report page error: {error}")
    for error in observation.get("console_errors", ()) or ():
        failures.append(f"static report console error: {error}")

    overflow = observation.get("horizontal_overflow")
    overflow = overflow if isinstance(overflow, Mapping) else {}
    for viewport in ("desktop", "mobile"):
        if viewport not in overflow:
            failures.append(f"static report layout: missing {viewport} overflow evidence")
        elif overflow[viewport] is not False:
            failures.append(f"static report layout: horizontal overflow at {viewport} viewport")

    expected_generation = observation.get("expected_generation_id")
    rendered_generation = observation.get("generation_id")
    if expected_generation and rendered_generation != expected_generation:
        failures.append(
            "static report generation: browser opened a different generation than the freshly built report"
        )
    if observation.get("screenshot_requested") and observation.get("screenshot_written_this_run") is not True:
        failures.append("static report screenshot: not written during current browser run")

    return {"status": "PASS" if not failures else "FAIL", "failures": failures}


def finalize_endgoal_report(report: Mapping[str, Any]) -> tuple[int, list[str]]:
    """Return the only authoritative exit code and final human-report lines."""

    failures: list[str] = []
    gate_names: tuple[str, ...] = ("unit_tests", "live", "dedup", "failure_isolation", "dashboard")
    # Keep compatibility with callers that finalize a legacy dashboard-only
    # report, while making the static report mandatory for current endgoal
    # invocations (``main`` always includes this key).
    if "static_report" in report:
        gate_names = (*gate_names, "static_report")
    for name in gate_names:
        gate = report.get(name)
        if not isinstance(gate, Mapping) or gate.get("status") != "PASS":
            failures.append(f"gate failed: {name}")
            if isinstance(gate, Mapping) and gate.get("error"):
                failures.append(f"{name}: {gate['error']}")
    for gate_name in ("dashboard", "static_report"):
        gate = report.get(gate_name)
        if isinstance(gate, Mapping):
            failures.extend(str(item) for item in gate.get("failures", ()) or ())
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
    if column not in {"canonical_url", "normalized_url", "content_hash", "content_identity_key"}:
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


def _content_identity_duplicates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return duplicate parsed-content identities, scoped by normalized title.

    ``content_hash`` is evidence and intentionally not unique by itself.  The
    storage identity is the hash together with the normalized title.  Compute
    that pair here so this audit remains useful for legacy databases which do
    not yet have the derived ``content_identity_key`` column, while current
    schemas can still expose the same semantic check.
    """

    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(articles)")}
    if not {"content_hash", "title"}.issubset(columns):
        return []
    rows = connection.execute(
        "SELECT content_hash, title FROM articles "
        "WHERE content_hash IS NOT NULL AND content_hash <> ''"
    ).fetchall()
    groups: dict[tuple[str, str], int] = Counter()
    for row in rows:
        content_hash = str(row["content_hash"] or "")
        title = re.sub(
            r"\s+", " ", unicodedata.normalize("NFKC", str(row["title"] or "")).casefold()
        ).strip()
        if content_hash and title:
            groups[(content_hash, title)] += 1
    duplicate_groups = [
        {"value": f"{content_hash}\x00{title}", "count": count}
        for (content_hash, title), count in sorted(groups.items())
        if count > 1
    ]
    if "content_identity_key" in columns:
        duplicate_groups.extend(_duplicate_groups(connection, "content_identity_key"))
    return duplicate_groups


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
    content_identity_duplicates = _content_identity_duplicates(connection)
    net_run2_increase = max(0, after_run2_total - after_run1_total)
    warnings = []
    if content_hash_duplicates:
        warnings.append(
            "content_hash collisions are non-unique evidence; content identity is hash plus normalized title"
        )
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
        "content_identity_duplicates": content_identity_duplicates,
        "content_hash_collision_warning": bool(content_hash_duplicates),
        "warnings": warnings,
        "canonical_sql": "GROUP BY canonical_url HAVING COUNT(*) > 1",
        "normalized_sql": "GROUP BY normalized_url HAVING COUNT(*) > 1",
        "content_hash_sql": "GROUP BY content_hash HAVING COUNT(*) > 1",
        "content_identity_sql": "GROUP BY content_hash, normalized title HAVING COUNT(*) > 1",
        "increase_only_new_distinct_canonical": net_run2_increase == len(run2_new),
        "no_duplicate_canonical": not canonical_duplicates,
        "no_duplicate_normalized": not normalized_duplicates,
        "no_duplicate_content_hash": not content_hash_duplicates,
        "no_duplicate_content_identity": not content_identity_duplicates,
        "status": "PASS"
        if not canonical_duplicates
        and not normalized_duplicates
        and not content_identity_duplicates
        and net_run2_increase == len(run2_new)
        else "FAIL",
    }


def _enabled_configured_zoos(config: Any) -> tuple[Any, ...]:
    """Return the configured zoo set that this acceptance run must cover."""

    return tuple(
        zoo for zoo in getattr(config, "zoos", ()) if _config_enabled(getattr(zoo, "enabled", True))
    )


def _config_enabled(value: Any) -> bool:
    """Interpret configuration booleans consistently for acceptance gates."""

    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "disabled"}
    return bool(value)


def _zoo_identifier(zoo: Any) -> str:
    """Return the persisted identity used by crawl results and run metadata."""

    return str(getattr(zoo, "id", None) or getattr(zoo, "slug", None) or "").strip()


def _enabled_configured_sources(config: Any) -> tuple[Any, ...]:
    """Return every enabled source whose run evidence acceptance must cover."""

    return tuple(
        source
        for source in getattr(config, "sources", ())
        if _config_enabled(getattr(source, "enabled", True))
    )


def _validate_live_registry(config: Any) -> dict[str, Any]:
    """Validate roster membership while reporting source execution scope.

    Configured roster coverage and executable source coverage are deliberately
    separate: an enabled zoo with no enabled source is still part of the
    required result roster and is expected to receive one external result.
    """

    zoos = _enabled_configured_zoos(config)
    sources = _enabled_configured_sources(config)
    configured_zoos = tuple(getattr(config, "zoos", ()))
    configured_sources = tuple(getattr(config, "sources", ()))
    zoo_ids = [_zoo_identifier(zoo) for zoo in zoos]
    source_ids = [str(getattr(source, "id", "") or "").strip() for source in sources]
    failures: list[str] = []
    if not zoos:
        failures.append("acceptance registry has no enabled configured zoos")
    if not sources:
        failures.append("acceptance registry has no enabled configured sources")
    if any(not identifier for identifier in zoo_ids):
        failures.append("acceptance registry has enabled zoo(s) without an ID")
    duplicate_zoo_ids = sorted(
        identifier for identifier, count in Counter(zoo_ids).items() if identifier and count > 1
    )
    if duplicate_zoo_ids:
        failures.append(
            "acceptance registry has duplicate enabled zoo IDs: "
            + ", ".join(duplicate_zoo_ids)
        )
    if any(not identifier for identifier in source_ids):
        failures.append("acceptance registry has enabled source(s) without an ID")
    duplicate_source_ids = sorted(
        identifier for identifier, count in Counter(source_ids).items() if identifier and count > 1
    )
    if duplicate_source_ids:
        failures.append(
            "acceptance registry has duplicate enabled source IDs: "
            + ", ".join(duplicate_source_ids)
        )
    no_enabled_sources = sorted(
        _zoo_identifier(zoo)
        for zoo in zoos
        if not any(
            str(getattr(source, "zoo_id", "") or "").strip()
            in {_zoo_identifier(zoo), str(getattr(zoo, "slug", "") or "").strip()}
            for source in sources
        )
    )
    provenance_count = sum(
        1
        for zoo in zoos
        if bool(getattr(zoo, "list_provenance", None))
        or bool(getattr(zoo, "provenance", None))
    )
    provenance_declared = any(
        bool(getattr(zoo, "list_provenance", None))
        or bool(getattr(zoo, "provenance", None))
        for zoo in configured_zoos
    )
    if provenance_declared and provenance_count != len(zoos):
        failures.append(
            "acceptance registry roster provenance is incomplete: "
            f"{provenance_count}/{len(zoos)} enabled zoos have provenance"
        )
    enabled_zoo_ids = sorted(identifier for identifier in zoo_ids if identifier)
    enabled_source_ids = sorted(identifier for identifier in source_ids if identifier)
    return {
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "enabled_zoo_ids": enabled_zoo_ids,
        "enabled_source_ids": enabled_source_ids,
        "configured_roster_count": len(configured_zoos),
        "enabled_roster_count": len(zoos),
        "roster_membership_count": len(enabled_zoo_ids),
        "roster_membership_coverage": len(enabled_zoo_ids) == len(zoos),
        "roster_provenance_count": provenance_count,
        "roster_provenance_coverage": provenance_count == len(zoos),
        "roster_provenance_declared": provenance_declared,
        "configured_source_count": len(configured_sources),
        "enabled_source_count": len(sources),
        "executable_source_count": len(sources),
        "disabled_source_ids": sorted(
            str(getattr(source, "id", "") or "").strip()
            for source in configured_sources
            if not _config_enabled(getattr(source, "enabled", True))
            and str(getattr(source, "id", "") or "").strip()
        ),
        "zoos_without_enabled_sources": no_enabled_sources,
    }


def _validate_articles(
    connection: sqlite3.Connection,
    config: Any,
    *,
    minimum_per_zoo: int = 3,
    accepted_zoo_classifications: Mapping[str, Any] | None = None,
    accepted_source_classifications: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate live rows while distinguishing external limitations.

    Article content, identity, date and URL checks remain unconditional.  A
    zoo/source may only bypass the minimum/article-health requirement when a
    current run supplied an explicit nonblocking public classification and the
    structural run evidence has already been validated separately.
    """

    from zoofan.discovery import URLPolicy

    enabled_zoos = _enabled_configured_zoos(config)
    rows = _article_rows(connection)
    if not enabled_zoos:
        return {
            "status": "FAIL",
            "total_rows": 0,
            "valid_articles": 0,
            "invalid_articles": [],
            "zoos": {},
            "failures": ["acceptance registry has no enabled configured zoos"],
        }

    zoos = {str(z.slug): z for z in enabled_zoos}
    source_models = {str(source.id): source for source in getattr(config, "sources", ())}
    accepted_zoo_classifications = accepted_zoo_classifications or {}
    accepted_source_classifications = accepted_source_classifications or {}
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
        source_override = accepted_source_classifications.get(source_id)
        source_external = (
            source_override is not None
            and _public_classification_info(source_override)["external"]
        )
        if (
            not bool(row.get("source_success")) or source_status in {"error", "failed", "failure"}
        ) and not source_external:
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
        zoo_identifier = _zoo_identifier(zoo)
        zoo_ids = {str(zoo.id or ""), slug}
        zoo_sources = [
            source
            for source in getattr(config, "sources", ())
            if _config_enabled(getattr(source, "enabled", True)) and str(source.zoo_id) in zoo_ids
        ]
        source_rows = [
            row for row in source_by_id.values()
            if str(row.get("zoo_id") or "") in zoo_ids
        ]
        official_success = []
        for source in zoo_sources:
            source_url = str(source.url or "")
            source_policy = URLPolicy.from_zoo_source(zoo, source)
            if _url_ok(source_url) and source_policy.is_official(source_url):
                matching = [row for row in source_rows if str(row.get("source_id")) == str(source.id)]
                if any(bool(row.get("source_success")) and str(row.get("source_status") or "").lower() not in {"error", "failed"} for row in matching):
                    official_success.append(str(source.id))
        zoo_override = accepted_zoo_classifications.get(slug)
        if zoo_override is None:
            zoo_override = accepted_zoo_classifications.get(str(getattr(zoo, "id", "") or ""))
        zoo_external = (
            zoo_override is not None
            and _public_classification_info(zoo_override)["nonblocking"]
            and zoo_identifier not in _GOLDEN_ZOO_IDS
            and slug not in _GOLDEN_ZOO_IDS
        )
        zoo_results[slug] = {
            "source_count": len(zoo_sources),
            "successful_official_sources": official_success,
            "article_count": int(valid_by_zoo.get(slug, 0)),
            "minimum": minimum_per_zoo,
            "status": (
                "PASS"
                if (official_success and valid_by_zoo.get(slug, 0) >= minimum_per_zoo)
                or zoo_external
                else "FAIL"
            ),
            "latest": latest_by_zoo.get(slug),
            "classification": zoo_override,
        }
    return {
        "status": "PASS" if not invalid and all(item["status"] == "PASS" for item in zoo_results.values()) else "FAIL",
        "total_rows": len(row_by_article),
        "valid_articles": len(valid_ids),
        "invalid_articles": invalid,
        "zoos": zoo_results,
        "failures": [],
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


def _validate_enabled_sources(
    config: Any,
    connection: sqlite3.Connection,
    *,
    accepted_classifications: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Require current evidence for every source, allowing explicit limits.

    A source marked ``SOURCE_NOT_FOUND``/``ROBOTS_DISALLOWED`` (and the other
    public external classes) is still required to have a persisted row and a
    current check timestamp.  Only success/last-success/root-HTTP assertions
    are relaxed for that source; missing or stale evidence remains blocking.
    """

    enabled_sources = _enabled_configured_sources(config)
    rows = {
        str(row["id"]): dict(row)
        for row in connection.execute(
            "SELECT id, status, success, last_checked, last_success, last_error, last_http_status FROM sources"
        )
    }
    failures: list[str] = []
    sources: dict[str, dict[str, Any]] = {}
    accepted_classifications = accepted_classifications or {}
    if not enabled_sources:
        failures.append("acceptance registry has no enabled configured sources")
    for source in enabled_sources:
        source_id = str(source.id)
        row = rows.get(source_id)
        reasons: list[str] = []
        override = accepted_classifications.get(source_id)
        info = _public_classification_info(override) if override is not None else None
        external = bool(info and info["external"])
        nonblocking = bool(info and info["nonblocking"])
        if row is None:
            reasons.append("missing persisted source evidence")
        else:
            status = str(row.get("status") or "").lower()
            row_info = _classification_info(row)
            external = external or bool(row_info["external"])
            nonblocking = nonblocking or bool(row_info["nonblocking"])
            if (
                row.get("success") not in {1, True}
                or status not in {"completed", "success", "healthy"}
            ) and not nonblocking:
                reasons.append(f"current status is not successful ({status or 'unknown'})")
            if not row.get("last_checked"):
                reasons.append("missing last_checked")
            if not row.get("last_success") and not nonblocking:
                reasons.append("missing last_success")
            if row.get("last_error") and not nonblocking:
                reasons.append(f"last_error is set ({row['last_error']})")
            if row.get("last_http_status") is None and not nonblocking:
                reasons.append("missing root HTTP status")
        source_info = info or (_classification_info(row) if row is not None else _classification_info({"status": "failed"}))
        sources[source_id] = {
            "status": "FAIL" if reasons else "PASS",
            "reasons": reasons,
            "classification": source_info["public_classification"],
            "raw_categories": source_info["raw_categories"],
            "external": bool(source_info["external"]),
        }
        failures.extend(f"enabled source {source_id}: {reason}" for reason in reasons)
    return {
        "status": "FAIL" if failures else "PASS",
        "sources": sources,
        "classifications": {
            source_id: item["classification"] for source_id, item in sources.items()
        },
        "failures": failures,
    }


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


def _normalise_raw_category(value: Any) -> str:
    """Normalize one persisted runtime category without changing its meaning."""

    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _sequence_values(value: Any) -> list[Any]:
    if value in (None, "", "[]"):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _raw_category_from_text(value: Any, *, http_status: Any = None) -> str | None:
    """Extract one stable category from an error/status message.

    The crawler stores the original sanitized message alongside its stable
    category.  This fallback is intentionally narrow and exists for legacy
    rows where only ``last_error``/``errors_json`` survived persistence.
    """

    text = str(value or "").strip().lower()
    if not text:
        return None
    try:
        status = int(http_status) if http_status is not None else None
    except (TypeError, ValueError):
        status = None
    status_match = re.search(r"\b(?:http\s*)?([45]\d{2})\b", text)
    if status is None and status_match:
        status = int(status_match.group(1))
    robots_marker = any(
        marker in text
        for marker in ("robots.txt", "robots disallow", "robots unavailable", "robot disallow")
    )
    if robots_marker:
        return "robots_disallowed"
    if status == 404 or re.search(r"\b404\b", text) or "source not found" in text:
        return "source_not_found"
    if status in {401, 403, 407, 451}:
        return "blocked"
    if any(
        marker in text
        for marker in (
            "javascript required",
            "javascriptrequired",
            "requires javascript",
            "enable javascript",
            "javascript is disabled",
        )
    ):
        return "javascript_required"
    if any(marker in text for marker in ("unsupported", "unknown source kind", "not implemented")):
        return "unsupported"
    if any(marker in text for marker in ("captcha", "cloudflare", "access denied", "request blocked", "blocked")):
        return "blocked"
    if any(marker in text for marker in ("date parse", "dateparse", "invalid date", "unparseable date")):
        return "date_parse_error"
    if any(marker in text for marker in ("content parse", "contentparse", "content parsing", "missing article content")):
        return "content_parse_error"
    if any(marker in text for marker in ("configuration", "config error", "missing source", "source url is required")):
        return "configuration_error"
    if any(marker in text for marker in ("parseerror", "parse error", "not well-formed", "html parser")):
        return "parse_error"
    if any(marker in text for marker in ("timed out", "timeout", " read timed")):
        return "timeout"
    if any(marker in text for marker in ("dns", "name or service not known", "getaddrinfo failed")):
        return "dns_error"
    if any(marker in text for marker in ("ssl", "tls", "certificate verify failed")):
        return "tls_error"
    if status is not None and status >= 400:
        return "http_error"
    if any(marker in text for marker in ("failed", "failure", "error", "exception")):
        return "failed"
    return None


def _raw_categories_from_evidence(value: Any) -> list[str]:
    """Return stable raw categories represented by one result/stat/source row."""

    if isinstance(value, Mapping):
        evidence: Mapping[str, Any] = value
    else:
        try:
            evidence = vars(value)
        except TypeError:
            evidence = {}
    metadata = _json_mapping(evidence.get("metadata", evidence.get("metadata_json")))
    categories: list[str] = []

    def add(raw: Any) -> None:
        category = _normalise_raw_category(raw)
        if not category:
            return
        aliases = {
            "robots": "robots_disallowed",
            "robots_error": "robots_disallowed",
            "robots_unavailable": "robots_disallowed",
            "javascript": "javascript_required",
            "js_required": "javascript_required",
            "no_items": "success_no_items",
            "not_found": "source_not_found",
            "404": "source_not_found",
            "failure": "failed",
            "error": "failed",
            "completed_with_errors": "partial",
        }
        category = aliases.get(category, category)
        if category not in categories:
            categories.append(category)

    explicit_keys = ("error_category", "error_classification", "source_status")
    for key in explicit_keys:
        add(evidence.get(key))
    for key in ("error_categories", "error_classifications"):
        for item in _sequence_values(evidence.get(key)):
            add(item)
    for key in ("error_categories", "error_classifications"):
        for item in _sequence_values(metadata.get(key)):
            add(item)
    for key in ("error_category", "error_classification"):
        add(metadata.get(key))

    http_status = evidence.get("http_status", evidence.get("last_http_status"))
    messages: list[Any] = []
    for key in ("error", "last_error", "error_summary", "errors", "errors_json"):
        messages.extend(_sequence_values(evidence.get(key)))
    for item in messages:
        category = _raw_category_from_text(item, http_status=http_status)
        if category:
            add(category)
    if http_status is not None:
        category = _raw_category_from_text("", http_status=http_status)
        if category:
            add(category)

    # A broad HTTP category is refined by an explicit status/message.  A 404
    # is a source limitation, while other HTTP errors remain blocking unless
    # the crawler recorded a more specific stable external class.
    if "source_not_found" in categories:
        categories = [item for item in categories if item != "http_error"]
    elif "blocked" in categories or "robots_disallowed" in categories:
        categories = [item for item in categories if item != "http_error"]

    status = _normalise_raw_category(evidence.get("status"))
    status_alias = {
        "completed": "success",
        "ok": "success",
        "healthy": "success",
        "success": "success",
        "success_no_items": "success_no_items",
        "partial": "partial",
        "warning": "partial",
        "completed_with_errors": "partial",
        "source_not_found": "source_not_found",
        "not_found": "source_not_found",
        "robots_disallowed": "robots_disallowed",
        "robots": "robots_disallowed",
        "javascript_required": "javascript_required",
        "javascript": "javascript_required",
        "js_required": "javascript_required",
        "blocked": "blocked",
        "unsupported": "unsupported",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
    }
    status_category = status_alias.get(status)
    # Do not turn an explicit external/parser category into a generic failed
    # state merely because its persisted row status is ``error``/``partial``.
    if status_category and status_category not in categories:
        if not categories or status_category not in {"failed", "partial", "success"}:
            add(status_category)
        elif status_category == "partial":
            add(status_category)
        elif status_category == "success" and not categories:
            add(status_category)
        elif status_category == "failed" and not categories:
            add(status_category)
    if not categories:
        categories.append("failed")
    return categories


def _classification_info(value: Any) -> dict[str, Any]:
    """Classify raw crawl evidence and decide whether it is nonblocking."""

    raw_categories = _raw_categories_from_evidence(value)
    blocking = [item for item in raw_categories if item in _BLOCKING_RAW_CATEGORIES]
    external = [item for item in raw_categories if item in _EXTERNAL_RAW_CATEGORIES]
    if blocking:
        public = "FAILED"
    else:
        public = "FAILED"
        # Match the stable reporting precedence: explicit external categories
        # take priority over summary statuses such as ``partial``.
        for raw in raw_categories:
            if raw in _EXTERNAL_RAW_CATEGORIES:
                public = _PUBLIC_CLASSIFICATION_BY_RAW[raw]
                break
        else:
            for raw in ("success_no_items", "partial", "success"):
                if raw in raw_categories:
                    public = _PUBLIC_CLASSIFICATION_BY_RAW[raw]
                    break
    nonblocking = public in {"SUPPORTED", "SUPPORTED_NO_CURRENT_NEWS"}
    nonblocking = nonblocking or public in {
        "SOURCE_NOT_FOUND",
        "ROBOTS_DISALLOWED",
        "JAVASCRIPT_REQUIRED",
        "BLOCKED",
        "UNSUPPORTED",
    }
    if public == "PARTIALLY_SUPPORTED":
        # ``partial`` is a known, honest outcome rather than an unknown code
        # failure.  Any parser/config/date/content category still entered the
        # blocking branch above and therefore keeps this result blocking.
        nonblocking = True
    return {
        "classification": public,
        "public_classification": public,
        "raw_categories": raw_categories,
        "raw_category": raw_categories[0] if raw_categories else "failed",
        "external": bool(external),
        "blocking_categories": blocking,
        "nonblocking": nonblocking and not blocking,
    }


def _public_classification_info(value: Any) -> dict[str, Any]:
    """Build evidence metadata from a public classification override."""

    public = str(value or "").strip().upper()
    if public not in PUBLIC_COMPLETION_CLASSIFICATIONS:
        return _classification_info({"status": "failed"})
    raw = next(
        (key for key, label in _PUBLIC_CLASSIFICATION_BY_RAW.items() if label == public),
        "failed",
    )
    info = _classification_info({"status": raw})
    info["classification"] = public
    info["public_classification"] = public
    info["raw_categories"] = [raw]
    info["raw_category"] = raw
    info["external"] = raw in _EXTERNAL_RAW_CATEGORIES
    info["blocking_categories"] = []
    info["nonblocking"] = public in {
        "SUPPORTED",
        "SUPPORTED_NO_CURRENT_NEWS",
        "PARTIALLY_SUPPORTED",
        "SOURCE_NOT_FOUND",
        "ROBOTS_DISALLOWED",
        "JAVASCRIPT_REQUIRED",
        "BLOCKED",
        "UNSUPPORTED",
    }
    return info


def _evidence_is_nonblocking(value: Any, override: Any = None) -> bool:
    if override is not None:
        return bool(_public_classification_info(override)["nonblocking"])
    return bool(_classification_info(value)["nonblocking"])


def _validate_run_source_evidence(
    config: Any,
    connection: sqlite3.Connection,
    *,
    run_id: str,
    started_at: Any,
    finished_at: Any,
) -> dict[str, Any]:
    """Bind every enabled source's stat and health evidence to one crawl run."""

    enabled_ids = {str(source.id) for source in _enabled_configured_sources(config)}
    stat_rows = [
        dict(row) for row in connection.execute(
            "SELECT * FROM crawl_run_stats WHERE crawl_run_id=?", (run_id,)
        )
    ]
    stat_ids = [str(row.get("source_id") or "") for row in stat_rows]
    failures: list[str] = []
    if not enabled_ids:
        failures.append(f"run {run_id}: acceptance registry has no enabled configured sources")
    for source_id in sorted(enabled_ids - set(stat_ids)):
        failures.append(f"run {run_id}: missing stat for enabled source {source_id}")
    for source_id in sorted(set(stat_ids) - enabled_ids):
        failures.append(f"run {run_id}: unexpected stat source {source_id or 'NULL'}")
    for source_id, count in sorted(Counter(stat_ids).items()):
        if count != 1:
            failures.append(f"run {run_id}: source {source_id or 'NULL'} has {count} stats, expected 1")
    source_classifications: dict[str, str] = {}
    source_raw_categories: dict[str, list[str]] = {}
    for row in stat_rows:
        source_id = str(row.get("source_id") or "")
        errors = _persisted_errors(row.get("errors_json"))
        evidence = dict(row)
        evidence["errors"] = errors
        info = _classification_info(evidence)
        source_classifications[source_id] = str(info["public_classification"])
        source_raw_categories[source_id] = list(info["raw_categories"])
        if str(row.get("status") or "") != "completed" and not info["nonblocking"]:
            failures.append(
                f"run {run_id}: source {source_id or 'NULL'} stat status is "
                f"{row.get('status')!r}, expected 'completed'"
            )
        if (
            int(row.get("error_count") or 0) != 0
            or row.get("error")
            or errors
        ) and not info["nonblocking"]:
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
        source_row = source_rows.get(source_id)
        if source_row is None:
            failures.append(f"run {run_id}: missing current source row {source_id}")
            continue
        stat_info = _public_classification_info(source_classifications[source_id]) if source_id in source_classifications else None
        source_info = _classification_info(source_row)
        stat_nonblocking = bool(stat_info and stat_info["nonblocking"])
        source_nonblocking = bool(source_info["nonblocking"])
        nonblocking = stat_nonblocking and source_nonblocking if stat_info is not None else source_nonblocking
        if source_info["blocking_categories"]:
            nonblocking = False
        if (
            str(source_row.get("status") or "") != "completed"
            or source_row.get("success") not in {1, True}
        ) and not nonblocking:
            failures.append(f"run {run_id}: source {source_id} current status is not successful completed")
        if source_row.get("last_error") and not nonblocking:
            failures.append(f"run {run_id}: source {source_id} current last_error is set")
        http_status = source_row.get("last_http_status")
        if (
            not isinstance(http_status, int)
            or not 200 <= http_status < 400
        ) and not nonblocking:
            failures.append(f"run {run_id}: source {source_id} missing successful root HTTP status")
        for field in ("last_checked", "last_success"):
            evidence_time = _parse_utc_timestamp(source_row.get(field))
            if field == "last_success" and nonblocking and evidence_time is None:
                continue
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
        "classifications": source_classifications,
        "raw_categories": source_raw_categories,
        "failures": failures,
    }


def _validate_run_zoo_evidence(
    config: Any,
    connection: sqlite3.Connection,
    *,
    run_id: str,
    result: Any,
) -> dict[str, Any]:
    """Require one persisted zoo result and complete run coverage metadata."""

    expected_list = [
        _zoo_identifier(zoo)
        for zoo in _enabled_configured_zoos(config)
    ]
    expected_ids = {identifier for identifier in expected_list if identifier}
    failures: list[str] = []
    if not expected_ids:
        failures.append(f"run {run_id}: acceptance registry has no enabled configured zoos")
    if len(expected_list) != len(expected_ids):
        duplicate_expected = sorted(
            zoo_id for zoo_id, count in Counter(expected_list).items() if zoo_id and count > 1
        )
        failures.append(
            f"run {run_id}: configured enabled zoo IDs are duplicated: "
            f"{', '.join(duplicate_expected) or 'NULL'}"
        )

    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='crawl_zoo_results'"
        ).fetchone()
        if table_exists is None:
            rows: list[dict[str, Any]] = []
            failures.append(f"run {run_id}: missing crawl_zoo_results table")
        else:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM crawl_zoo_results WHERE crawl_run_id=? ORDER BY zoo_id, id",
                    (run_id,),
                )
            ]
    except sqlite3.Error as exc:
        rows = []
        failures.append(f"run {run_id}: cannot read crawl_zoo_results: {type(exc).__name__}: {exc}")

    actual_ids = [str(row.get("zoo_id") or "") for row in rows]
    actual_set = set(actual_ids)
    missing_ids = sorted(expected_ids - actual_set)
    extra_ids = sorted(actual_set - expected_ids)
    duplicate_ids = sorted(
        zoo_id for zoo_id, count in Counter(actual_ids).items() if count > 1
    )
    result_classifications: dict[str, str] = {}
    result_raw_categories: dict[str, list[str]] = {}
    for row in rows:
        zoo_id = str(row.get("zoo_id") or "")
        info = _classification_info(row)
        result_classifications[zoo_id] = str(info["public_classification"])
        result_raw_categories[zoo_id] = list(info["raw_categories"])
    if missing_ids:
        failures.append(
            f"run {run_id}: missing zoo result for enabled zoo(s): {', '.join(missing_ids)}"
        )
    if extra_ids:
        failures.append(
            f"run {run_id}: unexpected zoo result ID(s): "
            f"{', '.join(item or 'NULL' for item in extra_ids)}"
        )
    if duplicate_ids:
        failures.append(
            f"run {run_id}: zoo result ID(s) have duplicate rows: {', '.join(item or 'NULL' for item in duplicate_ids)}"
        )

    if isinstance(result, Mapping):
        metadata_value = result.get("metadata")
        result_mapping: Mapping[str, Any] = result
    else:
        metadata_value = getattr(result, "metadata", None)
        result_mapping = {}
    metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
    for key in ("enabled_zoos", "processed_zoos", "selected_zoos"):
        if key in metadata:
            raw_value = metadata[key]
        elif key in result_mapping:
            raw_value = result_mapping[key]
        else:
            continue
        if isinstance(raw_value, str) or not isinstance(raw_value, (list, tuple, set)):
            failures.append(f"run {run_id}: metadata {key} must be a zoo ID sequence")
            continue
        values = [str(item).strip() for item in raw_value]
        value_set = set(values)
        if len(values) != len(value_set):
            failures.append(f"run {run_id}: metadata {key} contains duplicate zoo IDs")
        if value_set != expected_ids:
            failures.append(
                f"run {run_id}: metadata {key} does not match enabled zoo IDs "
                f"(expected {sorted(expected_ids)}, got {sorted(value_set)})"
            )
    for key in ("enabled", "processed", "selected"):
        if key in metadata:
            raw_value = metadata[key]
        elif key in result_mapping:
            raw_value = result_mapping[key]
        else:
            continue
        try:
            count = int(raw_value)
        except (TypeError, ValueError):
            failures.append(f"run {run_id}: metadata {key} must be an integer")
            continue
        if count != len(expected_ids):
            failures.append(
                f"run {run_id}: metadata {key}={count}, expected {len(expected_ids)}"
            )

    return {
        "status": "FAIL" if failures else "PASS",
        "run_id": run_id,
        "enabled_zoo_ids": sorted(expected_ids),
        "result_zoo_ids": sorted(actual_ids),
        "missing_zoo_ids": missing_ids,
        "unexpected_zoo_ids": extra_ids,
        "duplicate_zoo_ids": duplicate_ids,
        "result_count": len(rows),
        "classifications": result_classifications,
        "raw_categories": result_raw_categories,
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


def _prepare_static_report_screenshot(path: Path | None) -> None:
    """Remove only the exact optional static-report screenshot before a run."""

    if path is not None and path.exists():
        path.unlink()


class _QuietStaticReportHandler(SimpleHTTPRequestHandler):
    """Serve one generated report directory without noisy stderr logging."""

    def log_message(self, _format: str, *_args: Any) -> None:  # pragma: no cover - stdlib callback
        return


@contextmanager
def _serve_static_report(report_path: Path) -> Iterator[str]:
    """Serve exactly ``report_path`` through a loopback-only HTTP server.

    A temporary server keeps the smoke test independent from Flask and avoids
    browser ``file://`` restrictions while retaining SimpleHTTPRequestHandler's
    path handling.  The server is shut down before this context exits, even if
    Playwright or a page assertion raises.
    """

    resolved = Path(report_path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"static acceptance report does not exist: {resolved}")
    handler = partial(_QuietStaticReportHandler, directory=str(resolved.parent))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="zoofan-static-report", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/{quote(resolved.name)}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _build_static_report(
    config_path: Path | str,
    db_path: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Build and identify one fresh static report generation for this run."""

    try:
        from zoofan.reporting import build_reports

        build = build_reports(
            config_path=config_path,
            db_path=db_path,
            output_dir=output_dir,
        )
        paths = getattr(build, "paths", None)
        if not isinstance(paths, Mapping):
            paths = build if isinstance(build, Mapping) else {}
        raw_path = paths.get("acceptance/index.html")
        report_path = Path(raw_path) if raw_path else Path(output_dir) / "latest" / "acceptance" / "index.html"
        projection = getattr(build, "projection", {})
        projection = projection if isinstance(projection, Mapping) else {}
        scope = projection.get("scope", {})
        scope = scope if isinstance(scope, Mapping) else {}
        generation_id = str(projection.get("generation_id") or "").strip()
        expanded_roster_available = scope.get("expanded_roster_available")
        if not isinstance(expanded_roster_available, bool):
            expanded_roster_available = None
        if not report_path.is_file():
            raise FileNotFoundError(f"report builder did not create {report_path}")
        return {
            "status": "PASS",
            "report_path": str(report_path.resolve()),
            "output_dir": str(Path(output_dir).resolve()),
            "generation_id": generation_id,
            "scope_label": str(scope.get("label") or "Configured registry only"),
            "expanded_roster_available": expanded_roster_available,
            "files": {str(key): str(value) for key, value in paths.items()},
        }
    except Exception as exc:
        return {
            "status": "FAIL",
            "report_path": str((Path(output_dir) / "latest" / "acceptance" / "index.html").resolve()),
            "output_dir": str(Path(output_dir).resolve()),
            "generation_id": "",
            "scope_label": "Configured registry only",
            "expanded_roster_available": None,
            "failures": [f"static report build: {type(exc).__name__}: {exc}"],
            "error": f"{type(exc).__name__}: {exc}",
        }


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
    first_values: list[Any] = [row.get("discovered_at") for row in rows if row.get("discovered_at")]
    last_values: list[Any] = [
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
        enabled_zoos = _enabled_configured_zoos(config)
        enabled_zoo_slugs = {str(zoo.slug) for zoo in enabled_zoos}
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
        for zoo in enabled_zoos:
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
            expected_metrics: dict[str, int | None] = {
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
            run_table: dict[str, str] = next(
                (item for item in run_card.get("tables", []) if item.get("zoo") == str(zoo.get("name") or "")),
                {},
            )
            home_panel = card_names.get(str(zoo.get("name") or ""), {})
            duration_value = run_card.get("fields", {}).get("duration", "")
            database_matches.update(
                {
                    "run.duration": run_row.get("duration_ms") is not None and duration_value in {str(run_row.get("duration_ms")), f"{run_row.get('duration_ms')} ms"},
                    "run.start": _rendered_exact(run_card.get("fields", {}).get("start", run_card.get("fields", {}).get("started")), run_row.get("started_at"), timestamp=True),
                    "run.end": _rendered_exact(run_card.get("fields", {}).get("end", run_card.get("fields", {}).get("finished")), run_row.get("finished_at"), timestamp=True),
                    "run.status": _rendered_exact(run_card.get("status"), run_row.get("status")),
                    "run.discovered": _rendered_exact(run_table.get("discovered"), expected_metrics["discovered"]),
                    "run.fetched": _rendered_exact(run_table.get("fetched"), expected_metrics["fetched"]),
                    "run.stored": _rendered_exact(run_table.get("stored", run_table.get("stored new")), expected_metrics["stored"]),
                    "run.already_known": _rendered_exact(run_table.get("already known skipped", run_table.get("already known")), expected_metrics["already known"]),
                    "run.duplicate_candidates": _rendered_exact(run_table.get("duplicate candidates"), expected_metrics["duplicate candidates"]),
                    "run.errors": _rendered_exact(run_table.get("errors"), expected_metrics["errors"]),
                    "home.stored": _rendered_exact(home_panel.get("Stored", home_panel.get("Stored/new")), expected_metrics["stored"]),
                    "home.already_known": _rendered_exact(home_panel.get("Already known / skipped", home_panel.get("Already known")), expected_metrics["already known"]),
                    "home.duplicate_candidates": _rendered_exact(home_panel.get("Duplicate candidates"), expected_metrics["duplicate candidates"]),
                    "home.errors": _rendered_exact(home_panel.get("Errors"), expected_metrics["errors"]),
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
        missing_zoo_checks = sorted(enabled_zoo_slugs - set(zoo_checks))
        if missing_zoo_checks:
            browser_assertions.append(
                "overview missing configured zoo evidence: " + ", ".join(missing_zoo_checks)
            )
        if set(zoo_checks) != enabled_zoo_slugs or not all(
            zoo_checks[slug]["status"] == "PASS" for slug in enabled_zoo_slugs
        ):
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
            "configured_zoo_count": len(enabled_zoos),
            "enabled_zoo_slugs": sorted(enabled_zoo_slugs),
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


def _static_report_smoke_in_context(
    playwright: Any,
    report_url: str,
    report_path: Path,
    config: Any,
    console_errors: list[str],
    page_errors: list[str],
    state: dict[str, Any],
    *,
    expected_generation_id: str | None = None,
    expected_scope_label: str | None = None,
    expected_expanded_roster_available: bool | None = None,
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    """Collect browser evidence for one generated static report page."""

    browser = None
    context = None
    page = None
    browser_assertion_errors: list[str] = []
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            text = str(exc)
            if not any(token in text.lower() for token in ("executable", "browser", "chromium")):
                raise
            state["playwright_install"] = _install_chromium_once()
            if state["playwright_install"].get("status") != "PASS":
                raise RuntimeError(f"Playwright Chromium install failed: {state['playwright_install']}")
            browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        response = page.goto(report_url, wait_until="networkidle", timeout=60000)
        if response is None or response.status != 200:
            browser_assertion_errors.append(
                f"static report HTTP status: expected 200, got {response.status if response is not None else None!r}"
            )
        title = page.title()
        scope_banner = page.locator("#alerts").inner_text(timeout=10000) if page.locator("#alerts").count() else ""
        rows = page.locator("#zoo-table tbody tr")
        initial_row_count = rows.count()
        initial_names = [
            row.locator("td").first.inner_text().strip()
            for row in rows.all()
            if row.locator("td").count()
        ]
        controls = {
            control: page.locator(f"#{control}").count() == 1
            for control in _STATIC_REPORT_REQUIRED_CONTROLS
        }
        metric_labels = page.locator(".metric span").all_inner_texts()
        table_columns = page.locator("#zoo-table thead th").all_inner_texts()
        category_labels = page.locator("#run-panels").inner_text(timeout=10000) if page.locator("#run-panels").count() else ""
        generation_id = ""
        generation_meta = page.locator('meta[name="generation-id"]')
        if generation_meta.count():
            generation_id = str(generation_meta.first.get_attribute("content") or "").strip()

        try:
            raw_html_found = "raw_html" in report_path.read_text(encoding="utf-8").lower()
        except Exception as exc:
            raw_html_found = False
            browser_assertion_errors.append(
                f"static report file could not be inspected for raw_html: {type(exc).__name__}: {exc}"
            )

        screenshot_written = False
        if screenshot_path is not None:
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            screenshot_written = Path(screenshot_path).is_file()
            state["screenshot_written_this_run"] = screenshot_written

        search_evidence: dict[str, Any] = {"ok": True, "exercised": False}
        name_filter = page.locator("#name-filter")
        if initial_names and name_filter.count() == 1:
            query = initial_names[0]
            name_filter.fill(query)
            search_rows = page.locator("#zoo-table tbody tr")
            search_names = [
                row.locator("td").first.inner_text().strip()
                for row in search_rows.all()
                if row.locator("td").count()
            ]
            search_evidence = {
                "ok": len(search_names) == 1 and search_names[0] == query,
                "exercised": True,
                "query": query,
                "row_count": len(search_names),
            }
            name_filter.fill("")
        elif initial_names:
            search_evidence = {"ok": False, "exercised": False, "reason": "name search control unavailable"}

        filter_evidence: dict[str, Any] = {"ok": True, "exercised": False, "controls": {}}
        for control in ("country-filter", "group-filter", "status-filter"):
            select = page.locator(f"#{control}")
            values: list[str] = []
            for option in (select.locator("option").all() if select.count() else []):
                value = str(option.get_attribute("value") or "").strip()
                if value:
                    values.append(value)
            if not values:
                filter_evidence["controls"][control] = {"ok": True, "exercised": False}
                continue
            selected = values[0]
            select.select_option(selected)
            filtered_rows = page.locator("#zoo-table tbody tr").count()
            selected_value = str(select.input_value() or "").strip()
            control_ok = selected_value == selected and filtered_rows <= initial_row_count
            filter_evidence["controls"][control] = {
                "ok": control_ok,
                "exercised": True,
                "selected": selected_value,
                "row_count": filtered_rows,
            }
            filter_evidence["exercised"] = True
            filter_evidence["ok"] = bool(filter_evidence["ok"]) and control_ok
            select.select_option("")

        sort_evidence: dict[str, Any] = {"ok": False, "exercised": False}
        sort_filter = page.locator("#sort-filter")
        if sort_filter.count() == 1:
            sort_filter.select_option("name")
            sorted_names = [
                row.locator("td").first.inner_text().strip()
                for row in page.locator("#zoo-table tbody tr").all()
                if row.locator("td").count()
            ]
            name_order_ok = bool(
                page.evaluate(
                    """() => {
                        const names = Array.from(document.querySelectorAll('#zoo-table tbody tr td:first-child'))
                            .map(cell => cell.textContent.trim());
                        return names.every((name, index) => index === 0 || names[index - 1].localeCompare(name) <= 0);
                    }"""
                )
            )
            sort_filter.select_option("articles")
            article_sort_count = page.locator("#zoo-table tbody tr").count()
            sort_evidence = {
                "ok": name_order_ok
                and article_sort_count == initial_row_count,
                "exercised": True,
                "name_order": sorted_names,
                "article_sort_row_count": article_sort_count,
            }
            sort_filter.select_option("anomaly")

        detail_evidence: dict[str, Any] = {"ok": True, "exercised": False}
        page.locator("#name-filter").fill("")
        detail_rows = page.locator("#zoo-table tbody tr")
        if initial_names and detail_rows.count():
            detail_rows.first.click()
            detail_text = page.locator("#details").inner_text(timeout=10000)
            detail_evidence = {
                "ok": initial_names[0] in detail_text and "Select a zoo row" not in detail_text,
                "exercised": True,
                "name": initial_names[0],
            }

        desktop_metrics = page.evaluate(
            """() => ({
                document: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                body: document.body.scrollWidth > document.body.clientWidth
            })"""
        )
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(report_url, wait_until="networkidle", timeout=60000)
        mobile_metrics = page.evaluate(
            """() => ({
                document: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                body: document.body.scrollWidth > document.body.clientWidth
            })"""
        )
        horizontal_overflow = {
            "desktop": bool(desktop_metrics.get("document") or desktop_metrics.get("body")),
            "mobile": bool(mobile_metrics.get("document") or mobile_metrics.get("body")),
        }
        observation = {
            "title": title,
            "scope_banner": scope_banner,
            "expected_scope_label": expected_scope_label,
            "expanded_roster_available": expected_expanded_roster_available,
            "expected_zoo_count": len(_enabled_configured_zoos(config)),
            "row_count": initial_row_count,
            "initial_row_count": initial_row_count,
            "controls": controls,
            "search": search_evidence,
            "filters": filter_evidence,
            "sort": sort_evidence,
            "detail": detail_evidence,
            "metric_labels": metric_labels,
            "table_columns": table_columns,
            "category_labels": category_labels,
            "raw_html_found": raw_html_found,
            "generation_id": generation_id,
            "expected_generation_id": expected_generation_id,
            "horizontal_overflow": horizontal_overflow,
            "browser_assertion_errors": browser_assertion_errors,
            "page_errors": page_errors,
            "console_errors": console_errors,
            "screenshot_requested": screenshot_path is not None,
            "screenshot_written_this_run": screenshot_written,
        }
        semantic = evaluate_static_report_observation(
            observation,
            expected_zoo_count=len(_enabled_configured_zoos(config)),
            expected_scope_label=expected_scope_label,
        )
        return {
            "status": semantic["status"],
            "failures": semantic["failures"],
            "report_url": report_url,
            "report_path": str(report_path),
            "generation_id": generation_id,
            "expected_generation_id": expected_generation_id,
            "screenshot": str(screenshot_path) if screenshot_path is not None else None,
            "screenshot_written_this_run": screenshot_written,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "observation": observation,
        }
    finally:
        _close_playwright_handles(page, context, browser)


def _static_report_browser_smoke(
    report_path: Path,
    config: Any,
    *,
    expected_generation_id: str | None = None,
    expected_scope_label: str | None = None,
    expected_expanded_roster_available: bool | None = None,
    screenshot_path: Path | None = None,
) -> dict[str, Any]:
    """Smoke-test one freshly generated static report in a real browser."""

    report_path = Path(report_path)
    _prepare_static_report_screenshot(screenshot_path)
    if not report_path.is_file():
        return {
            "status": "FAIL",
            "report_path": str(report_path),
            "failures": [f"static report file is missing: {report_path}"],
        }
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            "status": "FAIL",
            "report_path": str(report_path),
            "failures": [f"static report Playwright import failed: {type(exc).__name__}: {exc}"],
            "error": f"{type(exc).__name__}: {exc}",
        }

    console_errors: list[str] = []
    page_errors: list[str] = []
    state: dict[str, Any] = {"playwright_install": None, "screenshot_written_this_run": False}
    try:
        with _serve_static_report(report_path) as report_url:
            with sync_playwright() as playwright:
                result = _static_report_smoke_in_context(
                    playwright,
                    report_url,
                    report_path,
                    config,
                    console_errors,
                    page_errors,
                    state,
                    expected_generation_id=expected_generation_id,
                    expected_scope_label=expected_scope_label,
                    expected_expanded_roster_available=expected_expanded_roster_available,
                    screenshot_path=screenshot_path,
                )
                result["playwright_install"] = state.get("playwright_install")
                return result
    except Exception as exc:
        return {
            "status": "FAIL",
            "report_path": str(report_path),
            "console_errors": console_errors,
            "page_errors": page_errors,
            "playwright_install": state.get("playwright_install"),
            "failures": [f"static report browser smoke: {type(exc).__name__}: {exc}"],
            "error": f"{type(exc).__name__}: {exc}",
        }


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


def _result_entries(result: Any) -> list[Any]:
    if isinstance(result, Mapping):
        value = result.get("zoo_results", ())
    else:
        value = getattr(result, "zoo_results", ())
    return list(value) if isinstance(value, (list, tuple)) else []


def _result_value(value: Any, *keys: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for key in keys:
            if key in value:
                return value[key]
        return default
    for key in keys:
        item = getattr(value, key, None)
        if item is not None:
            return item
    return default


def _zero_public_classification_counts() -> dict[str, int]:
    return {category: 0 for category in PUBLIC_COMPLETION_CLASSIFICATIONS}


def _classify_crawl_results(config: Any, result: Any) -> dict[str, Any]:
    """Classify exactly one latest-run result for each enabled configured zoo."""

    enabled_zoos = _enabled_configured_zoos(config)
    expected_ids = [_zoo_identifier(zoo) for zoo in enabled_zoos]
    expected_set = {item for item in expected_ids if item}
    entries = _result_entries(result)
    by_id: dict[str, list[Any]] = defaultdict(list)
    for entry in entries:
        identifier = str(_result_value(entry, "zoo_id", "id", default="") or "").strip()
        if identifier:
            by_id[identifier].append(entry)

    by_identifier: dict[str, dict[str, Any]] = {}
    by_slug: dict[str, dict[str, Any]] = {}
    result_records: list[dict[str, Any]] = []
    failures: list[str] = []
    counts = _zero_public_classification_counts()
    raw_counts: Counter[str] = Counter()
    for zoo in enabled_zoos:
        identifier = _zoo_identifier(zoo)
        matches = by_id.get(identifier, [])
        if len(matches) != 1:
            issue = "missing_result" if not matches else "duplicate_result"
            info = _classification_info({"status": "failed", "error_category": "configuration_error"})
            info["result_issue"] = issue
            failures.append(
                f"zoo {identifier or getattr(zoo, 'slug', '')}: expected one result, got {len(matches)}"
            )
        else:
            info = _classification_info(matches[0])
            info["result_issue"] = None
        info = dict(info)
        info["zoo_id"] = identifier
        info["zoo_slug"] = str(getattr(zoo, "slug", "") or identifier)
        info["zoo_name"] = str(getattr(zoo, "name", "") or identifier)
        by_identifier[identifier] = info
        if info["zoo_slug"]:
            by_slug[info["zoo_slug"]] = info
        counts[str(info["public_classification"])] += 1
        for raw in info["raw_categories"]:
            raw_counts[str(raw)] += 1
        result_records.append(
            {
                "zoo_id": identifier,
                "zoo_slug": info["zoo_slug"],
                "classification": info["public_classification"],
                "raw_categories": list(info["raw_categories"]),
                "nonblocking": bool(info["nonblocking"]),
                "result_issue": info.get("result_issue"),
            }
        )

    unexpected_ids = sorted(set(by_id) - expected_set)
    if unexpected_ids:
        failures.append("unexpected zoo result ID(s): " + ", ".join(unexpected_ids))
    return {
        "counts": counts,
        "classification_counts": dict(counts),
        "completion_classifications": dict(counts),
        "public_classification_counts": dict(counts),
        "raw_category_counts": dict(raw_counts),
        "by_id": by_identifier,
        "by_slug": by_slug,
        "results": result_records,
        "failures": failures,
        "status": "PASS" if not failures and all(item["nonblocking"] for item in by_identifier.values()) else "FAIL",
    }


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
            "completion_classification": "FAILED",
            "raw_categories": ["failed"],
            "result_issue": "missing_result",
        }
        for zoo in _enabled_configured_zoos(config)
    }


def _run_live_acceptance(config: Any, connection: sqlite3.Connection) -> dict[str, Any]:
    registry_validation = _validate_live_registry(config)
    if registry_validation["status"] != "PASS":
        failures = list(registry_validation["failures"])
        empty_zoo_report = _empty_zoo_report(config)
        empty_validation = {
            "status": "FAIL",
            "total_rows": 0,
            "valid_articles": 0,
            "invalid_articles": [],
            "zoos": {},
            "failures": failures,
        }
        empty_source_validation = {
            "status": "FAIL",
            "sources": {},
            "failures": failures,
        }
        empty_dedup = {"status": "FAIL", "failures": failures}
        return {
            "status": "FAIL",
            "failures": failures,
            "classification_counts": _zero_public_classification_counts(),
            "completion_classifications": _zero_public_classification_counts(),
            "public_classification_counts": _zero_public_classification_counts(),
            "classifications": {},
            "raw_category_counts": {},
            "registry_validation": registry_validation,
            "run1": None,
            "run2": None,
            "validation": empty_validation,
            "source_validation": empty_source_validation,
            "run_source_validation": {},
            "run_zoo_validation": {},
            "dedup": empty_dedup,
            "zoos": empty_zoo_report,
        }

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
        run1_zoo_validation = _validate_run_zoo_evidence(
            config,
            connection,
            run_id=str(getattr(result1.run, "id", "") or ""),
            result=result1,
        )
        run1_classifications = _classify_crawl_results(config, result1)
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
        run2_zoo_validation = _validate_run_zoo_evidence(
            config,
            connection,
            run_id=str(getattr(result2.run, "id", "") or ""),
            result=result2,
        )
        run2_classifications = _classify_crawl_results(config, result2)
        after_run2_total = _article_total(connection)
        run2_sets = _article_canonical_sets(connection)
        print(f"ENDGOAL LIVE CRAWL 2 {result2.status}", flush=True)
        all_canonicals_before = set().union(*before_sets.values()) if before_sets else set()
        all_canonicals_run1 = set().union(*run1_sets.values()) if run1_sets else set()
        all_canonicals_run2 = set().union(*run2_sets.values()) if run2_sets else set()
        accepted_zoo_classifications = {
            slug: info["public_classification"]
            for slug, info in run2_classifications["by_slug"].items()
        }
        accepted_source_classifications = dict(run2_source_validation.get("classifications", {}))
        validation = _validate_articles(
            connection,
            config,
            accepted_zoo_classifications=accepted_zoo_classifications,
            accepted_source_classifications=accepted_source_classifications,
        )
        source_validation = _validate_enabled_sources(
            config,
            connection,
            accepted_classifications=accepted_source_classifications,
        )
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
            errors: list[str] = []
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
                    "completion_classification": run2_classifications["by_slug"].get(slug, {}).get(
                        "public_classification", "FAILED"
                    ),
                    "raw_categories": run2_classifications["by_slug"].get(slug, {}).get(
                        "raw_categories", ["failed"]
                    ),
                    "result_issue": run2_classifications["by_slug"].get(slug, {}).get(
                        "result_issue"
                    ),
                }
            )
        run_failures: list[str] = []
        for label, result, classifications in (
            ("run1", result1, run1_classifications),
            ("run2", result2, run2_classifications),
        ):
            if classifications["status"] != "PASS":
                if result.status != "completed":
                    run_failures.append(f"{label}: status is {result.status}, expected completed")
                else:
                    run_failures.append(f"{label}: blocking or incomplete zoo classification")
            for stat in result.stats:
                if stat.error_count or stat.error or stat.errors:
                    stat_info = _classification_info(
                        {
                            "status": stat.status,
                            "error": stat.error,
                            "errors": stat.errors,
                            "error_category": getattr(stat, "error_category", None),
                            "metadata": stat.metadata,
                        }
                    )
                    if not stat_info["nonblocking"]:
                        run_failures.append(
                            f"{label}: source {stat.source_id or 'unknown'} recorded blocking errors"
                        )
        validation_failures: list[str] = []
        if validation["status"] != "PASS":
            validation_failures.append("article validation failed")
            for invalid in validation.get("invalid_articles", ()):
                validation_failures.append(
                    f"article {invalid.get('article_id') or 'unknown'}: "
                    + "; ".join(str(reason) for reason in invalid.get("reasons", ()))
                )
            for slug, zoo_validation in validation.get("zoos", {}).items():
                if zoo_validation.get("status") != "PASS":
                    validation_failures.append(
                        f"zoo {slug}: article/source evidence does not meet acceptance criteria"
                    )
        failures = [
            *source_validation["failures"],
            *run1_source_validation["failures"],
            *run2_source_validation["failures"],
            *run1_zoo_validation["failures"],
            *run2_zoo_validation["failures"],
            *run1_classifications["failures"],
            *run2_classifications["failures"],
            *run_failures,
            *validation_failures,
            *dedup.get("failures", []),
        ]
        return {
            "status": "PASS"
            if not failures and validation["status"] == "PASS" and dedup["status"] == "PASS"
            else "FAIL",
            "failures": failures,
            "classification_counts": run2_classifications["classification_counts"],
            "completion_classifications": run2_classifications["completion_classifications"],
            "public_classification_counts": run2_classifications["public_classification_counts"],
            "raw_category_counts": run2_classifications["raw_category_counts"],
            "classifications": {
                slug: info["public_classification"]
                for slug, info in run2_classifications["by_slug"].items()
            },
            "run1_classifications": run1_classifications,
            "run2_classifications": run2_classifications,
            "registry_validation": registry_validation,
            "run1": result1.as_dict(),
            "run2": result2.as_dict(),
            "validation": validation,
            "source_validation": source_validation,
            "run_source_validation": {
                "run1": run1_source_validation,
                "run2": run2_source_validation,
            },
            "run_zoo_validation": {
                "run1": run1_zoo_validation,
                "run2": run2_zoo_validation,
            },
            "dedup": dedup,
            "zoos": zoos,
        }
    finally:
        storage.close()


def main(
    config_path: Path | str | None = None,
    *,
    report_output_dir: Path | str | None = None,
) -> int:
    selected_config_path = Path(config_path) if config_path is not None else CONFIG_PATH
    selected_report_output_dir = (
        Path(report_output_dir) if report_output_dir is not None else REPORTS_DIR
    )
    static_report_screenshot_path = ARTIFACTS_DIR / STATIC_REPORT_SCREENSHOT_NAME
    started_at = _now()
    report: dict[str, Any] = {
        "status": "FAIL",
        "started_at": started_at,
        "finished_at": None,
        "database": str(DB_PATH),
        "config": str(selected_config_path),
        "artifacts": {
            "report": str(REPORT_PATH),
            "dashboard_screenshot": str(SCREENSHOT_PATH),
            "static_report": str(
                (selected_report_output_dir / "latest" / "acceptance" / "index.html").resolve()
            ),
            "static_report_screenshot": str(static_report_screenshot_path),
        },
        "unit_tests": None,
        "zoos": {},
        "dedup": None,
        "classification_counts": _zero_public_classification_counts(),
        "completion_classifications": _zero_public_classification_counts(),
        "public_classification_counts": _zero_public_classification_counts(),
        "classifications": {},
        "failure_isolation": None,
        "dashboard": None,
        "static_report": None,
        "static_report_build": None,
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

        config = load_config(selected_config_path)
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
                "classification_counts": live.get(
                    "classification_counts", _zero_public_classification_counts()
                ),
                "completion_classifications": live.get(
                    "completion_classifications", _zero_public_classification_counts()
                ),
                "public_classification_counts": live.get(
                    "public_classification_counts", _zero_public_classification_counts()
                ),
                "classifications": live.get("classifications", {}),
                "sources_findings": _source_findings(config, connection),
            }
        )
        # Build from this run's fresh acceptance database before starting any
        # browser smoke.  The returned path/generation is passed directly to
        # the static collector so an older ``reports/latest`` cannot satisfy
        # the gate after a build failure.
        static_build = _build_static_report(
            selected_config_path,
            DB_PATH,
            selected_report_output_dir,
        )
        report["static_report_build"] = static_build
        if static_build.get("status") == "PASS":
            static_smoke = _static_report_browser_smoke(
                Path(str(static_build["report_path"])),
                config,
                expected_generation_id=str(static_build.get("generation_id") or "") or None,
                expected_scope_label=str(static_build.get("scope_label") or "") or None,
                expected_expanded_roster_available=static_build.get("expanded_roster_available"),
                screenshot_path=static_report_screenshot_path,
            )
            static_result = dict(static_build)
            static_result["smoke"] = static_smoke
            static_result["status"] = static_smoke.get("status", "FAIL")
            static_result["failures"] = [
                *static_build.get("failures", []),
                *static_smoke.get("failures", []),
            ]
        else:
            static_result = dict(static_build)
            static_result["smoke"] = None
            static_result["status"] = "FAIL"
        report["static_report"] = static_result
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
    "PUBLIC_COMPLETION_CLASSIFICATIONS",
    "REPORT_PATH",
    "REPORTS_DIR",
    "SCREENSHOT_PATH",
    "_atomic_write_json",
    "_dedup_report",
    "_human_banner",
    "_validate_articles",
    "evaluate_dashboard_observation",
    "evaluate_static_report_observation",
    "emit_final_endgoal_result",
    "finalize_endgoal_report",
    "navigation_false_positive",
    "main",
]
