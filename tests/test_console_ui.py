"""Smoke checks for the server-rendered Operations Console shell."""

from __future__ import annotations

import json
import threading
import time
from contextlib import suppress
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask, jsonify
from werkzeug.serving import make_server

from zoofan.dashboard import create_app


def test_console_pages_render_loading_shells_and_safe_navigation():
    client = create_app(":memory:").test_client()
    for path in (
        "/console",
        "/console/overview",
        "/console/runs",
        "/console/runs/run-1",
        "/console/zoos/zoo-1",
        "/console/articles",
        "/console/events",
    ):
        response = client.get(path)
        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert "Loading console data" in body
        assert "data-console-page" in body
        assert "raw_html" not in body


def test_console_ui_declares_polling_and_incremental_event_contract():
    client = create_app(":memory:").test_client()
    body = client.get("/console/runs/run-1").get_data(as_text=True)
    assert "setInterval" in body
    assert "2500" in body
    assert "pollRunSummary(root, generation)" in body
    assert "after_id" in body
    assert "No events recorded for this run" in body
    assert "noopener noreferrer" in body


def test_events_page_polling_stops_when_run_becomes_terminal_in_browser():
    """Exercise timer lifecycle, incremental cursor, and dedupe in a real DOM."""

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = create_app(":memory:").test_client().get("/console/events?run_id=run-1").get_data(as_text=True)
    run_html = create_app(":memory:").test_client().get("/console/runs/run-1").get_data(as_text=True)
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise

            event_queries = []
            run_requests = []
            page = browser.new_page()

            def route_request(route):
                parsed = urlsplit(route.request.url)
                if parsed.path == "/console/events":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif parsed.path == "/api/crawl-runs/run-1/events":
                    event_queries.append(parse_qs(parsed.query))
                    events = [{"id": 1, "message": "first"}]
                    if len(event_queries) > 2:
                        # Repeating id=1 proves the append path still deduplicates.
                        events.append({"id": 2, "message": "second"})
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"events": events, "next_after_id": 0 if len(event_queries) == 2 else len(event_queries)}),
                    )
                elif parsed.path == "/api/crawl-runs/run-1":
                    run_requests.append(parsed.path)
                    status = "running" if len(run_requests) < 3 else "success"
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"run": {"id": "run-1", "classified_status": status}}),
                    )
                else:
                    route.fulfill(status=200, body="")

            page.route("**/*", route_request)
            page.goto("http://console.test/console/events?run_id=run-1", wait_until="load")
            page.wait_for_function(
                "document.querySelectorAll('[data-console-events] tbody tr').length === 2",
                timeout=11_000,
            )
            page.wait_for_timeout(100)
            assert len(event_queries) == 3
            assert len(run_requests) == 3
            assert event_queries[0].get("after_id") is None
            assert event_queries[1]["after_id"] == ["1"]
            assert event_queries[2]["after_id"] == ["1"]
            assert page.locator("[data-console-events] tbody tr").count() == 2

            # More than one complete interval proves the terminal observation
            # cleared the existing timer instead of merely delaying another tick.
            page.wait_for_timeout(3_000)
            assert len(event_queries) == 3
            assert len(run_requests) == 3

            terminal_event_requests = []
            terminal_run_requests = []
            terminal_page = browser.new_page()

            def route_terminal_request(route):
                parsed = urlsplit(route.request.url)
                if parsed.path == "/console/events":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif parsed.path == "/api/crawl-runs/run-1/events":
                    terminal_event_requests.append(parsed.query)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"events": [], "next_after_id": None}),
                    )
                elif parsed.path == "/api/crawl-runs/run-1":
                    terminal_run_requests.append(parsed.path)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"run": {"id": "run-1", "classified_status": "success"}}),
                    )
                else:
                    route.fulfill(status=200, body="")

            terminal_page.route("**/*", route_terminal_request)
            terminal_page.goto("http://console.test/console/events?run_id=run-1", wait_until="load")
            terminal_page.wait_for_function("document.querySelector('[data-console-page]').dataset.state === 'ready'")
            terminal_page.wait_for_timeout(3_000)
            assert len(terminal_event_requests) == 1
            assert len(terminal_run_requests) == 1

            leaving_event_requests = []
            leaving_run_requests = []
            leaving_page = browser.new_page()

            def route_leaving_request(route):
                parsed = urlsplit(route.request.url)
                if parsed.path == "/console/events":
                    route.fulfill(status=200, content_type="text/html", body=html)
                elif parsed.path == "/api/crawl-runs/run-1/events":
                    leaving_event_requests.append(parse_qs(parsed.query))
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"events": [{"id": 1, "message": "persisted"}], "next_after_id": 1}),
                    )
                elif parsed.path == "/api/crawl-runs/run-1":
                    leaving_run_requests.append(parsed.path)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"run": {"id": "run-1", "classified_status": "running"}}),
                    )
                else:
                    route.fulfill(status=200, body="")

            leaving_page.route("**/*", route_leaving_request)
            leaving_page.goto("http://console.test/console/events?run_id=run-1", wait_until="load")
            leaving_page.wait_for_function("document.querySelector('[data-console-page]').dataset.state === 'ready'")
            leaving_page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
            leaving_page.wait_for_timeout(3_000)
            assert len(leaving_event_requests) == 1
            assert len(leaving_run_requests) == 1
            leaving_page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            leaving_page.wait_for_function("document.querySelector('[data-console-page]').dataset.state === 'ready'")
            leaving_page.wait_for_timeout(100)
            assert len(leaving_event_requests) == 2
            assert len(leaving_run_requests) == 2
            assert leaving_event_requests[1]["after_id"] == ["1"]
            leaving_page.wait_for_timeout(3_000)
            assert len(leaving_event_requests) == 3
            assert len(leaving_run_requests) == 3
            assert leaving_event_requests[2]["after_id"] == ["1"]
            leaving_page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            leaving_page.wait_for_timeout(3_000)
            assert len(leaving_event_requests) == 3
            assert len(leaving_run_requests) == 3

            detail_run_requests = []
            detail_event_requests = []
            detail_page = browser.new_page()

            def route_run_detail_request(route):
                parsed = urlsplit(route.request.url)
                if parsed.path == "/console/runs/run-1":
                    route.fulfill(status=200, content_type="text/html", body=run_html)
                elif parsed.path == "/api/crawl-runs/run-1/events":
                    detail_event_requests.append(parse_qs(parsed.query))
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"events": [{"id": 1, "message": "detail"}], "next_after_id": 1}),
                    )
                elif parsed.path == "/api/crawl-runs/run-1":
                    detail_run_requests.append(parsed.path)
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"run": {"id": "run-1", "classified_status": "running"}}),
                    )
                elif parsed.path.endswith("/zoos"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"zoos": []}))
                elif parsed.path.endswith("/sources"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"sources": []}))
                elif parsed.path.endswith("/articles"):
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"articles": []}))
                else:
                    route.fulfill(status=200, body="")

            detail_page.route("**/*", route_run_detail_request)
            detail_page.goto("http://console.test/console/runs/run-1", wait_until="load")
            detail_page.wait_for_function("document.querySelector('[data-console-page]').dataset.state === 'ready'")
            detail_page.wait_for_timeout(100)
            assert len(detail_run_requests) == 1
            assert len(detail_event_requests) == 1
            detail_page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
            detail_page.wait_for_timeout(3_000)
            assert len(detail_run_requests) == 1
            assert len(detail_event_requests) == 1
            detail_page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            detail_page.wait_for_timeout(100)
            assert len(detail_run_requests) == 2
            assert len(detail_event_requests) == 1
            detail_page.wait_for_timeout(3_000)
            assert len(detail_run_requests) == 3
            assert len(detail_event_requests) == 2
            assert detail_event_requests[1]["after_id"] == ["1"]
            detail_page.wait_for_timeout(3_000)
            assert len(detail_run_requests) == 4
            assert len(detail_event_requests) == 3
    finally:
        if browser is not None:
            with suppress(Exception):
                browser.close()


def test_events_page_slow_poll_never_overlaps_requests():
    """Hold an events response across a tick and measure server concurrency."""

    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = create_app(":memory:").test_client().get("/console/events?run_id=run-1").get_data(as_text=True)
    app = Flask(__name__)
    lock = threading.Lock()
    slow_started = threading.Event()
    release_slow_response = threading.Event()
    event_calls = 0
    events_in_flight = 0
    max_events_in_flight = 0
    run_calls = 0

    @app.get("/console/events")
    def events_page():
        return html

    @app.get("/api/crawl-runs/run-1/events")
    def events_api():
        nonlocal event_calls, events_in_flight, max_events_in_flight
        with lock:
            event_calls += 1
            call_number = event_calls
            events_in_flight += 1
            max_events_in_flight = max(max_events_in_flight, events_in_flight)
        try:
            if call_number == 2:
                slow_started.set()
                release_slow_response.wait(timeout=6)
            events = [{"id": 1, "message": "first"}]
            if call_number > 1:
                events.append({"id": 2, "message": "second"})
            return jsonify({"events": events, "next_after_id": call_number})
        finally:
            with lock:
                events_in_flight -= 1

    @app.get("/api/crawl-runs/run-1")
    def run_api():
        nonlocal run_calls
        with lock:
            run_calls += 1
            status = "running" if run_calls == 1 else "success"
        return jsonify({"run": {"id": "run-1", "classified_status": status}})

    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/console/events?run_id=run-1", wait_until="load")
            assert slow_started.wait(timeout=5)

            # The next legal interval fires while request 2 is deliberately
            # blocked. Without the in-flight guard, the threaded server would
            # observe request 3 concurrently here.
            time.sleep(2.7)
            with lock:
                assert event_calls == 2
                assert events_in_flight == 1
                assert max_events_in_flight == 1

            release_slow_response.set()
            page.wait_for_function(
                "document.querySelectorAll('[data-console-events] tbody tr').length === 2",
                timeout=4_000,
            )
            page.wait_for_timeout(3_000)
            with lock:
                assert event_calls == 2
                assert max_events_in_flight == 1
                assert run_calls == 2
    finally:
        release_slow_response.set()
        if browser is not None:
            with suppress(Exception):
                browser.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)


def test_run_detail_slow_summary_and_events_polls_do_not_overlap():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = create_app(":memory:").test_client().get("/console/runs/run-1").get_data(as_text=True)
    app = Flask(__name__)
    lock = threading.Lock()
    slow_run_started = threading.Event()
    slow_events_started = threading.Event()
    release_slow_responses = threading.Event()
    counts = {"run": 0, "events": 0, "run_in_flight": 0, "events_in_flight": 0, "run_max": 0, "events_max": 0}

    @app.get("/console/runs/run-1")
    def run_detail_page():
        return html

    @app.get("/api/crawl-runs/run-1")
    def detail_run_api():
        with lock:
            counts["run"] += 1
            call_number = counts["run"]
            counts["run_in_flight"] += 1
            counts["run_max"] = max(counts["run_max"], counts["run_in_flight"])
        try:
            if call_number == 2:
                slow_run_started.set()
                release_slow_responses.wait(timeout=7)
            status = "running" if call_number == 1 else "success"
            return jsonify({"run": {"id": "run-1", "classified_status": status}})
        finally:
            with lock:
                counts["run_in_flight"] -= 1

    @app.get("/api/crawl-runs/run-1/events")
    def detail_events_api():
        with lock:
            counts["events"] += 1
            call_number = counts["events"]
            counts["events_in_flight"] += 1
            counts["events_max"] = max(counts["events_max"], counts["events_in_flight"])
        try:
            if call_number == 2:
                slow_events_started.set()
                release_slow_responses.wait(timeout=7)
            return jsonify({"events": [{"id": call_number, "message": "detail"}], "next_after_id": call_number})
        finally:
            with lock:
                counts["events_in_flight"] -= 1

    @app.get("/api/crawl-runs/run-1/<section>")
    def detail_collection_api(section):
        return jsonify({section: []})

    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/console/runs/run-1", wait_until="load")
            assert slow_run_started.wait(timeout=5)
            assert slow_events_started.wait(timeout=2)
            time.sleep(2.7)
            with lock:
                assert counts == {
                    "run": 2,
                    "events": 2,
                    "run_in_flight": 1,
                    "events_in_flight": 1,
                    "run_max": 1,
                    "events_max": 1,
                }

            release_slow_responses.set()
            page.wait_for_function("document.querySelector('[data-console-run-status] [data-status=success]')")
            page.wait_for_timeout(3_000)
            with lock:
                assert counts["run"] == 2
                assert counts["events"] == 2
                assert counts["run_max"] == 1
                assert counts["events_max"] == 1
    finally:
        release_slow_responses.set()
        if browser is not None:
            with suppress(Exception):
                browser.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)


def test_run_detail_ignores_pre_bfcache_running_response_after_terminal():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = create_app(":memory:").test_client().get("/console/runs/run-1").get_data(as_text=True)
    app = Flask(__name__)
    lock = threading.Lock()
    old_running_started = threading.Event()
    release_old_running = threading.Event()
    run_calls = 0
    event_calls = 0

    @app.get("/console/runs/run-1")
    def race_run_detail_page():
        return html

    @app.get("/api/crawl-runs/run-1")
    def race_run_api():
        nonlocal run_calls
        with lock:
            run_calls += 1
            call_number = run_calls
        if call_number == 1:
            old_running_started.set()
            release_old_running.wait(timeout=7)
            status = "running"
        else:
            status = "success"
        return jsonify({"run": {"id": "run-1", "classified_status": status}})

    @app.get("/api/crawl-runs/run-1/events")
    def race_events_api():
        nonlocal event_calls
        with lock:
            event_calls += 1
        return jsonify({"events": [{"id": 1, "message": "terminal"}], "next_after_id": 1})

    @app.get("/api/crawl-runs/run-1/<section>")
    def race_collection_api(section):
        return jsonify({section: []})

    server = make_server("127.0.0.1", 0, app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    browser = None
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/console/runs/run-1", wait_until="load")
            assert old_running_started.wait(timeout=3)
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}))")
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
            page.wait_for_function("document.querySelector('[data-console-run-status] [data-status=success]')")
            with lock:
                assert run_calls == 2
                assert event_calls == 1

            release_old_running.set()
            page.wait_for_timeout(3_000)
            with lock:
                assert run_calls == 2
                assert event_calls == 1
            assert page.locator("[data-console-run-status] [data-status=success]").count() == 1
    finally:
        release_old_running.set()
        if browser is not None:
            with suppress(Exception):
                browser.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)


def test_console_overview_formats_totals_and_surfaces_current_activity_dto():
    client = create_app(":memory:").test_client()
    body = client.get("/console").get_data(as_text=True)

    assert "completedTotal" in body
    assert "zoos_completed" in body
    assert "zoos_total" in body
    assert "sources_completed" in body
    assert "sources_total" in body
    assert "current_activity" in body
    assert "console-current-badge" in body
    assert "JSON.stringify(item.progress)" in body
    assert "textContent" in body


def test_zoo_detail_renders_fixture_data_and_tolerates_missing_optional_title_in_browser():
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    html = create_app(":memory:").test_client().get("/console/zoos/zoo-berlin").get_data(as_text=True)
    title_markup = '<span data-console-zoo-title>Zoo zoo-berlin</span>'
    assert title_markup in html
    html_without_title = html.replace(title_markup, "")
    zoo_body = {
        "zoo": {
            "id": "zoo-berlin",
            "name": "Berlin Zoological Garden",
            "country_code": "DE",
            "website_url": "https://official.example/news",
            "enabled": True,
            "raw_html": "<script>window.__unsafe_zoo_fixture = true</script>",
            "sources": [
                {
                    "id": "news",
                    "name": "Official news",
                    "kind": "archive",
                    "enabled": True,
                    "status": "success",
                    "last_http_status": 200,
                }
            ],
        }
    }
    history_body = {
        "history": [
            {
                "started_at": "2026-08-16T10:00:00Z",
                "classified_status": "success",
                "stored": 7,
                "duplicates": 2,
                "errors": 0,
                "raw_html": "<b>unsafe history fixture</b>",
            }
        ]
    }
    browser = None
    page_errors = []
    console_errors = []
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                if any(token in str(exc).lower() for token in ("executable", "browser", "chromium")):
                    pytest.skip(f"Chromium is unavailable: {exc}")
                raise

            def exercise_page(document_path, document_body, expect_title):
                page = browser.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

                def route_request(route):
                    parsed = urlsplit(route.request.url)
                    if parsed.path == document_path:
                        route.fulfill(status=200, content_type="text/html", body=document_body)
                    elif parsed.path == "/api/zoos/zoo-berlin":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(zoo_body))
                    elif parsed.path == "/api/zoos/zoo-berlin/crawl-history":
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(history_body))
                    else:
                        route.fulfill(status=200, body="")

                page.route("**/*", route_request)
                page.goto("http://console.test" + document_path, wait_until="load")
                page.wait_for_function("document.querySelector('[data-console-page]').dataset.state === 'ready'")
                assert page.locator(".console-region-error, button", has_text="Retry").count() == 0
                assert "DE" in page.locator("[data-console-zoo-info]").inner_text()
                assert "Official news" in page.locator("[data-console-zoo-info]").inner_text()
                assert "7" in page.locator("[data-console-zoo-history]").inner_text()
                assert page.locator("[data-console-zoo-info] a[href='https://official.example/news']").get_attribute("rel") == "noopener noreferrer"
                assert "unsafe_zoo_fixture" not in page.content()
                assert "unsafe history fixture" not in page.content()
                if expect_title:
                    assert page.locator("[data-console-zoo-title]").inner_text() == "Berlin Zoological Garden"
                else:
                    assert page.locator("[data-console-zoo-title]").count() == 0
                page.close()

            exercise_page("/console/zoos/zoo-berlin", html, True)
            exercise_page("/console/zoos/zoo-berlin-no-title", html_without_title, False)
    finally:
        if browser is not None:
            with suppress(Exception):
                browser.close()
    assert page_errors == []
    assert console_errors == []


def test_legacy_dashboard_routes_remain_available():
    client = create_app(":memory:").test_client()
    for path in ("/", "/articles", "/sources", "/runs"):
        response = client.get(path)
        assert response.status_code == 200


def test_console_classified_status_contract():
    body = create_app(":memory:").test_client().get("/console").get_data(as_text=True)
    for key, label in (("success", "Success"), ("warning", "Warning"), ("failed", "Failed"), ("budget_hit", "Budget Hit"), ("running", "Running"), ("stale", "Stale"), ("unknown", "Unknown")):
        assert f'{key}: "{label}"' in body
    for field in ("classified_status", "display_status", "status_key", "status", "status_label"):
        assert field in body
    assert "statusTag(run)" in body
    assert "statusTag(current)" in body
    assert "statusTag(item)" in body
    assert "statusTag(run.status" not in body
    assert "source.status || \"unknown\"" in body
    assert "source.terminal_status" in body
    assert "source.terminal === true" in body
    assert "source.finished_at" in body
    assert "source.status_observable === false" in body
    assert "source.is_active !== false" in body
    assert "source.raw_status" not in body
    assert "source.run_status" not in body
