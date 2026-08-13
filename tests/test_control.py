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
