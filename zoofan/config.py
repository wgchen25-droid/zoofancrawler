"""Configuration records and a small YAML loader for source definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .models import Source, Zoo


def parse_bool(value: Any, default: bool = False) -> bool:
    """Parse booleans without Python's surprising ``bool('false')`` behavior."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "y", "on", "1"}:
            return True
        if normalized in {"false", "no", "n", "off", "0"}:
            return False
    raise ValueError("expected a boolean value")


@dataclass
class ArchiveParserConfig:
    """CSS selectors used by the generic server-rendered archive parser."""

    article_selector: str = "article"
    link_selector: str = "a[href]"
    title_selector: Optional[str] = None
    date_selector: Optional[str] = None
    pagination_selector: Optional[str] = None
    max_pages: int = 1

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "ArchiveParserConfig":
        value = value or {}
        # Accept both concise keys and the longer names often used in YAML.
        return cls(
            article_selector=str(value.get("article_selector", value.get("article", "article"))),
            link_selector=str(value.get("link_selector", value.get("link", "a[href]"))),
            title_selector=value.get("title_selector", value.get("title")),
            date_selector=value.get("date_selector", value.get("date")),
            pagination_selector=value.get(
                "pagination_selector", value.get("pagination")
            ),
            max_pages=max(1, int(value.get("max_pages", value.get("pages", 1)))),
        )


@dataclass
class CrawlerConfig:
    """Top-level loaded crawler configuration."""

    zoos: list[Zoo] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    user_agent: str = "ZooFanCrawler/0.1"
    timeout: float = 20.0
    request_delay: float = 1.0
    max_retries: int = 3


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def load_config(path: Any) -> CrawlerConfig:
    """Load ``zoos.yaml``-style configuration.

    The loader intentionally only maps data into domain records; it does not
    initialize storage or perform network activity.
    """

    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency declaration path
        raise RuntimeError("PyYAML is required to load crawler configuration") from exc

    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    root = _mapping(data)
    raw_zoos = root.get("zoos", [])
    if isinstance(raw_zoos, Mapping):
        raw_zoos = [dict(value, slug=key) if isinstance(value, Mapping) else {"slug": key} for key, value in raw_zoos.items()]
    zoos: list[Zoo] = []
    sources: list[Source] = []
    for raw in raw_zoos if isinstance(raw_zoos, list) else []:
        item = _mapping(raw)
        zoo = Zoo(
            id=item.get("id"),
            slug=str(item.get("slug", item.get("key", item.get("name", "")))),
            name=str(item.get("name", item.get("slug", ""))),
            website_url=item.get("website_url", item.get("url", item.get("base_url"))),
            enabled=parse_bool(item.get("enabled"), True),
            metadata=dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
        )
        zoos.append(zoo)
        raw_sources = item.get("sources", [])
        if isinstance(raw_sources, Mapping):
            raw_sources = [dict(value, name=key) if isinstance(value, Mapping) else {"name": key, "url": value} for key, value in raw_sources.items()]
        for raw_source in raw_sources if isinstance(raw_sources, list) else []:
            source_item = _mapping(raw_source)
            config = dict(source_item.get("config", {})) if isinstance(source_item.get("config"), Mapping) else {}
            # Selector keys may be specified directly on a source.
            for selector_key in ("article_selector", "link_selector", "title_selector", "date_selector", "pagination_selector", "max_pages"):
                if selector_key in source_item:
                    config[selector_key] = source_item[selector_key]
            sources.append(
                Source(
                    id=source_item.get("id"),
                    zoo_id=zoo.id or zoo.slug,
                    url=str(source_item.get("url", "")),
                    kind=str(source_item.get("kind", source_item.get("type", "rss"))),
                    name=source_item.get("name"),
                    config=config,
                    enabled=parse_bool(source_item.get("enabled"), True),
                )
            )
    return CrawlerConfig(
        zoos=zoos,
        sources=sources,
        user_agent=str(root.get("user_agent", "ZooFanCrawler/0.1")),
        timeout=float(root.get("timeout", 20.0)),
        request_delay=float(root.get("request_delay", root.get("delay", 1.0))),
        max_retries=int(root.get("max_retries", root.get("retries", 3))),
    )


def load_zoos(path: Any) -> CrawlerConfig:
    """Backward-friendly alias for :func:`load_config`."""

    return load_config(path)
