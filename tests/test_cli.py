from __future__ import annotations

import builtins
import json
import logging
from pathlib import Path
import sys
import types
from types import SimpleNamespace

import cli
import pytest


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


def _install_crawl_stubs(monkeypatch, config, calls, *, supports_since_days=False):
    config_module = types.ModuleType("zoofan.config")
    config_module.load_config = lambda path: config
    crawler_module = types.ModuleType("zoofan.crawler")
    storage_module = types.ModuleType("zoofan.storage")

    class FakeResult:
        status = "completed"

        def as_dict(self):
            return {
                "status": self.status,
                "zoo_results": [{"zoo_id": "z1"}],
                "metadata": {"processed": 1, "enabled": 2, "processed_zoos": ["z1"]},
            }

    if supports_since_days:
        def crawl(self, selection, *, since_days=None):
            calls.append((selection, since_days))
            return FakeResult()
    else:
        def crawl(self, selection):
            calls.append((selection, None))
            return FakeResult()
    crawl_method = crawl

    class FakeCrawler:
        def __init__(self, loaded_config, storage):
            assert loaded_config is config

        crawl = crawl_method

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


def test_crawl_group_selection_is_config_driven_and_deduplicated(monkeypatch, capsys):
    config = SimpleNamespace(
        zoos=[
            SimpleNamespace(id="z1", slug="one", groups=("batch",), enabled=True),
            SimpleNamespace(id="z1", slug="duplicate", groups=("batch",), enabled=True),
            SimpleNamespace(id="z2", slug="two", groups=("batch",), enabled=False),
        ]
    )
    calls = []
    _install_crawl_stubs(monkeypatch, config, calls)

    assert cli.main(["crawl", "--group", "batch", "--live"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [(["z1"], None)]
    assert payload["zoo_results"]
    assert "coverage" in payload
    assert payload["live"] is True


def test_crawl_unknown_group_is_nonzero_and_does_not_run(monkeypatch, caplog):
    config = SimpleNamespace(zoos=[SimpleNamespace(id="z1", groups=(), enabled=True)])
    calls = []
    _install_crawl_stubs(monkeypatch, config, calls)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["crawl", "--group", "missing"]) != 0
    assert calls == []
    assert "unknown or has no enabled zoos" in caplog.text


def test_crawl_unknown_zoo_is_clean_configuration_error(monkeypatch, caplog):
    config = types.ModuleType("zoofan.config")
    config.load_config = lambda path: SimpleNamespace(zoos=[], sources=[])
    crawler_module = types.ModuleType("zoofan.crawler")
    storage_module = types.ModuleType("zoofan.storage")

    class FakeCrawler:
        def __init__(self, loaded_config, storage):
            pass

        def crawl(self, selection):
            raise ValueError(f"unknown zoo selection: {selection}")

    class FakeStorage:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    crawler_module.Crawler = FakeCrawler
    storage_module.SQLiteStorage = FakeStorage
    monkeypatch.setitem(sys.modules, "zoofan.config", config)
    monkeypatch.setitem(sys.modules, "zoofan.crawler", crawler_module)
    monkeypatch.setitem(sys.modules, "zoofan.storage", storage_module)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["crawl", "--zoo", "missing"]) == 2
    assert "configuration error: unknown zoo selection: missing" in caplog.text
    assert "Traceback" not in caplog.text


def test_crawl_since_days_rejects_unsupported_crawler(monkeypatch, caplog):
    config = SimpleNamespace(zoos=[])
    calls = []
    _install_crawl_stubs(monkeypatch, config, calls)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["crawl", "--since-days", "7"]) != 0
    assert calls == []
    assert "--since-days is not supported" in caplog.text


def test_crawl_since_days_is_validated_before_forwarding(monkeypatch, caplog):
    config = SimpleNamespace(zoos=[])
    calls = []
    _install_crawl_stubs(monkeypatch, config, calls, supports_since_days=True)

    with caplog.at_level(logging.ERROR):
        assert cli.main(["crawl", "--since-days", "-1"]) != 0
    assert calls == []
    assert "must be non-negative" in caplog.text

    assert cli.main(["crawl", "--since-days", "7"]) == 0
    assert calls == [("all", 7)]


def test_crawl_help_describes_since_days_behavior(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["crawl", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    normalized_help = " ".join(help_text.split())
    assert "retain candidates newer than N days" in normalized_help
    assert "undated candidates are retained" in normalized_help
    assert "requires crawler support" not in normalized_help


def test_validate_config_json_uses_strict_mode(monkeypatch, capsys):
    calls = []
    config_module = types.ModuleType("zoofan.config")

    class Validation:
        valid = False
        errors = [{"path": "zoos[0]", "message": "bad", "code": "value"}]

        def as_dict(self):
            return {"valid": self.valid, "errors": self.errors}

    def validate(path, *, strict=False):
        calls.append((path, strict))
        return Validation()

    config_module.validate_config = validate
    monkeypatch.setitem(sys.modules, "zoofan.config", config_module)

    assert cli.main(["--config", "broken.yaml", "validate-config", "--json"]) != 0
    assert calls == [(Path("broken.yaml"), True)]
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_build_acceptance_report_forwards_paths_and_prints_json(monkeypatch, capsys, tmp_path):
    calls = []
    reporting_module = types.ModuleType("zoofan.reporting")

    def build_reports(*, config_path, db_path, output_dir):
        calls.append((config_path, db_path, output_dir))
        return {
            "output_dir": tmp_path / "latest",
            "paths": {"run-summary.json": tmp_path / "latest" / "run-summary.json"},
        }

    reporting_module.build_reports = build_reports
    monkeypatch.setitem(sys.modules, "zoofan.reporting", reporting_module)

    assert cli.main([
        "--config", "custom.yaml", "build-acceptance-report", "--db", "crawl.db",
        "--output-dir", str(tmp_path / "reports"),
    ]) == 0
    assert calls == [(Path("custom.yaml"), "crawl.db", str(tmp_path / "reports"))]
    payload = json.loads(capsys.readouterr().out)
    assert payload["paths"]["run-summary.json"].endswith("run-summary.json")


def test_acceptance_is_an_alias_for_endgoal_and_forwards_config(monkeypatch):
    calls = []
    module = types.ModuleType("zoofan.endgoal")
    module.main = lambda config_path=None: calls.append(config_path) or 0
    monkeypatch.setitem(sys.modules, "zoofan.endgoal", module)

    assert cli.main(["--config", "custom.yaml", "acceptance"]) == 0
    assert calls == [Path("custom.yaml")]


def test_candidate_limit_parser_defaults_to_crawler_and_accepts_override():
    parser = cli._parser()

    crawl_defaults = parser.parse_args(["crawl"])
    scheduler_defaults = parser.parse_args(["scheduler"])
    assert crawl_defaults.max_candidates_per_source is None
    assert scheduler_defaults.max_candidates_per_source is None

    crawl_override = parser.parse_args(["crawl", "--max-candidates-per-source", "7"])
    scheduler_override = parser.parse_args(["scheduler", "--max-candidates-per-source", "11"])
    assert crawl_override.max_candidates_per_source == 7
    assert scheduler_override.max_candidates_per_source == 11


@pytest.mark.parametrize("command", ["crawl", "scheduler"])
@pytest.mark.parametrize("value", ["0", "-1"])
def test_candidate_limit_parser_rejects_non_positive_values(command, value, capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli._parser().parse_args([command, "--max-candidates-per-source", value])

    assert exc_info.value.code == 2
    assert "must be a positive integer" in capsys.readouterr().err


def _install_scheduler_stubs(monkeypatch, calls):
    config_module = types.ModuleType("zoofan.config")
    config_module.load_config = lambda path: object()
    crawler_module = types.ModuleType("zoofan.crawler")
    storage_module = types.ModuleType("zoofan.storage")
    scheduler_module = types.ModuleType("zoofan.scheduler")

    class FakeResult:
        status = "completed"

        def as_dict(self):
            return {"status": self.status}

    class FakeCrawler:
        def __init__(self, config, storage):
            pass

        def crawl(self, selection, **kwargs):
            calls.append(("crawl", selection, kwargs))
            return FakeResult()

    class FakeStorage:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeScheduler:
        def __init__(self, crawler, *, hours=6.0, max_candidates_per_source=None):
            assert hours == 6.0
            scheduler_kwargs = {"hours": hours}
            if max_candidates_per_source is not None:
                scheduler_kwargs["max_candidates_per_source"] = max_candidates_per_source
            calls.append(("scheduler", scheduler_kwargs))
            self.crawler = crawler
            self.max_candidates_per_source = max_candidates_per_source

        def run_once(self):
            if self.max_candidates_per_source is None:
                return self.crawler.crawl("all")
            return self.crawler.crawl("all", max_candidates_per_source=self.max_candidates_per_source)

        def start(self, *, blocking):
            assert blocking is True
            return self.run_once()

    crawler_module.Crawler = FakeCrawler
    storage_module.SQLiteStorage = FakeStorage
    scheduler_module.CrawlScheduler = FakeScheduler
    monkeypatch.setitem(sys.modules, "zoofan.config", config_module)
    monkeypatch.setitem(sys.modules, "zoofan.crawler", crawler_module)
    monkeypatch.setitem(sys.modules, "zoofan.storage", storage_module)
    monkeypatch.setitem(sys.modules, "zoofan.scheduler", scheduler_module)


def test_scheduler_once_forwards_candidate_limit(monkeypatch, capsys):
    calls = []
    _install_scheduler_stubs(monkeypatch, calls)

    assert cli.main(["scheduler", "--once", "--max-candidates-per-source", "7"]) == 0
    assert calls == [
        ("scheduler", {"hours": 6.0, "max_candidates_per_source": 7}),
        ("crawl", "all", {"max_candidates_per_source": 7}),
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_scheduler_once_without_candidate_override_uses_crawler_default(monkeypatch, capsys):
    calls = []
    _install_scheduler_stubs(monkeypatch, calls)

    assert cli.main(["scheduler", "--once"]) == 0
    assert calls == [
        ("scheduler", {"hours": 6.0}),
        ("crawl", "all", {}),
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


def test_crawl_scheduler_run_once_forwards_only_explicit_limit():
    from zoofan.scheduler import CrawlScheduler

    calls = []

    class FakeCrawler:
        def crawl(self, selection, **kwargs):
            calls.append((selection, kwargs))
            return "done"

    crawler = FakeCrawler()
    assert CrawlScheduler(crawler).run_once() == "done"
    assert CrawlScheduler(crawler, max_candidates_per_source=3).run_once() == "done"
    assert calls == [
        ("all", {}),
        ("all", {"max_candidates_per_source": 3}),
    ]
