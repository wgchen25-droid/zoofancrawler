from pathlib import Path

import pytest

from zoofan.config import load_config


def test_registry_contains_confirmed_zoo_sources():
    config = load_config(Path(__file__).parents[1] / "config" / "zoos.yaml")
    assert {z.slug for z in config.zoos} == {"zoo-berlin", "koeln-zoo", "gaiazoo"}
    by_zoo = {z.slug: [s for s in config.sources if s.zoo_id == z.id] for z in config.zoos}
    berlin = {source.kind: source for source in by_zoo["zoo-berlin"]}
    assert berlin["archive"].config["max_pages"] == 2
    assert "/de/aktuelles/news/artikel/" in berlin["archive"].config["article_allow"]
    koeln = by_zoo["koeln-zoo"]
    assert any(source.kind == "sitemap" for source in koeln)
    gaia = by_zoo["gaiazoo"]
    assert [source.kind for source in gaia][:2] == ["rss", "archive"]
    assert {(z.slug, z.country_code, z.language) for z in config.zoos} == {
        ("zoo-berlin", "DE", "de"), ("koeln-zoo", "DE", "de"), ("gaiazoo", "NL", "nl")
    }
    assert len(config.sources) == 8
    assert {source.language for source in by_zoo["zoo-berlin"]} == {"de"}
    assert {source.language for source in by_zoo["koeln-zoo"]} == {"de"}
    assert {source.language for source in by_zoo["gaiazoo"]} == {"nl"}


def _write_config(tmp_path, *, country="DE", zoo_language="de", source_language="de", kind="rss"):
    path = tmp_path / "zoos.yaml"
    values = {
        "country": f"    country_code: {country}\n" if country is not None else "",
        "zoo_language": f"    language: {zoo_language}\n" if zoo_language is not None else "",
        "source_language": f"        language: {source_language}\n" if source_language is not None else "",
        "kind": kind,
    }
    path.write_text(
        "zoos:\n  - id: z\n    slug: z\n    name: Zoo\n"
        + values["country"] + values["zoo_language"]
        + "    website_url: https://example.org\n    sources:\n      - id: s\n"
        + values["source_language"] + f"        kind: {values['kind']}\n        url: https://example.org/feed\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"country": None}, "country_code is required"),
        ({"zoo_language": None}, "language is required"),
        ({"source_language": None}, "language is required"),
        ({"country": "ZZ"}, "country_code is unsupported or malformed"),
        ({"country": "de"}, "country_code is unsupported or malformed"),
        ({"zoo_language": "DE"}, "language is unsupported or malformed"),
        ({"source_language": "xx"}, "language is unsupported or malformed"),
        ({"kind": "browser"}, "kind is unsupported"),
    ],
)
def test_config_rejects_missing_or_invalid_metadata(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        load_config(_write_config(tmp_path, **kwargs))


def test_config_accepts_required_metadata(tmp_path):
    config = load_config(_write_config(tmp_path))
    assert config.zoos[0].country_code == "DE"
    assert config.zoos[0].language == "de"
    assert config.sources[0].language == "de"
    assert config.sources[0].kind == "rss"
