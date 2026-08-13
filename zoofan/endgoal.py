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
               a.content_hash, a.raw_html, a.metadata_json,
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
    if column not in {"canonical_url", "normalized_url"}:
        raise ValueError(f"unsupported duplicate column: {column}")
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
        "canonical_sql": "GROUP BY canonical_url HAVING COUNT(*) > 1",
        "normalized_sql": "GROUP BY normalized_url HAVING COUNT(*) > 1",
        "increase_only_new_distinct_canonical": net_run2_increase == len(run2_new),
        "no_duplicate_canonical": not canonical_duplicates,
        "no_duplicate_normalized": not normalized_duplicates,
        "status": "PASS"
        if not canonical_duplicates
        and not normalized_duplicates
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
        if not re.fullmatch(r"[0-9a-fA-F]{64}", content_hash) or not row.get("raw_html"):
            reasons.append("missing html_hash/raw_html")
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
        for route in ("/", "/articles", "/sources", "/runs"):
            response = page.goto(base_url + route, wait_until="networkidle", timeout=60000)
            statuses[route] = response.status if response is not None else None
            body = page.locator("body").inner_text(timeout=10000)
            route_bodies[route] = body
            if "Internal Server Error" in body or "Traceback (most recent call last)" in body:
                page_errors.append(f"{route}: server error text in body")
            if route == "/":
                Path(SCREENSHOT_PATH).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
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
        route_ok = all(status == 200 for status in statuses.values())
        no_server_error = all(
            "Internal Server Error" not in body and "Traceback (most recent call last)" not in body
            for body in route_bodies.values()
        )
        status = "PASS" if (
            route_ok
            and len(getattr(config, "zoos", ())) == 3
            and all(item["status"] == "PASS" for item in zoo_checks.values())
            and bool(valid_articles)
            and official_article_visible
            and not console_errors
            and not page_errors
            and no_server_error
            and SCREENSHOT_PATH.exists()
        ) else "FAIL"
        return {
            "status": status,
            "base_url": base_url,
            "routes": statuses,
            "three_zoos": len(getattr(config, "zoos", ())) == 3,
            "zoo_article_counts": zoo_checks,
            "db_live_article_count": len(valid_articles),
            "official_article_visible": official_article_visible,
            "console_errors": console_errors,
            "page_errors": page_errors,
            "server_error_text": not no_server_error,
            "screenshot": str(SCREENSHOT_PATH),
            "playwright_install": state.get("playwright_install"),
        }
    finally:
        # This finally executes inside ``with sync_playwright()``. Never close
        # a browser after that context exits: it causes "Event loop is closed"
        # on Playwright's sync driver.
        _close_playwright_handles(page, context, browser)


def _dashboard_browser_smoke(base_url: str, db_path: Path, config: Any) -> dict[str, Any]:
    """Exercise all dashboard routes in a real Playwright browser."""

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"status": "FAIL", "error": f"Playwright import failed: {type(exc).__name__}: {exc}"}

    console_errors: list[str] = []
    page_errors: list[str] = []
    statuses: dict[str, int | None] = {}
    route_bodies: dict[str, str] = {}
    state: dict[str, Any] = {"playwright_install": None}
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
        after_run1_total = _article_total(connection)
        run1_sets = _article_canonical_sets(connection)
        print(f"ENDGOAL LIVE CRAWL 1 {result1.status}", flush=True)
        print("ENDGOAL LIVE CRAWL 2 START", flush=True)
        result2 = crawler.crawl("all")
        after_run2_total = _article_total(connection)
        run2_sets = _article_canonical_sets(connection)
        print(f"ENDGOAL LIVE CRAWL 2 {result2.status}", flush=True)
        all_canonicals_before = set().union(*before_sets.values()) if before_sets else set()
        all_canonicals_run1 = set().union(*run1_sets.values()) if run1_sets else set()
        all_canonicals_run2 = set().union(*run2_sets.values()) if run2_sets else set()
        validation = _validate_articles(connection, config)
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
        return {
            "status": "PASS" if result1.status in {"completed", "completed_with_errors"} and result2.status in {"completed", "completed_with_errors"} and validation["status"] == "PASS" and dedup["status"] == "PASS" else "FAIL",
            "run1": result1.as_dict(),
            "run2": result2.as_dict(),
            "validation": validation,
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
        gates = [
            report.get("unit_tests", {}).get("status") == "PASS" if isinstance(report.get("unit_tests"), Mapping) else False,
            report.get("live", {}).get("status") == "PASS" if isinstance(report.get("live"), Mapping) else False,
            report.get("dedup", {}).get("status") == "PASS" if isinstance(report.get("dedup"), Mapping) else False,
            report.get("failure_isolation", {}).get("status") == "PASS" if isinstance(report.get("failure_isolation"), Mapping) else False,
            report.get("dashboard", {}).get("status") == "PASS" if isinstance(report.get("dashboard"), Mapping) else False,
        ]
        report["status"] = "PASS" if all(gates) else "FAIL"
        try:
            _atomic_write_json(REPORT_PATH, report)
        except Exception as exc:
            report["status"] = "FAIL"
            report["errors"].append(f"report write failed: {type(exc).__name__}: {exc}")
            print(f"ENDGOAL ERROR report write failed: {exc}", flush=True)
        print("ENDGOAL HUMAN REPORT", flush=True)
        print(f"status={report['status']} database={DB_PATH}", flush=True)
        for slug, zoo in report.get("zoos", {}).items():
            print(
                f"zoo={slug} sources={zoo.get('source_count', 0)} articles={zoo.get('articles', 0)} "
                f"crawl1_new={zoo.get('crawl1_new', 0)} crawl2_new={zoo.get('crawl2_new', 0)} "
                f"errors={len(zoo.get('errors', []))}",
                flush=True,
            )
        print(f"ENDGOAL {'PASS' if report['status'] == 'PASS' else 'FAIL'}", flush=True)
        print(_human_banner(report["status"]), flush=True)
    return 0 if report["status"] == "PASS" else 1


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
    "navigation_false_positive",
    "main",
]
