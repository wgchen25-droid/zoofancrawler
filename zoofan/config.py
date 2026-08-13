"""Configuration records and a small YAML loader for source definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from .models import Source, Zoo


SUPPORTED_SOURCE_KINDS = frozenset({"rss", "atom", "sitemap", "archive", "registry_only"})
# Current ISO 3166-1 alpha-2 assignments and ISO 639-1 primary language
# subtags.  Keeping these tables local avoids making config loading depend on
# an optional locale/network package.
SUPPORTED_COUNTRY_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL
BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV
CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD
GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM
IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK
LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW
MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR
PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS
ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG UM US UY
UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
SUPPORTED_LANGUAGES = frozenset("""
aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co
cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl
gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg
ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk
ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps
pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta
te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za
zh zu
""".split())
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_PRIMARY_LANGUAGE = re.compile(r"^[a-z]{2,3}$")


def _required_code(item: Mapping[str, Any], key: str, *, context: str, pattern: re.Pattern[str], supported: frozenset[str]) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} is required")
    value = value.strip()
    if not pattern.fullmatch(value) or value not in supported:
        raise ValueError(f"{context}.{key} is unsupported or malformed: {value!r}")
    return value


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
        context = f"zoo[{item.get('id', item.get('slug', item.get('name', '?')))}]"
        country_code = _required_code(item, "country_code", context=context, pattern=_COUNTRY_CODE, supported=SUPPORTED_COUNTRY_CODES)
        language = _required_code(item, "language", context=context, pattern=_PRIMARY_LANGUAGE, supported=SUPPORTED_LANGUAGES)
        zoo = Zoo(
            id=item.get("id"),
            slug=str(item.get("slug", item.get("key", item.get("name", "")))),
            name=str(item.get("name", item.get("slug", ""))),
            website_url=item.get("website_url", item.get("url", item.get("base_url"))),
            country_code=country_code,
            language=language,
            enabled=parse_bool(item.get("enabled"), True),
            metadata=dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
        )
        zoos.append(zoo)
        raw_sources = item.get("sources", [])
        if isinstance(raw_sources, Mapping):
            raw_sources = [dict(value, name=key) if isinstance(value, Mapping) else {"name": key, "url": value} for key, value in raw_sources.items()]
        for raw_source in raw_sources if isinstance(raw_sources, list) else []:
            source_item = _mapping(raw_source)
            source_context = f"source[{source_item.get('id', source_item.get('name', '?'))}]"
            source_language = _required_code(source_item, "language", context=source_context, pattern=_PRIMARY_LANGUAGE, supported=SUPPORTED_LANGUAGES)
            kind = str(source_item.get("kind", source_item.get("type", "rss")))
            if kind not in SUPPORTED_SOURCE_KINDS:
                raise ValueError(f"{source_context}.kind is unsupported: {kind!r}")
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
                    kind=kind,
                    name=source_item.get("name"),
                    language=source_language,
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
