from __future__ import annotations

import builtins
import logging
import sys
import types

import cli


def test_dashboard_arguments_are_forwarded(monkeypatch):
    calls = []
    module = types.ModuleType("zoofan.dashboard")

    def dashboard_main(db_path, host, port, control_url):
        calls.append((db_path, host, port, control_url))

    module.main = dashboard_main
    monkeypatch.setitem(sys.modules, "zoofan.dashboard", module)

    assert cli.main([
        "dashboard", "--db", "crawler.sqlite", "--host", "0.0.0.0", "--port", "8123",
        "--control-url", "https://control.example",
    ]) == 0
    assert calls == [("crawler.sqlite", "0.0.0.0", 8123, "https://control.example")]


def test_dashboard_defaults_are_forwarded(monkeypatch):
    calls = []
    module = types.ModuleType("zoofan.dashboard")
    module.main = lambda db_path, host, port, control_url: calls.append((db_path, host, port, control_url))
    monkeypatch.setitem(sys.modules, "zoofan.dashboard", module)

    assert cli.main(["dashboard"]) == 0
    assert calls == [("data/zoofan.db", "127.0.0.1", 8000, None)]


def test_control_arguments_are_forwarded(monkeypatch):
    calls = []
    module = types.ModuleType("zoofan.control")
    module.main = lambda db_path, config_path, host, port, dashboard_url: calls.append(
        (db_path, config_path, host, port, dashboard_url)
    )
    monkeypatch.setitem(sys.modules, "zoofan.control", module)

    assert cli.main([
        "--config", "config/test.yaml", "control", "--db", "crawler.sqlite",
        "--host", "0.0.0.0", "--port", "8124", "--dashboard-url", "https://dashboard.example",
    ]) == 0
    assert calls == [
        ("crawler.sqlite", cli.Path("config/test.yaml"), "0.0.0.0", 8124, "https://dashboard.example")
    ]


def test_control_defaults_are_forwarded(monkeypatch):
    calls = []
    module = types.ModuleType("zoofan.control")
    module.main = lambda db_path, config_path, host, port, dashboard_url: calls.append(
        (db_path, config_path, host, port, dashboard_url)
    )
    monkeypatch.setitem(sys.modules, "zoofan.control", module)

    assert cli.main(["control"]) == 0
    assert calls == [
        ("data/zoofan.db", cli.DEFAULT_CONFIG, "127.0.0.1", 8001, "http://127.0.0.1:8000")
    ]


def test_missing_dashboard_dependency_is_nonzero_and_clear(monkeypatch, caplog):
    real_import = builtins.__import__

    def import_with_missing_dependency(name, *args, **kwargs):
        if name == "zoofan.dashboard":
            raise ModuleNotFoundError("No module named 'flask'", name="flask")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_with_missing_dependency)
    with caplog.at_level(logging.ERROR):
        result = cli.main(["dashboard"])
    assert result != 0
    assert "missing dependency flask" in caplog.text


def test_missing_optional_target_is_nonzero(monkeypatch, caplog):
    real_import = builtins.__import__

    def import_without_endgoal(name, *args, **kwargs):
        if name == "zoofan.endgoal":
            raise ModuleNotFoundError("No module named 'zoofan.endgoal'", name="zoofan.endgoal")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_endgoal)
    with caplog.at_level(logging.ERROR):
        result = cli.main(["endgoal"])
    assert result != 0
    assert "optional module zoofan.endgoal is not installed" in caplog.text


def test_crawl_completed_with_errors_returns_nonzero(monkeypatch, tmp_path):
    config_module = types.ModuleType("zoofan.config")
    config_module.load_config = lambda path: object()
    crawler_module = types.ModuleType("zoofan.crawler")
    storage_module = types.ModuleType("zoofan.storage")

    class FakeResult:
        status = "completed_with_errors"

        def as_dict(self):
            return {"status": self.status}

    class FakeCrawler:
        def __init__(self, config, storage):
            pass

        def crawl(self, selection):
            assert selection == "all"
            return FakeResult()

    class FakeStorage:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    crawler_module.Crawler = FakeCrawler
    storage_module.SQLiteStorage = FakeStorage
    monkeypatch.setitem(sys.modules, "zoofan.config", config_module)
    monkeypatch.setitem(sys.modules, "zoofan.crawler", crawler_module)
    monkeypatch.setitem(sys.modules, "zoofan.storage", storage_module)

    assert cli.main(["crawl", "all", "--db", str(tmp_path / "crawler.sqlite")]) == 1
