from __future__ import annotations

import json
import sqlite3

from zoofan.endgoal import (
    _atomic_write_json,
    _close_playwright_handles,
    _dedup_report,
    _human_banner,
    navigation_false_positive,
)


def test_navigation_gate_rejects_navigation_and_honors_explicit_allow():
    source = {"url": "https://zoo.example/news", "config": {}}
    assert navigation_false_positive("https://zoo.example/", source)
    assert navigation_false_positive("https://zoo.example/tickets", source)
    assert navigation_false_positive("https://zoo.example/events", source)
    allowed = {"url": source["url"], "config": {"allow_regex": r"/events/"}}
    assert navigation_false_positive("https://zoo.example/events/story", allowed) is None


def test_atomic_report_write_and_dedup_report(tmp_path):
    destination = tmp_path / "nested" / "report.json"
    _atomic_write_json(destination, {"status": "PASS", "value": 1})
    assert json.loads(destination.read_text()) == {"status": "PASS", "value": 1}

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE articles (canonical_url TEXT, normalized_url TEXT)")
    connection.executemany(
        "INSERT INTO articles VALUES (?, ?)",
        [("https://zoo.example/a", "https://zoo.example/a"), ("https://zoo.example/b", "https://zoo.example/b")],
    )
    report = _dedup_report(
        connection,
        before_total=0,
        after_run1_total=2,
        after_run2_total=2,
        run1_canonicals={"https://zoo.example/a", "https://zoo.example/b"},
        run2_canonicals={"https://zoo.example/a", "https://zoo.example/b"},
    )
    assert report["status"] == "PASS"
    assert report["run1_new"] == 2
    assert report["run2_new"] == 0
    connection.close()


def test_playwright_handles_close_in_order_before_driver_exit():
    closed = []

    class Handle:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    _close_playwright_handles(Handle("page"), Handle("context"), Handle("browser"))
    assert closed == ["page", "context", "browser"]


def test_human_banner_is_exact_acceptance_string():
    assert _human_banner("PASS") == "ZOOFAN CRAWLER PROTOTYPE: PASS"
    assert _human_banner("FAIL") == "ZOOFAN CRAWLER PROTOTYPE: FAIL"
