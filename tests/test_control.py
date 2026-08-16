from __future__ import annotations

from dataclasses import dataclass

from zoofan.control import create_app, main


def _config_file(tmp_path):
    path = tmp_path / "zoos.yaml"
    path.write_text(
        "zoos:\n"
        "  - slug: enabled-zoo\n"
        "    name: Enabled Zoo\n"
        "    website_url: https://enabled.example\n"
        "    country_code: DE\n"
        "    language: de\n"
        "    enabled: true\n"
        "  - slug: disabled-zoo\n"
        "    name: Disabled Zoo\n"
        "    website_url: https://disabled.example\n"
        "    country_code: DE\n"
        "    language: de\n"
        "    enabled: false\n",
        encoding="utf-8",
    )
    return path


def _source_config_file(tmp_path):
    path = tmp_path / "zoos-with-sources.yaml"
    path.write_text(
        "zoos:\n"
        "  - slug: enabled-zoo\n"
        "    name: Enabled Zoo\n"
        "    website_url: https://enabled.example\n"
        "    country_code: DE\n"
        "    language: de\n"
        "    enabled: true\n"
        "    sources:\n"
        "      - id: enabled-source\n"
        "        name: Enabled Feed\n"
        "        kind: rss\n"
        "        url: https://enabled.example/feed.xml\n"
        "        language: de\n"
        "        enabled: true\n"
        "      - id: disabled-source\n"
        "        name: Disabled Feed\n"
        "        kind: rss\n"
        "        url: https://enabled.example/disabled.xml\n"
        "        language: de\n"
        "        enabled: false\n"
        "  - slug: disabled-zoo\n"
        "    name: Disabled Zoo\n"
        "    website_url: https://disabled.example\n"
        "    country_code: DE\n"
        "    language: de\n"
        "    enabled: false\n"
        "    sources:\n"
        "      - id: disabled-zoo-source\n"
        "        kind: rss\n"
        "        url: https://disabled.example/feed.xml\n"
        "        language: de\n"
        "        enabled: true\n",
        encoding="utf-8",
    )
    return path


class ImmediateThread:
    def __init__(self, *, target):
        self.target = target
        self.daemon = False

    def start(self):
        self.target()


class WaitingThread:
    def __init__(self, *, target):
        self.target = target
        self.daemon = False

    def start(self):
        pass


class Storage:
    instances = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.instances.append(self)

    def close(self):
        self.closed = True


@dataclass
class Result:
    status: str = "completed"
    discovered_count: int = 4
    fetched_count: int = 3
    stored_count: int = 2
    error_count: int = 0
    run: object = None


class SuccessfulCrawler:
    selections = []

    def __init__(self, config, storage):
        self.storage = storage

    def crawl(self, selection):
        self.selections.append(selection)
        return Result(run=type("Run", (), {"id": "run_123"})())


def _token(client):
    page = client.get("/").get_data(as_text=True)
    return page.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def test_control_only_allows_enabled_yaml_zoos_and_returns_safe_summary(tmp_path):
    Storage.instances.clear()
    SuccessfulCrawler.selections.clear()
    app = create_app(
        tmp_path / "crawl.sqlite", _config_file(tmp_path), crawler_factory=SuccessfulCrawler,
        storage_factory=Storage, thread_factory=ImmediateThread, clock=lambda: "now",
    )
    client = app.test_client()

    response = client.post("/crawl", data={"csrf_token": _token(client), "selection": "enabled-zoo"})

    assert response.status_code == 303
    assert SuccessfulCrawler.selections == ["enabled-zoo"]
    assert Storage.instances[0].closed is True
    payload = client.get("/status").get_json()
    assert payload == {
        "status": "completed", "selection": "enabled-zoo", "run_id": "run_123", "started_at": "now", "finished_at": "now",
        "summary": {"discovered": 4, "fetched": 3, "stored": 2, "errors": 0}, "message": "Crawl completed.",
    }
    rejected = client.post("/crawl", data={"csrf_token": _token(client), "selection": "disabled-zoo"})
    assert rejected.status_code == 400


def test_control_requires_process_csrf_and_rejects_a_second_running_task(tmp_path):
    app = create_app(
        tmp_path / "crawl.sqlite", _config_file(tmp_path), crawler_factory=SuccessfulCrawler,
        storage_factory=Storage, thread_factory=WaitingThread,
    )
    client = app.test_client()

    assert client.post("/crawl", data={"selection": "all"}).status_code == 400
    token = _token(client)
    assert client.post("/crawl", data={"csrf_token": token, "selection": "all"}).status_code == 303
    response = client.post("/crawl", data={"csrf_token": token, "selection": "all"})
    assert response.status_code == 409
    assert client.get("/status").get_json()["status"] == "running"


def test_control_maps_exception_to_safe_failure(tmp_path):
    class FailingCrawler:
        def __init__(self, config, storage):
            pass

        def crawl(self, selection):
            raise RuntimeError("secret path /private and remote response")

    app = create_app(
        tmp_path / "crawl.sqlite", _config_file(tmp_path), crawler_factory=FailingCrawler,
        storage_factory=Storage, thread_factory=ImmediateThread,
    )
    client = app.test_client()
    assert client.post("/crawl", data={"csrf_token": _token(client), "selection": "all"}).status_code == 303
    payload = client.get("/status").get_json()
    assert payload["status"] == "failed"
    assert payload["message"] == "Crawl failed."
    assert "private" not in client.get("/status").get_data(as_text=True)
    assert client.post("/crawl", data={"csrf_token": _token(client), "selection": "all"}).status_code == 303


def test_main_always_binds_loopback(monkeypatch):
    calls = []

    class App:
        def run(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr("zoofan.control.create_app", lambda *args, **kwargs: App())
    main("crawl.sqlite", "zoos.yaml", "127.0.0.1", 8123, "http://127.0.0.1:8000/")
    assert calls == [{"host": "127.0.0.1", "port": 8123}]


def test_main_rejects_non_loopback_host():
    try:
        main("crawl.sqlite", "zoos.yaml", "0.0.0.0", 8123)
    except ValueError as error:
        assert "127.0.0.1" in str(error)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("main accepted a public host")


def test_control_does_not_render_an_unsafe_dashboard_link_and_declares_polling_contract(tmp_path):
    app = create_app(
        tmp_path / "crawl.sqlite", _config_file(tmp_path), dashboard_url="javascript:alert(1)",
        crawler_factory=SuccessfulCrawler, storage_factory=Storage, thread_factory=WaitingThread,
    )
    page = app.test_client().get("/").get_data(as_text=True)
    assert "Open dashboard" not in page
    assert 'data-current-status="idle"' in page
    assert "state.status !== panel.getAttribute('data-current-status')" in page


def test_control_ignores_malformed_ipv6_dashboard_url(tmp_path):
    app = create_app(
        tmp_path / "crawl.sqlite", _config_file(tmp_path), dashboard_url="http://[::1",
        crawler_factory=SuccessfulCrawler, storage_factory=Storage, thread_factory=WaitingThread,
    )
    page = app.test_client().get("/").get_data(as_text=True)
    assert "Open dashboard" not in page


class NextBatchCrawler:
    calls = []

    def __init__(self, config, storage):
        self.storage = storage

    def next_batch(self, source_id, *, limit):
        self.calls.append((source_id, limit))
        return type(
            "NextResult",
            (),
            {
                "status": "completed",
                "processed_count": 3,
                "stored_count": 2,
                "already_known_count": 1,
                "duplicate_candidate_count": 4,
                "error_count": 0,
                "remaining_estimate": 7,
                "run": type("Run", (), {"id": "batch_run"})(),
            },
        )()


def test_control_source_query_preselects_only_enabled_source_without_starting(tmp_path):
    NextBatchCrawler.calls.clear()
    app = create_app(
        tmp_path / "crawl.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=NextBatchCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    client = app.test_client()

    page = client.get("/?source_id=enabled-source")
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'value="enabled-source" selected' in body
    assert "Enabled Zoo · Enabled Feed" in body
    assert NextBatchCrawler.calls == []

    for source_id in ("disabled-source", "disabled-zoo-source", "not-configured"):
        rejected = client.get("/", query_string={"source_id": source_id})
        assert rejected.status_code == 400
        assert "Invalid or disabled source selection." in rejected.get_data(as_text=True)
    assert NextBatchCrawler.calls == []


def test_control_source_labels_are_html_escaped(tmp_path):
    config_path = _source_config_file(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "name: Enabled Feed", "name: <script>alert(1)</script>"
        ),
        encoding="utf-8",
    )
    app = create_app(
        tmp_path / "crawl.sqlite",
        config_path,
        crawler_factory=NextBatchCrawler,
        storage_factory=Storage,
        thread_factory=WaitingThread,
    )
    body = app.test_client().get("/?source_id=enabled-source").get_data(as_text=True)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<script>alert(1)</script>" not in body


def test_control_next_batch_validates_source_and_csrf_before_background_call(tmp_path):
    NextBatchCrawler.calls.clear()
    app = create_app(
        tmp_path / "crawl.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=NextBatchCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
        clock=lambda: "now",
    )
    client = app.test_client()

    assert client.post(
        "/crawl",
        data={"operation": "next_batch", "source_id": "enabled-source"},
    ).status_code == 400
    token = _token(client)
    rejected = client.post(
        "/crawl",
        data={
            "csrf_token": token,
            "operation": "next_batch",
            "source_id": "https://enabled.example/feed.xml",
            "url": "https://attacker.example/evil",
            "selector": "body",
        },
    )
    assert rejected.status_code == 400
    assert NextBatchCrawler.calls == []

    started = client.post(
        "/crawl",
        data={"csrf_token": token, "operation": "next_batch", "source_id": "enabled-source"},
    )
    assert started.status_code == 303
    assert NextBatchCrawler.calls == [("enabled-source", 10)]


def test_control_next_batch_exposes_safe_result_summary_and_html_fields(tmp_path):
    NextBatchCrawler.calls.clear()
    app = create_app(
        tmp_path / "crawl.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=NextBatchCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
        clock=lambda: "now",
    )
    client = app.test_client()
    token = _token(client)
    assert client.post(
        "/crawl",
        data={"csrf_token": token, "operation": "next_batch", "source_id": "enabled-source"},
    ).status_code == 303
    payload = client.get("/status").get_json()
    assert payload["status"] == "completed"
    assert payload["source_id"] == "enabled-source"
    assert payload["summary"] == {
        "processed": 3,
        "stored": 2,
        "known": 1,
        "duplicate": 4,
        "errors": 0,
        "remaining_estimate": 7,
    }
    body = client.get("/").get_data(as_text=True)
    for label in ("Processed", "Stored", "Known", "Duplicates", "Errors", "Remaining estimate"):
        assert label in body


def test_control_next_batch_handles_background_failure_and_lease_busy_safely(tmp_path):
    class FailingCrawler:
        def __init__(self, config, storage):
            pass

        def next_batch(self, source_id, *, limit):
            raise RuntimeError("secret path /private and attacker URL")

    app = create_app(
        tmp_path / "crawl.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=FailingCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    client = app.test_client()
    assert client.post(
        "/crawl",
        data={"csrf_token": _token(client), "operation": "next_batch", "source_id": "enabled-source"},
    ).status_code == 303
    payload = client.get("/status").get_json()
    assert payload["status"] == "failed"
    assert payload["message"] == "Next batch failed."
    assert "private" not in client.get("/status").get_data(as_text=True)

    class CrawlerBusyError(Exception):
        pass

    class BusyCrawler:
        def __init__(self, config, storage):
            pass

        def next_batch(self, source_id, *, limit):
            raise CrawlerBusyError("do not expose this")

    busy_app = create_app(
        tmp_path / "busy.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=BusyCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    busy_client = busy_app.test_client()
    assert busy_client.post(
        "/crawl",
        data={"csrf_token": _token(busy_client), "operation": "next_batch", "source_id": "enabled-source"},
    ).status_code == 303
    busy_payload = busy_client.get("/status").get_json()
    assert busy_payload["status"] == "busy"
    assert busy_payload["message"] == "Next batch was rejected because another crawl is already running."

    class BusyResultCrawler:
        def __init__(self, config, storage):
            pass

        def crawl(self, selection):
            return Result(status="busy")

        def next_batch(self, source_id, *, limit):
            return Result(status="busy")

    next_result_app = create_app(
        tmp_path / "busy-result-next.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=BusyResultCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    next_result_client = next_result_app.test_client()
    assert next_result_client.post(
        "/crawl",
        data={
            "csrf_token": _token(next_result_client),
            "operation": "next_batch",
            "source_id": "enabled-source",
        },
    ).status_code == 303
    next_result_message = next_result_client.get("/status").get_json()["message"]
    assert next_result_message == "Next batch was rejected because another crawl is already running."

    all_result_app = create_app(
        tmp_path / "busy-result-all.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=BusyResultCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    all_result_client = all_result_app.test_client()
    assert all_result_client.post(
        "/crawl",
        data={"csrf_token": _token(all_result_client), "selection": "all"},
    ).status_code == 303
    all_result_message = all_result_client.get("/status").get_json()["message"]
    assert all_result_message == "Crawl was rejected because another crawl is already running."

    class BusyAllCrawler:
        def __init__(self, config, storage):
            pass

        def crawl(self, selection):
            raise CrawlerBusyError("do not expose this")

    all_exception_app = create_app(
        tmp_path / "busy-exception-all.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=BusyAllCrawler,
        storage_factory=Storage,
        thread_factory=ImmediateThread,
    )
    all_exception_client = all_exception_app.test_client()
    assert all_exception_client.post(
        "/crawl",
        data={"csrf_token": _token(all_exception_client), "selection": "all"},
    ).status_code == 303
    all_exception_payload = all_exception_client.get("/status").get_json()
    assert all_exception_payload["status"] == "busy"
    assert all_exception_payload["message"] == "Crawl was rejected because another crawl is already running."


def test_control_next_batch_rejects_duplicate_running_task(tmp_path):
    app = create_app(
        tmp_path / "crawl.sqlite",
        _source_config_file(tmp_path),
        crawler_factory=NextBatchCrawler,
        storage_factory=Storage,
        thread_factory=WaitingThread,
    )
    client = app.test_client()
    token = _token(client)
    data = {"csrf_token": token, "operation": "next_batch", "source_id": "enabled-source"}
    assert client.post("/crawl", data=data).status_code == 303
    assert client.post("/crawl", data=data).status_code == 409
    assert client.get("/status").get_json()["status"] == "running"
