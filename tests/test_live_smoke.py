from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import cli
from zoofan.config import CrawlerConfig
from zoofan.crawler import Crawler
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.storage import SQLiteStorage


class MapFetcher:
    supports_request_policy = True

    def __init__(self, responses, failures=()):
        self.responses = dict(responses)
        self.failures = set(failures)
        self.calls = []

    def fetch(self, url, *, request_policy):
        assert request_policy.validate(url) == url
        self.calls.append(url)
        if url in self.failures:
            raise RuntimeError(f"site unavailable: {url}")
        value = self.responses[url]
        if isinstance(value, FetchResponse):
            return value
        return FetchResponse(url, 200, str(value).encode("utf-8"))


def _config(*, zoos, sources):
    return CrawlerConfig(zoos=list(zoos), sources=list(sources), request_delay=0)


def _rss(urls):
    links = "".join(f"<item><link>{url}</link></item>" for url in urls)
    return f"<rss><channel>{links}</channel></rss>"


def _article(title):
    return f"<html><head><title>{title}</title></head><article>{title} body</article></html>"


def test_candidate_limit_keeps_discovery_evidence_and_fetches_only_bounded_pages():
    zoo = Zoo(id="z1", slug="z1", name="One", website_url="https://official.example/")
    source = Source(
        id="feed", zoo_id="z1", kind="rss", url="https://official.example/feed",
        config={"official_host": "official.example", "allow_regex": r"/news/"},
    )
    urls = [f"https://official.example/news/{index}" for index in range(3)]
    fetcher = MapFetcher(
        {source.url: _rss(urls), urls[0]: _article("One")},
    )
    result = Crawler(
        _config(zoos=[zoo], sources=[source]),
        storage=SQLiteStorage(":memory:"),
        fetcher=fetcher,
    ).crawl(max_candidates_per_source=1)

    assert fetcher.calls == [source.url, urls[0]]
    assert result.stats[0].discovered_count == 3
    assert result.stats[0].metadata["candidates_processed"] == 1
    assert result.stats[0].metadata["candidates_limited"] == 2
    assert result.zoo_results[0].metadata["candidates_limited"] == 2
    payload = result.as_dict()
    assert payload["candidates_processed"] == 1
    assert payload["candidates_limited"] == 2
    assert payload["coverage"]["processed_zoos"] == ["z1"]
    assert payload["coverage"]["exactly_one_result_per_processed_zoo"] is True


def test_max_pages_bounds_archive_pagination_without_mutating_source_config():
    zoo = Zoo(id="z1", slug="z1", name="One", website_url="https://official.example/")
    source = Source(
        id="archive", zoo_id="z1", kind="archive", url="https://official.example/archive",
        config={
            "official_host": "official.example",
            "article_selector": ".card",
            "link_selector": "a.article",
            "pagination_selector": "a.next",
            "max_pages": 2,
        },
    )
    page_two = "https://official.example/archive?page=2"
    first = "https://official.example/news/one"
    second = "https://official.example/news/two"
    fetcher = MapFetcher(
        {
            source.url: "<div class='card'><a class='article' href='/news/one'>One</a></div>"
            "<a class='next' href='/archive?page=2'>Next</a>",
            page_two: "<div class='card'><a class='article' href='/news/two'>Two</a></div>",
            first: _article("One"),
            second: _article("Two"),
        }
    )
    storage = SQLiteStorage(":memory:")
    result = Crawler(_config(zoos=[zoo], sources=[source]), storage=storage, fetcher=fetcher).crawl(
        max_pages=1
    )

    assert fetcher.calls == [source.url, first]
    assert result.inserted_count == 1
    assert source.config["max_pages"] == 2
    assert result.zoo_results[0].metadata["max_pages"] == 1


def test_live_site_failure_isolated_and_result_coverage_is_exactly_one_per_zoo():
    first_zoo = Zoo(id="bad-zoo", slug="bad-zoo", name="Bad", website_url="https://bad.example/")
    second_zoo = Zoo(id="good-zoo", slug="good-zoo", name="Good", website_url="https://good.example/")
    first_source = Source(
        id="bad-feed", zoo_id="bad-zoo", kind="rss", url="https://bad.example/feed",
        config={"official_host": "bad.example"},
    )
    second_source = Source(
        id="good-feed", zoo_id="good-zoo", kind="rss", url="https://good.example/feed",
        config={"official_host": "good.example", "allow_regex": r"/news/"},
    )
    article = "https://good.example/news/one"
    fetcher = MapFetcher(
        {second_source.url: _rss([article]), article: _article("Good")},
        failures={first_source.url},
    )
    result = Crawler(
        _config(zoos=[first_zoo, second_zoo], sources=[first_source, second_source]),
        storage=SQLiteStorage(":memory:"),
        fetcher=fetcher,
    ).crawl("all", max_candidates_per_source=1)

    by_zoo = {item.zoo_id: item for item in result.zoo_results}
    assert set(by_zoo) == {"bad-zoo", "good-zoo"}
    assert by_zoo["bad-zoo"].status == "failed"
    assert by_zoo["bad-zoo"].error_category == "parse_error"
    assert by_zoo["good-zoo"].status == "success"
    assert by_zoo["good-zoo"].inserted == 1
    coverage = result.as_dict()["coverage"]
    assert coverage["processed_zoos"] == ["bad-zoo", "good-zoo"]
    assert coverage["zoo_result_counts"] == {"bad-zoo": 1, "good-zoo": 1}
    assert coverage["exactly_one_result_per_processed_zoo"] is True


def _install_cli_stubs(monkeypatch, config, calls, *, result=None, crawl_error=None):
    config_module = types.ModuleType("zoofan.config")
    config_module.load_config = lambda path: config
    crawler_module = types.ModuleType("zoofan.crawler")
    storage_module = types.ModuleType("zoofan.storage")

    class FakeResult:
        status = "completed_with_errors"

        def as_dict(self):
            return result or {
                "status": self.status,
                "processed": 2,
                "enabled": 2,
                "processed_zoos": ["z1", "z2"],
                "enabled_zoos": ["z1", "z2"],
                "selected_zoos": ["z1", "z2"],
                "coverage": {
                    "processed": 2,
                    "enabled": 2,
                    "processed_zoos": ["z1", "z2"],
                    "enabled_zoos": ["z1", "z2"],
                    "selected_zoos": ["z1", "z2"],
                    "zoo_result_counts": {"z1": 1, "z2": 1},
                    "missing_processed_zoos": [],
                    "duplicate_processed_zoos": [],
                    "exactly_one_result_per_processed_zoo": True,
                },
                "zoo_results": [
                    {"zoo_id": "z1", "status": "failed", "source_status": "http_error", "error_summary": "down"},
                    {"zoo_id": "z2", "status": "success", "source_status": "success"},
                ],
            }

    class FakeCrawler:
        def __init__(self, loaded_config, storage):
            assert loaded_config is config

        def crawl(self, selection, *, since_days=None, max_candidates_per_source=None, max_pages=None):
            calls.append((selection, since_days, max_candidates_per_source, max_pages))
            if crawl_error:
                raise crawl_error
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


def test_live_cli_is_structured_bounded_and_site_failures_do_not_change_exit_code(monkeypatch, capsys):
    config = SimpleNamespace(
        zoos=[
            SimpleNamespace(id="z1", groups=("smoke",), enabled=True),
            SimpleNamespace(id="z2", groups=("smoke",), enabled=True),
        ]
    )
    calls = []
    _install_cli_stubs(monkeypatch, config, calls)

    assert cli.main(
        [
            "crawl",
            "--group",
            "smoke",
            "--live",
            "--since-days",
            "7",
            "--max-candidates-per-source",
            "1",
            "--max-pages",
            "1",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [(["z1", "z2"], 7, 1, 1)]
    assert payload["live"] is True
    assert payload["mode"] == "live_smoke"
    assert payload["runner_status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["limits"] == {"max_candidates_per_source": 1, "max_pages": 1}
    assert payload["site_failure_count"] == 1
    assert payload["site_failures"][0]["error_category"] == "http_error"
    assert payload["coverage"]["exactly_one_result_per_processed_zoo"] is True


def test_live_cli_runner_error_is_nonzero_and_machine_readable(monkeypatch, capsys):
    config = SimpleNamespace(zoos=[SimpleNamespace(id="z1", groups=("smoke",), enabled=True)])
    calls = []
    _install_cli_stubs(monkeypatch, config, calls, crawl_error=RuntimeError("runner exploded"))

    assert cli.main(["crawl", "--group", "smoke", "--live", "--max-pages", "1"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert calls == [(["z1"], None, None, 1)]
    assert payload["status"] == "runner_error"
    assert payload["runner_status"] == "failed"
    assert payload["exit_code"] == 1
    assert payload["site_failures"] == []


def test_live_cli_unknown_group_is_configuration_error_not_silent_skip(monkeypatch, capsys):
    config = SimpleNamespace(zoos=[SimpleNamespace(id="z1", groups=(), enabled=True)])
    calls = []
    _install_cli_stubs(monkeypatch, config, calls)

    assert cli.main(["crawl", "--group", "smoke", "--live"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert calls == []
    assert payload["status"] == "configuration_error"
    assert payload["error_category"] == "configuration_error"
    assert payload["coverage"]["exactly_one_result_per_processed_zoo"] is False
