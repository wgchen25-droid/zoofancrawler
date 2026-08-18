#!/usr/bin/env python3
"""Deterministic acceptance gate for the read-only Crawler Console.

The verifier deliberately owns its fixture and report only.  It never opens
the production database and never runs a real crawl.  A failed check is still
rendered into the report before the process exits non-zero.
"""

from __future__ import annotations

import ast
import base64
import html
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
REPORT_RELATIVE_PATH = "reports/latest/crawler_console_acceptance.html"
REPORT_PATH = Path(
    os.environ.get("CRAWLER_CONSOLE_REPORT_PATH", str(ROOT / REPORT_RELATIVE_PATH))
).expanduser()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Check:
    section: str
    name: str
    status: str
    evidence: str
    required: bool = True


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    regression: Dict[str, Any] = field(default_factory=dict)
    environment: Dict[str, Any] = field(default_factory=dict)
    known_limitations: List[str] = field(default_factory=list)
    screenshot_data_uri: Optional[str] = None

    def add(
        self,
        section: str,
        name: str,
        status: str,
        evidence: Any,
        *,
        required: bool = True,
    ) -> None:
        if isinstance(evidence, (dict, list, tuple)):
            evidence_text = json.dumps(evidence, ensure_ascii=False, sort_keys=True, default=str)
        else:
            evidence_text = str(evidence)
        self.checks.append(
            Check(
                section=section,
                name=name,
                status=status.upper(),
                evidence=evidence_text,
                required=required,
            )
        )

    @property
    def blockers(self) -> List[Check]:
        return [item for item in self.checks if item.required and item.status != "PASS"]

    @property
    def warnings(self) -> List[Check]:
        return [item for item in self.checks if not item.required and item.status != "PASS"]

    @property
    def status(self) -> str:
        return "PASS" if not self.blockers else "FAIL"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _obj(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    method = getattr(value, "as_dict", None)
    if callable(method):
        result = method()
        return dict(result) if isinstance(result, Mapping) else {}
    return dict(getattr(value, "__dict__", {}) or {})


def _event_dict(value: Any) -> Dict[str, Any]:
    result = _as_dict(value)
    if "metadata" not in result and "metadata_json" in result:
        try:
            result["metadata"] = json.loads(result["metadata_json"] or "{}")
        except (TypeError, ValueError):
            result["metadata"] = {}
    return result


def _result_stop_reason(result: Any) -> Optional[str]:
    direct = getattr(result, "stop_reason", None)
    if direct:
        return str(direct)
    metadata = _obj(result, "metadata", {}) or {}
    if isinstance(metadata, Mapping) and metadata.get("stop_reason"):
        return str(metadata["stop_reason"])
    return None


def _safe_tail(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return "…\n" + text[-limit:]


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        value = (completed.stdout or "").strip()
        return value or "unavailable"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def _parse_pytest_summary(
    output: str,
) -> Tuple[str, Dict[str, int], Optional[int]]:
    """Parse pytest's terminal summary without assuming its duration format."""

    counts = {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
    }
    # Pytest renders short durations as decimal seconds (``101.51s``), clock
    # values (``01:40`` / ``0:01:40``), or both for long runs
    # (``100.89s (0:01:40)``).
    clock_duration = r"\d+:\d{2}(?::\d{2})?(?:\.\d+)?"
    duration = (
        r"(?:\d+(?:\.\d+)?s(?:\s+\(" + clock_duration + r"\))?|"
        + clock_duration
        + r")"
    )
    status = r"\b(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
    summary_pattern = re.compile(r"\bin\s+" + duration + r"\s*$")
    summary_lines = [
        line.strip()
        for line in output.splitlines()
        if summary_pattern.search(line) and re.search(status, line)
    ]
    summary = summary_lines[-1] if summary_lines else ""
    labels = re.compile(
        r"(?P<count>\d+)\s+(?P<label>passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b"
    )
    for match in labels.finditer(summary):
        label = match.group("label")
        if label == "error":
            label = "errors"
        counts[label] = int(match.group("count"))

    parsed_count = sum(counts[label] for label in counts if label != "deselected")
    test_count: Optional[int] = parsed_count if summary else None
    return summary, counts, test_count


def _pytest_summary_parser_self_check(report: Report) -> None:
    cases = [
        ("754 passed in 101.51s", {"passed": 754, "failed": 0, "skipped": 0, "total": 754}),
        ("754 passed in 0:01:40", {"passed": 754, "failed": 0, "skipped": 0, "total": 754}),
        (
            "754 passed in 100.89s (0:01:40)",
            {"passed": 754, "failed": 0, "skipped": 0, "total": 754},
        ),
        (
            "2 failed, 750 passed, 3 skipped in 01:41",
            {"passed": 750, "failed": 2, "skipped": 3, "total": 755},
        ),
    ]
    evidence: List[Dict[str, Any]] = []
    passed = True
    for sample, expected in cases:
        summary, counts, total = _parse_pytest_summary(sample)
        actual = {
            "passed": counts["passed"],
            "failed": counts["failed"] + counts["errors"],
            "skipped": counts["skipped"],
            "total": total,
        }
        ok = summary == sample and actual == expected
        passed = passed and ok
        evidence.append(
            {
                "sample": sample,
                "expected": expected,
                "actual": actual,
                "result": "PASS" if ok else "FAIL",
            }
        )
    report.add(
        "Regression Tests",
        "Pytest summary parser supports seconds and clock durations",
        "PASS" if passed else "FAIL",
        evidence,
    )


def _run_regression(report: Report) -> None:
    command = "python3 -m pytest -q"
    started = time.perf_counter()
    try:
        regression_environment = os.environ.copy()
        # The quality-gate tests invoke a nested mypy process.  An isolated
        # cache keeps this verifier reproducible when a previous developer
        # run left an incremental cache with stale source metadata.
        with tempfile.TemporaryDirectory(prefix="zoofancrawler-regression-") as cache_dir:
            regression_environment["MYPY_CACHE_DIR"] = cache_dir
            completed = subprocess.run(
                ["python3", "-m", "pytest", "-q"],
                cwd=str(ROOT),
                env=regression_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=900,
            )
        output = completed.stdout or ""
        return_code = completed.returncode
    except subprocess.TimeoutExpired as error:
        output = (error.stdout or "") if isinstance(error.stdout, str) else ""
        return_code = 124
    except OSError as error:
        output = str(error)
        return_code = 127
    duration = time.perf_counter() - started
    summary, counts, test_count = _parse_pytest_summary(output)
    failed_total = counts["failed"] + counts["errors"]
    report.regression = {
        "command": command,
        "exit_code": return_code,
        "duration_seconds": round(duration, 3),
        "test_count": test_count,
        "passed": counts["passed"],
        "failed": failed_total,
        "failed_tests": counts["failed"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
        "xfailed": counts["xfailed"],
        "xpassed": counts["xpassed"],
        "deselected": counts["deselected"],
        "summary_line": summary or None,
        "output": _safe_tail(output),
    }
    report.add(
        "Regression Tests",
        command,
        "PASS" if return_code == 0 else "FAIL",
        {
            "exit_code": return_code,
            "test_count": test_count,
            "passed": counts["passed"],
            "failed": failed_total,
            "errors": counts["errors"],
            "deselected": counts["deselected"],
            "summary_line": summary or None,
            "duration_seconds": round(duration, 3),
            "output_tail": _safe_tail(output, 3000),
        },
    )


class FixtureFetcher:
    """Small deterministic fetcher matching the injected crawler contract."""

    supports_request_policy = True

    def __init__(self, payloads: Mapping[str, Any]) -> None:
        self.payloads = dict(payloads)
        self.calls: List[str] = []

    def fetch(self, url: str, *, request_policy: Any) -> Any:
        from zoofan.models import FetchResponse

        if request_policy.validate(url) != url:
            raise AssertionError("fixture request crossed URL policy boundary")
        self.calls.append(url)
        if url not in self.payloads:
            raise AssertionError("fixture has no payload for " + url)
        value = self.payloads[url]
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, FetchResponse):
            return value
        return FetchResponse(url, 200, str(value).encode("utf-8"))


def _fixture_records(
    storage: Any,
    *,
    zoo_id: str,
    slug: str,
    name: str,
    host: str,
    source_specs: Sequence[Tuple[str, str, Any]],
) -> Tuple[Any, List[Any], Dict[str, Any]]:
    from zoofan.models import Source, Zoo

    zoo = Zoo(
        id=zoo_id,
        slug=slug,
        name=name,
        website_url="https://" + host + "/",
        country_code="DE",
        language="en",
    )
    storage.upsert_zoo(zoo)
    sources: List[Any] = []
    payloads: Dict[str, Any] = {}
    for source_id, path, value in source_specs:
        source_url = "https://" + host + path
        source = Source(
            id=source_id,
            zoo_id=zoo_id,
            kind="rss",
            url=source_url,
            name="News",
            language="en",
            config={"official_host": host, "allow_regex": r"/news/"},
        )
        storage.upsert_source(source)
        sources.append(source)
        payloads[source_url] = value
    return zoo, sources, payloads


def _feed(urls: Iterable[str]) -> str:
    return "<rss><channel>" + "".join(
        "<item><title>Fixture article</title><link>" + url + "</link></item>"
        for url in urls
    ) + "</channel></rss>"


def _article(url: str, title: str = "Fixture article") -> str:
    return (
        "<html><head><title>"
        + title
        + "</title></head><article>Fixture body for "
        + url
        + "</article></html>"
    )


def _run_scenarios(storage: Any) -> Dict[str, Any]:
    from zoofan.config import CrawlerConfig
    from zoofan.crawler import Crawler
    from zoofan.events import CrawlEventRecorder
    from zoofan.models import FetchResponse

    scenarios: Dict[str, Any] = {}

    normal_url = "https://normal.example/news/one"
    normal_zoo, normal_sources, normal_payloads = _fixture_records(
        storage,
        zoo_id="accept-normal-zoo",
        slug="accept-normal-zoo",
        name="Acceptance Normal Zoo",
        host="normal.example",
        source_specs=[("accept-normal-source", "/feed", _feed([normal_url]))],
    )
    normal_payloads[normal_url] = _article(normal_url, "Normal fixture article")
    normal_result = Crawler(
        CrawlerConfig(zoos=[normal_zoo], sources=normal_sources, request_delay=0),
        storage=storage,
        fetcher=FixtureFetcher(normal_payloads),
        event_sink=CrawlEventRecorder(storage),
    ).crawl()
    scenarios["Normal Crawl"] = {
        "result": normal_result,
        "zoo_id": normal_zoo.id,
        "source_id": normal_sources[0].id,
        "article_url": normal_url,
        "run_id": _obj(_obj(normal_result, "run"), "id"),
        "expected": "completed / stored article / lifecycle events",
    }

    budget_urls = [
        "https://budget.example/news/one",
        "https://budget.example/news/two",
    ]
    budget_zoo, budget_sources, budget_payloads = _fixture_records(
        storage,
        zoo_id="accept-budget-zoo",
        slug="accept-budget-zoo",
        name="Acceptance Budget Zoo",
        host="budget.example",
        source_specs=[("accept-budget-source", "/feed", _feed(budget_urls))],
    )
    for index, url in enumerate(budget_urls, 1):
        budget_payloads[url] = _article(url, "Budget fixture article " + str(index))
    budget_result = Crawler(
        CrawlerConfig(zoos=[budget_zoo], sources=budget_sources, request_delay=0),
        storage=storage,
        fetcher=FixtureFetcher(budget_payloads),
        event_sink=CrawlEventRecorder(storage),
    ).next_batch(budget_sources[0].id, limit=1)
    scenarios["Budget Hit"] = {
        "result": budget_result,
        "zoo_id": budget_zoo.id,
        "source_id": budget_sources[0].id,
        "run_id": _obj(_obj(budget_result, "run"), "id"),
        "expected": "structured stop_reason=article_limit",
    }

    failure_article_url = "https://failure.example/news/recovered"
    failure_zoo, failure_sources, failure_payloads = _fixture_records(
        storage,
        zoo_id="accept-failure-zoo",
        slug="accept-failure-zoo",
        name="Acceptance Failure Zoo",
        host="failure.example",
        source_specs=[
            (
                "accept-failure-source",
                "/bad-feed",
                FetchResponse(
                    "https://failure.example/bad-feed",
                    503,
                    b"fixture HTTP 503",
                ),
            ),
            ("accept-recovery-source", "/good-feed", _feed([failure_article_url])),
        ],
    )
    failure_payloads[failure_article_url] = _article(
        failure_article_url, "Recovered fixture article"
    )
    failure_result = Crawler(
        CrawlerConfig(zoos=[failure_zoo], sources=failure_sources, request_delay=0),
        storage=storage,
        fetcher=FixtureFetcher(failure_payloads),
        event_sink=CrawlEventRecorder(storage),
    ).crawl()
    scenarios["Failure / Source Isolation"] = {
        "result": failure_result,
        "zoo_id": failure_zoo.id,
        "source_id": failure_sources[0].id,
        "recovery_source_id": failure_sources[1].id,
        "run_id": _obj(_obj(failure_result, "run"), "id"),
        "expected": "HTTP 503 source failure isolated; recovery source stores article",
        "failure_kind": "http_error",
        "failure_status_code": 503,
    }
    return scenarios


def _scenario_checks(report: Report, storage: Any, scenarios: Mapping[str, Any]) -> None:
    required_scenarios = ("Normal Crawl", "Budget Hit", "Failure / Source Isolation")
    for name in required_scenarios:
        scenario = scenarios.get(name)
        if not isinstance(scenario, Mapping):
            report.add(
                "Scenario Validation",
                name,
                "BLOCKED",
                {"error": "required deterministic scenario was not produced"},
            )
            continue
        result = scenario.get("result")
        run_id = scenario.get("run_id")
        run = storage.get_crawl_run(run_id) if run_id else None
        stats = storage.list_run_stats(run_id) if run_id else []
        events = [_event_dict(item) for item in storage.list_crawl_run_events(run_id)] if run_id else []
        relations = storage.list_crawl_run_articles(run_id) if run_id else []
        event_types = [str(item.get("event_type")) for item in events]
        stat_evidence: List[Dict[str, Any]] = []
        for item in stats:
            metadata = _obj(item, "metadata", {}) or {}
            source = storage.get_source(_obj(item, "source_id")) if _obj(item, "source_id") else None
            stat_evidence.append(
                {
                    "source_id": _obj(item, "source_id"),
                    "status": _obj(item, "status"),
                    "stored": _obj(item, "stored_count", 0),
                    "errors": _obj(item, "error_count", 0),
                    "error": _obj(item, "error"),
                    "error_category": metadata.get("error_classification")
                    if isinstance(metadata, Mapping)
                    else None,
                    "stop_reason": metadata.get("stop_reason")
                    if isinstance(metadata, Mapping)
                    else None,
                    "last_http_status": _obj(source, "last_http_status") if source else None,
                }
            )
        failure_events = [
            item
            for item in events
            if item.get("event_type") == "http_error"
        ]
        evidence = {
            "run_id": run_id,
            "result_status": _obj(result, "status"),
            "result_stop_reason": _result_stop_reason(result),
            "db_status": _obj(run, "status"),
            "db_stop_reason": _obj(run, "stop_reason"),
            "stored": _obj(result, "stored_count", 0),
            "stats": stat_evidence,
            "event_types": event_types,
            "run_article_relations": len(relations),
        }
        passed = False
        if name == "Normal Crawl":
            passed = (
                evidence["result_status"] == "completed"
                and int(evidence["stored"] or 0) >= 1
                and evidence["db_status"] == "completed"
                and evidence["run_article_relations"] >= 1
                and {"crawl_started", "source_started", "article_stored", "source_completed", "crawl_completed"}
                <= set(event_types)
            )
        elif name == "Budget Hit":
            budget_events = [
                item for item in events if item.get("event_type") == "crawl_budget_hit"
            ]
            passed = (
                evidence["result_stop_reason"] == "article_limit"
                and evidence["db_stop_reason"] == "article_limit"
                and bool(budget_events)
                and any(
                    (_obj(item, "metadata", {}) or {}).get("stop_reason") == "article_limit"
                    for item in budget_events
                )
            )
        elif name == "Failure / Source Isolation":
            failed_source = any(
                _obj(item, "source_id") == scenario.get("source_id")
                and _obj(item, "status") == "error"
                for item in stats
            )
            recovered = any(
                _obj(item, "source_id") == scenario.get("recovery_source_id")
                and int(_obj(item, "stored_count", 0) or 0) >= 1
                for item in stats
            )
            failed_stat = next(
                (
                    item
                    for item in stat_evidence
                    if item.get("source_id") == scenario.get("source_id")
                ),
                {},
            )
            recovery_stat = next(
                (
                    item
                    for item in stat_evidence
                    if item.get("source_id") == scenario.get("recovery_source_id")
                ),
                {},
            )
            http_event = failure_events[0] if failure_events else {}
            http_metadata = http_event.get("metadata") if isinstance(http_event, Mapping) else {}
            if not isinstance(http_metadata, Mapping):
                http_metadata = {}
            passed = (
                evidence["result_status"] == "completed_with_errors"
                and failed_source
                and recovered
                and "source_failed" in event_types
                and "http_error" in event_types
                and evidence["result_stop_reason"] == "http_error"
                and evidence["db_stop_reason"] == "http_error"
                and failed_stat.get("error_category") == "http_error"
                and failed_stat.get("last_http_status") == scenario.get("failure_status_code")
                and recovery_stat.get("status") == "completed"
                and recovery_stat.get("stored", 0) >= 1
                and http_event.get("source_id") == scenario.get("source_id")
                and http_metadata.get("status_code") == scenario.get("failure_status_code")
                and http_metadata.get("stop_reason") == "http_error"
            )
            evidence.update(
                {
                    "failure_kind": scenario.get("failure_kind"),
                    "failure_status_code": scenario.get("failure_status_code"),
                    "http_error_event": {
                        "source_id": http_event.get("source_id"),
                        "metadata": dict(http_metadata),
                    },
                    "recovery_source": recovery_stat,
                }
            )
        report.add("Scenario Validation", name, "PASS" if passed else "FAIL", evidence)


def _json_response(client: Any, path: str) -> Tuple[int, Dict[str, Any], str]:
    response = client.get(path, headers={"Accept": "application/json"})
    body_text = response.get_data(as_text=True)
    try:
        payload = response.get_json() or {}
    except (TypeError, ValueError):
        payload = {}
    return response.status_code, payload, body_text


def _collection_items(payload: Mapping[str, Any], *keys: str) -> List[Mapping[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
    return []


def _concurrent_http_checks(
    report: Report,
    app: Any,
    scenarios: Mapping[str, Any],
) -> None:
    """Exercise request-scoped console services through a real threaded server."""

    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    from werkzeug.serving import make_server

    normal = scenarios["Normal Crawl"]
    normal_run = str(normal["run_id"])
    expected: Dict[str, Dict[str, Any]] = {
        "runs": {
            "path": "/api/crawl-runs?limit=20",
            "count": len(scenarios),
            "id": normal_run,
        },
        "run_detail": {
            "path": "/api/crawl-runs/" + normal_run,
            "id": normal_run,
        },
        "zoos": {
            "path": "/api/crawl-runs/" + normal_run + "/zoos",
            "count": 1,
            "id": str(normal["zoo_id"]),
        },
        "sources": {
            "path": "/api/crawl-runs/" + normal_run + "/sources",
            "count": 1,
            "id": str(normal["source_id"]),
        },
        "articles": {
            "path": "/api/crawl-runs/" + normal_run + "/articles",
            "count": 1,
            "url": str(normal["article_url"]),
        },
    }
    # Five passes over five distinct endpoint families provide 25 genuinely
    # concurrent HTTP requests while keeping every response independently
    # verifiable.
    requests = [
        (name, str(spec["path"]))
        for _pass in range(5)
        for name, spec in expected.items()
    ]
    server: Any = None
    thread: Optional[threading.Thread] = None
    responses: List[Dict[str, Any]] = []

    def fetch(item: Tuple[str, str]) -> Dict[str, Any]:
        name, path = item
        request = Request(base_url + path, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:
                body = response.read().decode("utf-8")
                return {
                    "name": name,
                    "path": path,
                    "status": int(response.status),
                    "payload": json.loads(body),
                }
        except HTTPError as error:
            return {"name": name, "path": path, "status": error.code, "error": str(error)}
        except (OSError, URLError, ValueError) as error:
            return {"name": name, "path": path, "status": None, "error": str(error)}

    base_url = ""
    try:
        server = make_server("127.0.0.1", 0, app, threaded=True)
        base_url = "http://127.0.0.1:" + str(server.server_port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with ThreadPoolExecutor(max_workers=10) as executor:
            responses = list(executor.map(fetch, requests))
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)

    endpoint_results: List[Dict[str, Any]] = []
    all_passed = len(responses) == len(requests)
    for result in responses:
        name = str(result["name"])
        spec = expected[name]
        payload_value = result.get("payload")
        if isinstance(payload_value, Mapping):
            payload: Mapping[str, Any] = payload_value
            ok = result.get("status") == 200
        else:
            payload = {}
            ok = False
        observed_count: Optional[int] = None
        observed_ids: List[str] = []
        if ok and name == "runs":
            items = _collection_items(payload, "runs", "items", "results")
            observed_count = len(items)
            observed_ids = [str(item.get("id")) for item in items]
            ok = observed_count == spec["count"] and spec["id"] in observed_ids
        elif ok and name == "run_detail":
            run_value = payload.get("run")
            run: Mapping[str, Any] = run_value if isinstance(run_value, Mapping) else payload
            observed_ids = [str(run.get("id"))]
            ok = observed_ids == [spec["id"]]
        elif ok and name == "zoos":
            items = _collection_items(payload, "zoos", "items", "results")
            observed_count = len(items)
            observed_ids = [str(item.get("zoo_id")) for item in items]
            ok = observed_count == spec["count"] and observed_ids == [spec["id"]]
        elif ok and name == "sources":
            items = _collection_items(payload, "sources", "items", "results")
            observed_count = len(items)
            observed_ids = [str(item.get("source_id")) for item in items]
            ok = observed_count == spec["count"] and observed_ids == [spec["id"]]
        elif ok and name == "articles":
            items = _collection_items(payload, "articles", "items", "results")
            observed_count = len(items)
            observed_ids = [str(item.get("id") or item.get("article_id")) for item in items]
            ok = (
                observed_count == spec["count"]
                and [str(item.get("canonical_url")) for item in items] == [spec["url"]]
                and all(observed_id not in {"", "None"} for observed_id in observed_ids)
            )
        all_passed = all_passed and ok
        endpoint_results.append(
            {
                "endpoint": result["path"],
                "http_status": result.get("status"),
                "count": observed_count,
                "ids": observed_ids,
                "error": result.get("error"),
                "result": "PASS" if ok else "FAIL",
            }
        )

    server_stopped = thread is None or not thread.is_alive()
    all_passed = all_passed and len(requests) >= 20 and server_stopped
    report.add(
        "API Validation",
        "Threaded HTTP requests preserve run, zoo, source, and article data",
        "PASS" if all_passed else "FAIL",
        {
            "threaded_server": True,
            "request_count": len(requests),
            "response_count": len(responses),
            "all_http_200": bool(responses) and all(item.get("status") == 200 for item in responses),
            "server_stopped": server_stopped,
            "responses": endpoint_results,
        },
    )


def _api_checks(
    report: Report,
    database_path: Path,
    scenarios: Mapping[str, Any],
) -> Any:
    from zoofan.dashboard import create_app

    # Use the product factory exactly as deployed: each request constructs and
    # tears down its own read-only service over a real SQLite file.
    app = create_app(database_path)
    client = app.test_client()
    normal = scenarios["Normal Crawl"]
    normal_run = normal["run_id"]
    normal_zoo = normal["zoo_id"]

    global_paths = [
        "/api/crawler/status",
        "/api/crawl-runs?limit=20",
        "/api/zoos?limit=20",
        "/api/zoos/" + str(normal_zoo),
        "/api/zoos/" + str(normal_zoo) + "/crawl-history",
    ]
    global_evidence: List[Dict[str, Any]] = []
    all_passed = True
    for path in global_paths:
        status, payload, _ = _json_response(client, path)
        ok = status == 200
        all_passed = all_passed and ok
        global_evidence.append({"endpoint": path, "http_status": status, "result": "PASS" if ok else "FAIL"})
    report.add("API Validation", "Global status, runs, zoos, and history endpoints", "PASS" if all_passed else "FAIL", global_evidence)

    resource_evidence: List[Dict[str, Any]] = []
    required_routes = [
        ("run", "/api/crawl-runs/" + str(normal_run)),
        ("zoos", "/api/crawl-runs/" + str(normal_run) + "/zoos"),
        ("articles", "/api/crawl-runs/" + str(normal_run) + "/articles"),
        ("events", "/api/crawl-runs/" + str(normal_run) + "/events"),
    ]
    for label, path in required_routes:
        status, payload, body_text = _json_response(client, path)
        ok = status == 200 and bool(payload)
        if label in {"articles", "events"}:
            ok = ok and "raw_html" not in body_text
        all_passed = all_passed and ok
        resource_evidence.append({"endpoint": path, "http_status": status, "result": "PASS" if ok else "FAIL", "raw_html_exposed": "raw_html" in body_text})
    report.add("API Validation", "Run subresources and raw_html boundary", "PASS" if all_passed else "FAIL", resource_evidence)

    # Exercise the same read boundary for every deterministic scenario.  The
    # fixture's DB checks above are not enough: the structured stop reason and
    # source-isolation evidence must survive repository -> service -> API.
    for name, scenario in scenarios.items():
        run_id = str(scenario.get("run_id"))
        run_status, run_payload, _ = _json_response(client, "/api/crawl-runs/" + run_id)
        zoo_status, zoo_payload, _ = _json_response(client, "/api/crawl-runs/" + run_id + "/zoos")
        event_status, scenario_event_payload, _ = _json_response(client, "/api/crawl-runs/" + run_id + "/events?limit=200")
        run_view = run_payload.get("run") if isinstance(run_payload.get("run"), Mapping) else run_payload
        zoo_items = zoo_payload.get("zoos") or zoo_payload.get("items") or []
        scenario_events = scenario_event_payload.get("events") or scenario_event_payload.get("items") or []
        scenario_event_types = {str(item.get("event_type")) for item in scenario_events if isinstance(item, Mapping)}
        api_run_status = (
            run_view.get("classified_status")
            or run_view.get("status_key")
            or run_view.get("status")
        )
        api_status = str(api_run_status or "").casefold()
        raw_status = run_view.get("raw_status")
        if raw_status in (None, ""):
            raw_status = run_view.get("run_status")
        terminal_api_statuses = {
            "success",
            "budget_hit",
            "warning",
            "failed",
        }
        api_http_events = [
            item
            for item in scenario_events
            if isinstance(item, Mapping) and item.get("event_type") == "http_error"
        ]
        api_http_event = api_http_events[0] if api_http_events else {}
        api_http_metadata = api_http_event.get("metadata") if isinstance(api_http_event, Mapping) else {}
        if not isinstance(api_http_metadata, Mapping):
            api_http_metadata = {}
        api_recovery_events = [
            item
            for item in scenario_events
            if isinstance(item, Mapping)
            and item.get("event_type") == "article_stored"
            and item.get("source_id") == scenario.get("recovery_source_id")
        ]
        if name == "Normal Crawl":
            ok = (
                run_status == 200
                and zoo_status == 200
                and event_status == 200
                and api_status == "success"
                and api_status in terminal_api_statuses
                and "article_stored" in scenario_event_types
            )
        elif name == "Budget Hit":
            ok = (
                run_status == 200
                and zoo_status == 200
                and event_status == 200
                and run_view.get("stop_reason") == "article_limit"
                and api_status == "budget_hit"
                and api_status in terminal_api_statuses
                and any(
                    isinstance(item, Mapping)
                    and (item.get("stop_reason") == "article_limit" or item.get("error_category") == "article_limit")
                    for item in zoo_items
                )
                and "crawl_budget_hit" in scenario_event_types
            )
        else:
            ok = (
                run_status == 200
                and zoo_status == 200
                and event_status == 200
                and api_status in {"warning", "failed"}
                and api_status in terminal_api_statuses
                and run_view.get("stop_reason") == "http_error"
                and {"source_failed", "http_error"} <= scenario_event_types
                and api_http_event.get("source_id") == scenario.get("source_id")
                and api_http_metadata.get("status_code") == scenario.get("failure_status_code")
                and api_http_metadata.get("stop_reason") == "http_error"
                and bool(api_recovery_events)
            )
        api_evidence = {
            "run_endpoint_status": run_status,
            "zoo_endpoint_status": zoo_status,
            "events_endpoint_status": event_status,
            "api_run_status": api_run_status,
            "api_stop_reason": run_view.get("stop_reason"),
            "stop_reason": run_view.get("stop_reason"),
            "api_zoo_count": len(zoo_items),
            "api_event_types": sorted(scenario_event_types),
            "http_error_event": {
                "source_id": api_http_event.get("source_id"),
                "metadata": dict(api_http_metadata),
            },
            "recovery_article_stored": len(api_recovery_events),
        }
        if raw_status not in (None, ""):
            api_evidence["raw_status"] = raw_status
        report.add(
            "Scenario Validation",
            name + " API evidence",
            "PASS" if ok else "FAIL",
            api_evidence,
        )

    event_status, event_payload, event_text = _json_response(
        client, "/api/crawl-runs/" + str(normal_run) + "/events?limit=1"
    )
    event_items = event_payload.get("events") or event_payload.get("items") or []
    cursor_ok = False
    cursor_evidence: Dict[str, Any] = {"initial_status": event_status, "initial_count": len(event_items)}
    if event_items and isinstance(event_items[0], Mapping) and event_items[0].get("id") is not None:
        cursor = int(event_items[0]["id"])
        after_status, after_payload, _ = _json_response(
            client,
            "/api/crawl-runs/" + str(normal_run) + "/events?after_id=" + str(cursor),
        )
        after_items = after_payload.get("events") or after_payload.get("items") or []
        cursor_ok = after_status == 200 and all(int(item.get("id", 0)) > cursor for item in after_items)
        cursor_evidence.update({"cursor": cursor, "after_status": after_status, "after_ids": [item.get("id") for item in after_items]})
    report.add("API Validation", "Events support incremental after_id pagination", "PASS" if cursor_ok else "FAIL", cursor_evidence)

    missing_status, missing_payload, _ = _json_response(client, "/api/crawl-runs/missing-acceptance-run")
    missing_ok = missing_status == 404 and missing_payload.get("error", {}).get("code") == "not_found"
    report.add("API Validation", "Missing run returns stable 404 JSON", "PASS" if missing_ok else "FAIL", {"http_status": missing_status, "payload": missing_payload})

    _concurrent_http_checks(report, app, scenarios)
    return app, client


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _imports(path: Path) -> List[str]:
    try:
        tree = ast.parse(_read(path), filename=str(path))
    except SyntaxError:
        return []
    result: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.append(node.module)
    return result


def _max_function_lines(paths: Sequence[Path]) -> Tuple[int, str]:
    maximum = 0
    owner = "none"
    for path in paths:
        try:
            tree = ast.parse(_read(path), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                end = getattr(node, "end_lineno", node.lineno)
                size = int(end) - int(node.lineno) + 1
                if size > maximum:
                    maximum = size
                    owner = str(path.relative_to(ROOT)) + ":" + node.name
    return maximum, owner


def _ast_tree(path: Path) -> Optional[ast.AST]:
    try:
        return ast.parse(_read(path), filename=str(path))
    except SyntaxError:
        return None


def _has_sql_execution(path: Path) -> bool:
    tree = _ast_tree(path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in {"execute", "executemany", "executescript"}:
            return True
    return False


def _schema_contract(storage: Any) -> Dict[str, Any]:
    connection = getattr(storage, "connection", None)
    if not isinstance(connection, sqlite3.Connection):
        return {"available": False, "reason": "fixture storage connection unavailable"}

    def table_columns(table: str) -> set[str]:
        rows = connection.execute("PRAGMA table_info(" + table + ")").fetchall()
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in rows
        }

    def index_names(table: str) -> set[str]:
        rows = connection.execute("PRAGMA index_list(" + table + ")").fetchall()
        return {
            str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
            for row in rows
        }

    tables = {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required_columns = {
        "crawl_runs": {
            "stop_reason",
            "heartbeat_at",
            "current_zoo_id",
            "current_source_id",
            "progress_json",
        },
        "crawl_run_stats": {"stop_reason"},
        "crawl_zoo_results": {"stop_reason"},
        "crawl_run_events": {
            "id",
            "run_id",
            "zoo_id",
            "source_id",
            "created_at",
            "level",
            "component",
            "event_type",
            "message",
            "metadata_json",
        },
        "crawl_run_articles": {
            "run_id",
            "article_id",
            "zoo_id",
            "source_id",
            "outcome",
            "created_at",
            "metadata_json",
        },
    }
    columns = {
        table: sorted(required - table_columns(table))
        for table, required in required_columns.items()
    }
    required_indexes = {
        "crawl_run_events": {
            "idx_crawl_run_events_run_id_id",
            "idx_crawl_run_events_run_scope_id",
        },
        "crawl_run_articles": {"idx_crawl_run_articles_run_created_id"},
    }
    indexes = {
        table: sorted(required - index_names(table))
        for table, required in required_indexes.items()
    }
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    except (TypeError, ValueError, sqlite3.Error):
        user_version = None
    try:
        from zoofan.storage import SQLiteStorage

        schema_version = int(SQLiteStorage.SCHEMA_VERSION)
    except (ImportError, TypeError, ValueError, AttributeError):
        schema_version = None
    table_ok = {table: table in tables for table in required_columns}
    schema_ok = (
        schema_version is not None
        and user_version == schema_version
        and all(table_ok.values())
        and not any(columns.values())
        and not any(indexes.values())
    )
    return {
        "available": True,
        "schema_version": schema_version,
        "sqlite_user_version": user_version,
        "tables": table_ok,
        "missing_columns": columns,
        "missing_indexes": indexes,
        "ok": schema_ok,
    }


def _architecture_checks(report: Report, storage_fixture: Any = None) -> None:
    api = ROOT / "zoofan" / "console_api.py"
    service = ROOT / "zoofan" / "console_service.py"
    repository = ROOT / "zoofan" / "console_repository.py"
    events = ROOT / "zoofan" / "events.py"
    storage_path = ROOT / "zoofan" / "storage.py"
    crawler = ROOT / "zoofan" / "crawler.py"
    templates = list((ROOT / "zoofan" / "templates").glob("console_*.html"))
    modules = [api, service, repository, events, storage_path, crawler]
    module_presence = all(path.is_file() for path in modules) and bool(templates)

    report.add(
        "Architecture Validation",
        "Console feature modules and templates are present",
        "PASS" if module_presence else "FAIL",
        {"modules": [str(path.relative_to(ROOT)) for path in modules], "template_count": len(templates)},
    )

    api_text = _read(api)
    events_text = _read(events)
    storage_text = _read(storage_path)
    crawler_text = _read(crawler)
    template_text = "\n".join(_read(path) for path in templates)
    api_imports = _imports(api)
    service_imports = _imports(service)
    forbidden_api_imports = sorted(
        item
        for item in api_imports
        if item == "sqlite3"
        or item == "zoofan.storage"
        or item == "zoofan.console_repository"
        or item.startswith("zoofan.storage.")
    )
    forbidden_service_imports = sorted(
        item
        for item in service_imports
        if item == "sqlite3" or item == "zoofan.storage" or item.startswith("zoofan.storage.")
    )
    api_sql = bool(forbidden_api_imports) or _has_sql_execution(api)
    service_sql = bool(forbidden_service_imports) or _has_sql_execution(service)
    sql_select = r"SELECT\s+(?:\*|[A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s+FROM"
    frontend_forbidden_tokens = sorted(
        token
        for token, present in {
            "sqlite3": "sqlite3" in template_text.lower(),
            "PRAGMA": bool(re.search(r"\bPRAGMA\b", template_text, re.IGNORECASE)),
            "SQL SELECT": bool(re.search(sql_select, template_text, re.IGNORECASE)),
        }.items()
        if present
    )
    frontend_api_contract = {
        "fetch": "fetch(" in template_text,
        "api_url_bindings": "crawler_console.api_" in template_text,
        "forbidden_sql_tokens": frontend_forbidden_tokens,
        "raw_html_token": "raw_html" in template_text,
    }
    no_sql_in_frontend = not frontend_forbidden_tokens and frontend_api_contract["fetch"] and frontend_api_contract["api_url_bindings"]
    report.add(
        "Architecture Validation",
        "Frontend does not query SQLite directly",
        "PASS" if no_sql_in_frontend else "FAIL",
        frontend_api_contract,
    )
    report.add(
        "Architecture Validation",
        "API and service layers are read-boundary adapters, not SQL owners",
        "PASS" if not api_sql and not service_sql and _has_sql_execution(repository) else "FAIL",
        {
            "api_has_sql": api_sql,
            "service_has_sql": service_sql,
            "repository_owns_sql": _has_sql_execution(repository),
            "api_forbidden_imports": forbidden_api_imports,
            "service_forbidden_imports": forbidden_service_imports,
        },
    )

    crawler_imports = set(_imports(crawler))
    forbidden_crawler_imports = sorted(
        item
        for item in crawler_imports
        if item in {"flask", "zoofan.dashboard", "zoofan.console_api"}
        or item.startswith("flask.")
    )
    crawler_frontend_ok = (
        not forbidden_crawler_imports
        and "_emit_event" in crawler_text
    )
    report.add(
        "Architecture Validation",
        "Crawler is independent of Flask/frontend and emits through a hook",
        "PASS" if crawler_frontend_ok else "FAIL",
        {"forbidden_imports": forbidden_crawler_imports, "event_hook_present": "_emit_event" in crawler_text},
    )

    event_isolated = (
        "class EventSink" in events_text
        and "class CrawlEventRecorder" in events_text
        and "crawl_run_events" in storage_text
        and "record_crawl_event" in storage_text
        and not _has_sql_execution(events)
    )
    report.add(
        "Architecture Validation",
        "Event persistence has an explicit isolated boundary",
        "PASS" if event_isolated else "FAIL",
        {
            "event_sink": "class EventSink" in events_text,
            "recorder": "class CrawlEventRecorder" in events_text,
            "storage_event_table": "crawl_run_events" in storage_text,
            "event_module_owns_sql": _has_sql_execution(events),
        },
    )

    schema_evidence = _schema_contract(storage_fixture)
    migration_source_ok = (
        "SCHEMA_VERSION" in storage_text
        and "def _migrate_schema" in storage_text
        and "CREATE TABLE IF NOT EXISTS" in storage_text
        and "CREATE INDEX IF NOT EXISTS" in storage_text
    )
    migration_ok = migration_source_ok and bool(schema_evidence.get("ok"))
    report.add(
        "Architecture Validation",
        "SQLite schema/version/migration/index contract is verified",
        "PASS" if migration_ok else "FAIL",
        {
            "migration_source": migration_source_ok,
            "schema": schema_evidence,
        },
    )

    dashboard_text = _read(ROOT / "zoofan" / "dashboard.py")
    second_server = "Flask(" in api_text or "app.run(" in api_text
    same_app_ok = (
        "from .console_api import create_console_blueprint" in dashboard_text
        and "app.register_blueprint(create_console_blueprint" in dashboard_text
        and not second_server
    )
    report.add(
        "Architecture Validation",
        "Console API is mounted in the existing Flask app without a second server",
        "PASS" if same_app_ok else "FAIL",
        {
            "dashboard_registers_console_blueprint": "app.register_blueprint(create_console_blueprint" in dashboard_text,
            "console_api_constructs_flask_app": "Flask(" in api_text,
            "console_api_starts_server": "app.run(" in api_text,
        },
    )

    verifier_text = _read(Path(__file__).resolve())
    shared_connection_override = "check_same_thread" + "=False"
    unsafe_cleanup_tokens = [
        token
        for token in (
            "shutil." + "rmtree",
            "rmtree" + "(",
            "os." + "remove" + "(",
            "os." + "unlink" + "(",
            "." + "unlink" + "(",
            "rm" + " -rf",
            "data" + "/zoofan.db",
            "artifacts" + "/",
        )
        if token in verifier_text
    ]
    bounded_fixture_ok = (
        not unsafe_cleanup_tokens
        and "TemporaryDirectory" in verifier_text
        and "console-acceptance.db" in verifier_text
        and shared_connection_override not in verifier_text
    )
    report.add(
        "Verifier Safety",
        "Fixture and cleanup scope is bounded to temporary state",
        "PASS" if bounded_fixture_ok else "FAIL",
        {
            "unsafe_cleanup_tokens": unsafe_cleanup_tokens,
            "uses_temporary_directory": "TemporaryDirectory" in verifier_text,
            "uses_temporary_file_sqlite": "console-acceptance.db" in verifier_text,
            "shared_cross_thread_connection_override": shared_connection_override in verifier_text,
            "report_path": str(REPORT_PATH),
        },
    )

    feature_modules = [api, service, repository, events]
    max_lines, owner = _max_function_lines(feature_modules)
    forbidden_infra = sorted(
        token
        for token in ("kafka", "redis", "celery", "elasticsearch", "prometheus", "loki")
        if any(token in _read(path).lower() for path in feature_modules)
    )
    god_ok = max_lines <= 350 and not forbidden_infra
    report.add(
        "Architecture Validation",
        "No major console god function or unnecessary heavy infrastructure",
        "PASS" if god_ok else "FAIL",
        {"largest_function_lines": max_lines, "largest_function": owner, "heavy_infrastructure_tokens": forbidden_infra},
    )


def _ui_checks(report: Report, client: Any, scenarios: Mapping[str, Any]) -> None:
    normal = scenarios["Normal Crawl"]
    paths = [
        "/console",
        "/console/overview",
        "/console/runs",
        "/console/runs/" + str(normal["run_id"]),
        "/console/zoos/" + str(normal["zoo_id"]),
        "/console/articles?run_id=" + str(normal["run_id"]),
        "/console/events?run_id=" + str(normal["run_id"]),
    ]
    evidence: List[Dict[str, Any]] = []
    passed = True
    for path in paths:
        response = client.get(path)
        body = response.get_data(as_text=True)
        ok = response.status_code == 200 and "data-console-page" in body and "raw_html" not in body
        passed = passed and ok
        evidence.append({"path": path, "http_status": response.status_code, "result": "PASS" if ok else "FAIL", "raw_html_exposed": "raw_html" in body})
    report.add("UI Validation", "Overview, Runs, Run Detail, Zoo Detail, Articles, and Events render", "PASS" if passed else "FAIL", evidence)

    failure = scenarios.get("Failure / Source Isolation")
    failure_ui_evidence: List[Dict[str, Any]] = []
    failure_ui_passed = isinstance(failure, Mapping)
    if isinstance(failure, Mapping):
        failure_run_id = str(failure.get("run_id"))
        failure_paths = [
            "/console/runs/" + failure_run_id,
            "/console/events?run_id=" + failure_run_id,
        ]
        events_status, events_payload, events_body = _json_response(
            client,
            "/api/crawl-runs/" + failure_run_id + "/events?limit=200",
        )
        event_items = events_payload.get("events") or events_payload.get("items") or []
        http_event_visible_to_ui = any(
            isinstance(item, Mapping)
            and item.get("event_type") == "http_error"
            and item.get("source_id") == failure.get("source_id")
            for item in event_items
        )
        for path in failure_paths:
            response = client.get(path)
            body = response.get_data(as_text=True)
            expected_run_marker = failure_run_id in body
            expected_events_api = (
                "/api/crawl-runs/" + failure_run_id + "/events"
            ) in body
            ok = (
                response.status_code == 200
                and "data-console-page" in body
                and expected_run_marker
                and expected_events_api
                and "raw_html" not in body
            )
            failure_ui_passed = failure_ui_passed and ok
            failure_ui_evidence.append(
                {
                    "path": path,
                    "http_status": response.status_code,
                    "run_marker": expected_run_marker,
                    "events_api_binding": expected_events_api,
                    "raw_html_exposed": "raw_html" in body,
                    "result": "PASS" if ok else "FAIL",
                }
            )
        failure_ui_evidence.append(
            {
                "failure_event_api_status": events_status,
                "failure_event_api_body_has_raw_html": "raw_html" in events_body,
                "http_error_event_visible_to_ui_data": http_event_visible_to_ui,
            }
        )
        failure_ui_passed = failure_ui_passed and events_status == 200 and http_event_visible_to_ui
    report.add(
        "UI Validation",
        "Failure scenario UI route preserves HTTP/source/event evidence contract",
        "PASS" if failure_ui_passed else "FAIL",
        failure_ui_evidence,
    )

    template_text = "\n".join(_read(path) for path in (ROOT / "zoofan" / "templates").glob("console_*.html"))
    polling_ok = all(token in template_text for token in ("setInterval", "2500", "after_id", "clearInterval"))
    report.add("UI Validation", "Polling and incremental event refresh contract", "PASS" if polling_ok else "FAIL", {"setInterval": "setInterval" in template_text, "interval_ms_2500": "2500" in template_text, "after_id": "after_id" in template_text, "clearInterval": "clearInterval" in template_text})


def _browser_check(report: Report, app: Any, scenarios: Mapping[str, Any]) -> None:
    """Attempt a real headless browser only when the installed browser is usable."""

    try:
        from werkzeug.serving import make_server
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError) as error:
        report.known_limitations.append("Browser visual check unavailable: Playwright is not installed.")
        report.add("UI Validation", "Browser visual check", "UNAVAILABLE", str(error), required=False)
        return

    server: Any = None
    thread: Optional[threading.Thread] = None
    browser: Any = None
    browser_started = False
    try:
        # Flask's development server would be unsafe to run in a verifier; a
        # disposable Werkzeug server is enough for a localhost browser smoke.
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser_started = True
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.goto(url + "/console/overview", wait_until="networkidle", timeout=15000)
                visible = "Crawler Console" in page.locator("body").inner_text()
                page.goto(url + "/console/runs/" + str(scenarios["Normal Crawl"]["run_id"]), wait_until="networkidle", timeout=15000)
                detail_body = page.locator("body").inner_text()
                detail_visible = page.locator("[data-console-page='run-detail']").count() == 1 and "Outcome" in detail_body
                page.goto(url + "/console/runs/" + str(scenarios["Failure / Source Isolation"]["run_id"]), wait_until="networkidle", timeout=15000)
                failure_body = page.locator("body").inner_text()
                failure_visible = (
                    "503" in failure_body
                    and ("http_error" in failure_body or "Source failed" in failure_body)
                )
                screenshot = page.screenshot(type="png", full_page=True)
                report.screenshot_data_uri = "data:image/png;base64," + base64.b64encode(screenshot).decode("ascii")
            finally:
                browser.close()
        passed = visible and detail_visible and failure_visible
        report.add(
            "UI Validation",
            "Browser visual check",
            "PASS" if passed else "FAIL",
            {
                "overview_visible": visible,
                "run_detail_visible": detail_visible,
                "failure_http_evidence_visible": failure_visible,
                "screenshot_embedded": bool(report.screenshot_data_uri),
            },
            required=True,
        )
    except Exception as error:
        if browser_started:
            report.add("UI Validation", "Browser visual check", "FAIL", traceback.format_exc(limit=3), required=True)
        else:
            report.known_limitations.append("Browser visual check unavailable: " + type(error).__name__ + ": " + str(error))
            report.add("UI Validation", "Browser visual check", "UNAVAILABLE", traceback.format_exc(limit=3), required=False)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)


def _render_report(report: Report) -> str:
    grouped: Dict[str, List[Check]] = {}
    for check in report.checks:
        grouped.setdefault(check.section, []).append(check)
    regression = report.regression
    status_class = "pass" if report.status == "PASS" else "fail"

    def esc(value: Any) -> str:
        return html.escape(str(value), quote=True)

    def check_html(check: Check) -> str:
        required = "required" if check.required else "optional"
        return (
            '<article class="check ' + esc(check.status.lower()) + '">'
            '<div class="check-head"><span class="badge">' + esc(check.status) + '</span>'
            '<strong>' + esc(check.name) + '</strong><span class="scope">' + required + '</span></div>'
            '<pre>' + esc(check.evidence) + '</pre></article>'
        )

    sections = []
    ordered = [
        "Architecture Validation",
        "Verifier Safety",
        "API Validation",
        "UI Validation",
        "Scenario Validation",
        "Regression Tests",
    ]
    for title in ordered:
        if title in grouped:
            sections.append('<section><h2>' + esc(title) + '</h2>' + "".join(check_html(item) for item in grouped[title]) + "</section>")

    blockers = report.blockers
    warnings = report.warnings
    limitation_items = list(report.known_limitations)
    if not limitation_items:
        limitation_items.append("No optional limitations recorded by the verifier.")
    limitation_html = "".join("<li>" + esc(item) + "</li>" for item in limitation_items)
    blocker_html = "".join("<li><b>" + esc(item.name) + "</b>: " + esc(item.evidence) + "</li>" for item in blockers) or "<li>None</li>"
    warning_html = "".join("<li><b>" + esc(item.name) + "</b>: " + esc(item.evidence) + "</li>" for item in warnings) or "<li>None</li>"
    screenshot_html = ""
    if report.screenshot_data_uri:
        screenshot_html = '<figure><figcaption>Embedded browser screenshot</figcaption><img class="screenshot" src="' + esc(report.screenshot_data_uri) + '" alt="Crawler Console browser screenshot"></figure>'

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crawler Console Acceptance Report</title>
<style>
:root{color-scheme:light dark;--bg:#f5f7fb;--panel:#fff;--text:#172033;--muted:#667085;--line:#d9dfeb;--pass:#087443;--fail:#b42318;--warn:#b54708;--blue:#1d4ed8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1180px;margin:0 auto;padding:32px 20px 64px}h1{margin:.2em 0;font-size:2rem}h2{margin:30px 0 12px;font-size:1.25rem}h3{margin:20px 0 8px}.hero,.summary,.check,section>figure{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 2px 8px #1018280b}.hero{padding:22px}.hero p{margin:.45em 0;color:var(--muted)}.summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:1px;margin-top:16px;overflow:hidden}.metric{padding:15px;background:var(--panel)}.metric strong{display:block;font-size:1.5rem}.metric span{color:var(--muted);font-size:.85rem}.metric.status strong{color:var(--pass)}.metric.status.fail strong{color:var(--fail)}.check{padding:14px 16px;margin:10px 0}.check-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.badge{font-size:.75rem;font-weight:700;border-radius:999px;padding:2px 8px;background:#e5e7eb;color:#344054}.pass .badge{background:#dcfae6;color:var(--pass)}.fail .badge,.blocked .badge{background:#fee4e2;color:var(--fail)}.warning .badge,.unavailable .badge{background:#fef0c7;color:var(--warn)}.scope{margin-left:auto;color:var(--muted);font-size:.8rem}.check pre{margin:10px 0 0;padding:10px;background:#0f172a;color:#e2e8f0;white-space:pre-wrap;overflow:auto;border-radius:8px;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}ul{padding-left:24px}.meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px 24px;margin-top:14px}.meta div{padding:8px 0;border-bottom:1px solid var(--line)}.meta b{display:inline-block;min-width:145px}.screenshot{display:block;max-width:100%;height:auto;border-radius:8px}figure{padding:14px;margin:12px 0}figcaption{font-weight:600;margin-bottom:10px}@media(max-width:720px){.summary{grid-template-columns:repeat(2,minmax(0,1fr))}.meta{grid-template-columns:1fr}.scope{margin-left:0}}
@media(prefers-color-scheme:dark){:root{--bg:#0b1220;--panel:#111827;--text:#e5e7eb;--muted:#98a2b3;--line:#263247}.metric{background:var(--panel)}}
</style></head><body><main>
<div class="hero"><div class="metric status __STATUS_CLASS__"><span>Acceptance</span><strong>__STATUS__</strong></div><h1>Crawler Console Acceptance Report</h1><p>Generated by <code>python3 scripts/verify_crawler_console.py</code>.</p>
<div class="meta"><div><b>Timestamp</b> __TIMESTAMP__</div><div><b>Python</b> __PYTHON__</div><div><b>Git commit</b> __COMMIT__</div><div><b>DB fixture</b> temporary file SQLite</div></div></div>
<div class="summary"><div class="metric"><span>Blocker Count</span><strong>__BLOCKERS__</strong></div><div class="metric"><span>Test Count</span><strong>__TEST_COUNT__</strong></div><div class="metric"><span>Passed</span><strong>__PASSED__</strong></div><div class="metric"><span>Failed</span><strong>__FAILED__</strong></div><div class="metric"><span>Regression exit</span><strong>__EXIT__</strong></div></div>
__SECTIONS__
<section><h2>Blockers</h2><ul>__BLOCKER_LIST__</ul><h3>Warnings</h3><ul>__WARNING_LIST__</ul></section>
<section><h2>Known Limitations</h2><ul>__LIMITATION_LIST__</ul>__SCREENSHOT__</section>
</main></body></html>""".replace("__STATUS_CLASS__", status_class).replace("__STATUS__", esc(report.status)).replace("__TIMESTAMP__", esc(report.environment.get("timestamp", "unavailable"))).replace("__PYTHON__", esc(report.environment.get("python", "unavailable"))).replace("__COMMIT__", esc(report.environment.get("git_commit", "unavailable"))).replace("__BLOCKERS__", str(len(blockers))).replace("__TEST_COUNT__", str(regression.get("test_count", "unavailable"))).replace("__PASSED__", str(regression.get("passed", "unavailable"))).replace("__FAILED__", str(regression.get("failed", "unavailable"))).replace("__EXIT__", str(regression.get("exit_code", "unavailable"))).replace("__SECTIONS__", "".join(sections)).replace("__BLOCKER_LIST__", blocker_html).replace("__WARNING_LIST__", warning_html).replace("__LIMITATION_LIST__", limitation_html).replace("__SCREENSHOT__", screenshot_html)


def _guarded(
    report: Report,
    section: str,
    name: str,
    callback: Any,
    *,
    required: bool = True,
) -> Any:
    """Run one verifier phase while preserving a readable report on failure."""

    try:
        return callback()
    except Exception as error:
        report.add(
            section,
            name,
            "BLOCKED",
            traceback.format_exc(limit=8),
            required=required,
        )
        report.known_limitations.append(
            f"{name} raised {type(error).__name__}: {error}"
        )
        return None


def main() -> int:
    report = Report()
    report.environment = {
        "timestamp": _now(),
        "python": platform.python_version() + " (" + sys.executable + ")",
        "git_commit": _git_commit(),
        "report_path": str(REPORT_PATH),
    }
    report.known_limitations.extend(
        [
            "The scenarios use a deterministic temporary-file fixture; no production data or real website is crawled.",
            "This P0 verifier does not exercise start/stop controls, WebSockets, or anomaly detection.",
        ]
    )
    app: Any = None
    storage: Any = None
    fixture_directory: Any = None
    fixture_path: Optional[Path] = None
    scenarios: Dict[str, Any] = {}
    try:
        _guarded(
            report,
            "Regression Tests",
            "Pytest summary parser self-check completed",
            lambda: _pytest_summary_parser_self_check(report),
        )
        _guarded(report, "Regression Tests", "Regression command completed", lambda: _run_regression(report))
        storage_module = _guarded(
            report,
            "Scenario Validation",
            "Acceptance storage module imported",
            lambda: __import__("zoofan.storage", fromlist=["SQLiteStorage"]),
        )
        if storage_module is not None:
            fixture_directory = tempfile.TemporaryDirectory(
                prefix="zoofancrawler-console-acceptance-"
            )
            fixture_path = Path(fixture_directory.name) / "console-acceptance.db"
            storage = _guarded(
                report,
                "Scenario Validation",
                "Acceptance fixture database initialized",
                lambda: storage_module.SQLiteStorage(fixture_path),
            )
        if storage is not None:
            assert fixture_path is not None
            scenarios = _guarded(
                report,
                "Scenario Validation",
                "Deterministic crawler scenarios executed",
                lambda: _run_scenarios(storage),
            ) or {}
            _guarded(
                report,
                "Scenario Validation",
                "Database scenario evidence collected",
                lambda: _scenario_checks(report, storage, scenarios),
            )
            storage.connection.commit()
            api_result = _guarded(
                report,
                "API Validation",
                "API scenario checks completed",
                lambda: _api_checks(report, fixture_path, scenarios),
            )
            if isinstance(api_result, tuple) and len(api_result) == 2:
                app, client = api_result
                _guarded(
                    report,
                    "UI Validation",
                    "UI route checks completed",
                    lambda: _ui_checks(report, client, scenarios),
                )
            else:
                report.add(
                    "UI Validation",
                    "UI route checks completed",
                    "BLOCKED",
                    {"error": "API fixture was not available"},
                )
        else:
            report.add(
                "Scenario Validation",
                "Required deterministic scenarios",
                "BLOCKED",
                {"error": "acceptance fixture database was not initialized"},
            )
        _guarded(
            report,
            "Architecture Validation",
            "Architecture checks completed",
            lambda: _architecture_checks(report, storage_fixture=storage),
        )
        if app is not None and storage is not None and scenarios:
            _guarded(
                report,
                "UI Validation",
                "Browser visual check completed",
                lambda: _browser_check(report, app, scenarios),
                required=True,
            )
        else:
            report.known_limitations.append(
                "Browser visual check unavailable: application fixture prerequisites were not available."
            )
            report.add(
                "UI Validation",
                "Browser visual check",
                "UNAVAILABLE",
                "application fixture prerequisites were not available",
                required=False,
            )
    finally:
        try:
            if storage is not None:
                storage.close()
        except Exception:
            pass
        try:
            if fixture_directory is not None:
                fixture_directory.cleanup()
            cleanup_ok = fixture_path is not None and not fixture_path.exists()
            report.add(
                "Verifier Safety",
                "Temporary SQLite fixture is removed after server shutdown",
                "PASS" if cleanup_ok else "FAIL",
                {
                    "temporary_fixture": fixture_path.name if fixture_path else None,
                    "fixture_removed": cleanup_ok,
                    "production_database_used": False,
                },
            )
        except Exception:
            report.add(
                "Verifier Safety",
                "Temporary SQLite fixture is removed after server shutdown",
                "FAIL",
                traceback.format_exc(limit=3),
            )
        try:
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(_render_report(report), encoding="utf-8")
        except Exception as error:
            # There is no safe alternate report location that fulfils the
            # contract; make the failure visible on stderr and return non-zero.
            print("Unable to write acceptance report:", error, file=sys.stderr)
            return 1
    print("Crawler Console Acceptance Report:", REPORT_PATH)
    print("Status:", report.status, "Blockers:", len(report.blockers))
    if report.regression:
        print(
            "Regression:",
            report.regression.get("command"),
            "exit=", report.regression.get("exit_code"),
            "tests=", report.regression.get("test_count"),
            "passed=", report.regression.get("passed"),
            "failed=", report.regression.get("failed"),
            "duration_s=", report.regression.get("duration_seconds"),
        )
    return 0 if report.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
