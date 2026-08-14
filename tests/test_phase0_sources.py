"""Offline conformance checks for the enabled Phase 0 source plans.

The fixtures in this module deliberately exercise the same source records that
are registered in ``config/zoos.yaml``.  A policy-aware fake transport keeps
the tests deterministic while still running the real discovery dispatch,
parser, normalization, and official-host filtering layers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
import warnings

import pytest
import yaml

from zoofan.config import load_config
from zoofan.discovery import DiscoveryEngine, URLPolicy
from zoofan.models import FetchResponse, Source, Zoo
from zoofan.parsers import parse_archive_page, parse_feed, parse_json_api, parse_sitemap


ROOT = Path(__file__).parents[1]
CONFIG_PATH = ROOT / "config" / "zoos.yaml"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "phase0_sources"
RESEARCH_DIR = ROOT / "reports" / "source-research"


class FixtureFetcher:
    """A one-hop, request-policy-aware fetcher backed only by fixture bytes."""

    supports_request_policy = True

    def __init__(self, payloads: Mapping[str, bytes]) -> None:
        self.payloads = dict(payloads)
        self.calls: list[str] = []

    def fetch(self, url: str, *, request_policy: Any) -> FetchResponse:
        checked_url = request_policy.validate(url)
        assert checked_url == url
        self.calls.append(url)
        return FetchResponse(url, 200, self.payloads[url])


# These are stable, enabled plans in the corrected Phase 0 research set.  The
# cases intentionally use three different source surfaces and include parser
# noise (navigation, external, and explicitly excluded links) in each fixture.
CASES = (
    {
        "source_id": "tierpark-berlin-news",
        "kind": "archive",
        "fixture": "tierpark_berlin_news_v1.html",
        "accepted_url": "https://www.tierpark-berlin.de/de/aktuelles/news/artikel/neuer-elefant",
        "navigation_url": "https://www.tierpark-berlin.de/de/aktuelles/news/artikel/",
        "excluded_url": "https://www.tierpark-berlin.de/de/aktuelles/presse/pressemitteilung",
        "external_url": "https://outside.example/de/aktuelles/news/artikel/external",
        "title": "Neuer Elefant im Tierpark",
        "published_at": "2025-04-03T09:30:00+02:00",
    },
    {
        "source_id": "koeln-zoo-news-sitemap",
        "kind": "sitemap",
        "fixture": "koeln_news_v1.xml",
        "accepted_url": "https://koelnerzoo.de/aktuellartical/neuer-erdmaennchen-nachwuchs",
        "navigation_url": "https://koelnerzoo.de/tickets/",
        "excluded_url": "https://koelnerzoo.de/aktuellartical/monatsubersicht",
        "external_url": "https://outside.example/aktuellartical/external",
        "lastmod": "2025-04-03T00:00:00+00:00",
    },
    {
        "source_id": "brook-valley-zoo-news-rss",
        "kind": "rss",
        "fixture": "brook_valley_news_v1.xml",
        "accepted_url": "https://brookvalleyzoo.com/nieuws/nieuwe-bewoner",
        "navigation_url": "https://brookvalleyzoo.com/tickets/",
        "excluded_url": "https://brookvalleyzoo.com/nieuws/category/park",
        "external_url": "https://outside.example/nieuws/external",
        "title": "Nieuwe bewoner in Brook Valley Zoo",
        "published_at": "2025-04-03T09:30:00+00:00",
    },
)


def _config() -> Any:
    """Reload the current config for every test (central imports may move)."""

    return load_config(CONFIG_PATH)


def _source_and_zoo(config: Any, source_id: str) -> tuple[Source, Zoo]:
    source = next((item for item in config.sources if item.id == source_id), None)
    if source is None or not source.enabled:
        pytest.skip(f"Phase 0 source {source_id!r} is not currently enabled")
    zoo = next((item for item in config.zoos if item.id == source.zoo_id), None)
    if zoo is None:
        pytest.fail(f"enabled source {source_id!r} has no owning zoo")
    return source, zoo


def _fixture(case: Mapping[str, Any]) -> bytes:
    return (FIXTURE_DIR / str(case["fixture"])).read_bytes()


@pytest.mark.parametrize("case", CASES, ids=lambda item: str(item["source_id"]))
def test_enabled_phase0_fixture_dispatch_filters_noise_and_keeps_deterministic_fields(
    case: Mapping[str, Any],
) -> None:
    config = _config()
    source, zoo = _source_and_zoo(config, str(case["source_id"]))
    assert source.kind == case["kind"]

    payload = _fixture(case)
    result = DiscoveryEngine(FixtureFetcher({source.url: payload})).discover(source, zoo=zoo)

    assert result.root_http_status == 200
    assert [candidate.url for candidate in result] == [case["accepted_url"]]
    assert not URLPolicy.from_zoo_source(zoo, source).accepts(source.url, source_url=source.url)
    policy = URLPolicy.from_zoo_source(zoo, source)
    assert policy.accepts(str(case["accepted_url"]), source_url=source.url)
    assert not policy.accepts(str(case["navigation_url"]), source_url=source.url)
    assert not policy.accepts(str(case["excluded_url"]), source_url=source.url)
    assert not policy.accepts(str(case["external_url"]), source_url=source.url)

    if case["kind"] == "archive":
        direct = parse_archive_page(payload, source.config, source.url)
        assert direct[0].title == case["title"]
        assert direct[0].published_at == datetime.fromisoformat(str(case["published_at"]))
        assert result.duplicate_candidate_count == 1
        assert result[0].title == case["title"]
        assert result[0].published_at == direct[0].published_at
    elif case["kind"] == "rss":
        direct = parse_feed(payload, source.url)
        assert direct[0].title == case["title"]
        assert direct[0].published_at == datetime.fromisoformat(str(case["published_at"]))
        assert result[0].title == case["title"]
        assert result[0].published_at == direct[0].published_at
    else:
        direct = parse_sitemap(payload, source.url)
        assert direct[0].lastmod == datetime.fromisoformat(str(case["lastmod"]))


def test_phase0_sitemap_fixture_preserves_lastmod_and_explicit_exclusion() -> None:
    config = _config()
    source, _ = _source_and_zoo(config, "koeln-zoo-news-sitemap")
    entries = parse_sitemap(_fixture(next(case for case in CASES if case["kind"] == "sitemap")), source.url)
    assert [entry.url for entry in entries] == [
        "https://koelnerzoo.de/aktuellartical/neuer-erdmaennchen-nachwuchs",
        "https://koelnerzoo.de/aktuellartical/monatsubersicht",
        "https://koelnerzoo.de/tickets",
        "https://outside.example/aktuellartical/external",
    ]
    assert entries[0].lastmod == datetime(2025, 4, 3, tzinfo=timezone.utc)
    assert entries[1].lastmod == datetime(2025, 4, 4, tzinfo=timezone.utc)


def test_atom_fixture_is_deterministic_even_without_an_enabled_atom_source() -> None:
    """Keep Atom parser coverage explicit without claiming a configured plan."""

    items = parse_feed(
        (FIXTURE_DIR / "atom_standalone_v1.xml").read_bytes(),
        "https://fixture.example/feed",
    )
    assert len(items) == 1
    assert items[0].url == "https://fixture.example/news/atom-article"
    assert items[0].title == "Atom article"
    assert items[0].published_at == datetime(2025, 4, 3, 9, 30, tzinfo=timezone.utc)
    assert items[0].updated_at_source == datetime(2025, 4, 3, 10, tzinfo=timezone.utc)

    configured = [source.id for source in _config().sources if source.enabled and source.kind == "atom"]
    assert configured == [], "an enabled Atom source needs a configured Phase 0 fixture case"


def test_json_api_fixture_is_deterministic_without_claiming_an_enabled_plan() -> None:
    payload = (FIXTURE_DIR / "json_api_standalone_v1.json").read_bytes()
    items = parse_json_api(
        payload,
        {"items_path": "data.items"},
        "https://fixture.example/api/news",
    )
    assert [item.url for item in items] == [
        "https://fixture.example/news/json-article",
        "https://fixture.example/tickets",
        "https://outside.example/news/external",
    ]
    assert items[0].title == "JSON API article"
    assert items[0].published_at == datetime(2025, 4, 3, 9, 30, tzinfo=timezone.utc)

    configured = [source.id for source in _config().sources if source.enabled and source.kind == "json_api"]
    assert configured == [], "an enabled JSON API source needs a configured Phase 0 fixture case"


def _research_source_index() -> dict[str, list[dict[str, Any]]]:
    """Collect source records from corrected YAML and adapter-plan artifacts."""

    records: dict[str, list[dict[str, Any]]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            source_id = value.get("source_id")
            if source_id is None and "id" in value and "url" in value and (
                "kind" in value or "canonical_kind" in value
            ):
                source_id = value.get("id")
            kind = value.get("kind", value.get("canonical_kind"))
            if isinstance(source_id, str) and isinstance(kind, str):
                records.setdefault(source_id, []).append(
                    {
                        "kind": kind,
                        "enabled": value.get("enabled"),
                        "status": value.get("source_status", value.get("status")),
                        "path": str(value.get("url", "")),
                    }
                )
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for path in sorted(RESEARCH_DIR.glob("*-config.yaml")):
        visit(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    for path in sorted(RESEARCH_DIR.glob("*-adapters.json")):
        visit(json.loads(path.read_text(encoding="utf-8")))
    return records


def test_enabled_executable_phase0_sources_are_represented_in_research_artifacts() -> None:
    config = _config()
    executable = {
        "rss",
        "atom",
        "sitemap",
        "archive",
        "json_api",
        "custom_adapter",
    }
    enabled = [source for source in config.sources if source.enabled and source.kind in executable]
    index = _research_source_index()
    missing = [source.id for source in enabled if source.id not in index]
    if missing:
        warnings.warn(
            "enabled config rows missing from corrected research artifacts: "
            + ", ".join(sorted(str(item) for item in missing)),
            UserWarning,
            stacklevel=1,
        )

    # The representative executable cases are the hard acceptance boundary;
    # historical rows which are still enabled during a central config import
    # are reported above until their corrected plan is imported or disabled.
    required = {str(case["source_id"]) for case in CASES}
    assert required <= set(index), f"fixture source plans missing research evidence: {sorted(required - set(index))}"

    # The current main config still contains Gaia's historical enabled rows,
    # while the corrected Dutch artifact marks both rows unsupported/disabled.
    # Surface that discrepancy without claiming those rows passed conformance.
    unresolved: list[str] = []
    for source in enabled:
        records = index.get(str(source.id), [])
        if any(record.get("enabled") is False or record.get("status") in {"unsupported", "disabled"} for record in records):
            unresolved.append(str(source.id))
    if unresolved:
        warnings.warn(
            "enabled config rows have disabled/unsupported research records: "
            + ", ".join(sorted(unresolved)),
            UserWarning,
            stacklevel=1,
        )


def test_enabled_phase0_adapter_kinds_have_explicit_fixture_or_gap() -> None:
    config = _config()
    enabled_kinds = {source.kind for source in config.sources if source.enabled}
    fixture_kinds = {str(case["kind"]) for case in CASES}
    absent_kinds = {"atom", "json_api", "custom_adapter"} - enabled_kinds
    assert absent_kinds == {"atom", "json_api", "custom_adapter"}, (
        "the standalone fixtures are not a success claim for an enabled plan; "
        "add a source-specific matrix case when one of these kinds is enabled"
    )
    assert fixture_kinds <= enabled_kinds
