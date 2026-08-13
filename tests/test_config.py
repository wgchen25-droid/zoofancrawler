from pathlib import Path

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
