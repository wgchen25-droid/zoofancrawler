"""Configuration records and a small YAML loader for source definitions."""

from __future__ import annotations

from dataclasses import dataclass, field, fields as dataclass_fields
import hashlib
from pathlib import Path
import re
from typing import Any, Iterable, List, Mapping, Optional, Union
from urllib.parse import urlsplit

from .adapters import (
    ADAPTER_ALIASES,
    CANONICAL_ADAPTER_KINDS,
    canonical_adapter_kind,
    is_custom_adapter_name,
    normalize_adapter_token,
)
from .models import Source, Zoo
from .normalization import normalize_url
from .statuses import SOURCE_STATUS_VOCABULARY


# Keep these names exported from config for compatibility.  Taxonomy and
# aliases are defined once in ``adapters.py`` and imported by discovery too.
SUPPORTED_SOURCE_KINDS = CANONICAL_ADAPTER_KINDS
SOURCE_ADAPTERS = ADAPTER_ALIASES
_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._~-][a-z0-9]+)*$")
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
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

# The expanded Phase 0 roster is intentionally a closed evidence boundary.
# These constants are used only for the explicitly documented null-field
# exceptions below; ordinary registry records continue to use the historical
# required-field validation.
_PHASE0_ROSTER_SOURCE = "data/zoofan_phase0_zoo_list_v0_1.md"
_PHASE0_ROSTER_VERSION = "Phase0 v0.1"
_PHASE0_ROSTER_SHA256 = "6c470233559657cc39e91c50ba5c12786c33af05bbe13c95c91b317ff76d63dd"
_PHASE0_ROSTER_AUTHORITY = "user_designated_authoritative_membership_list"
_PHASE0_ROSTER_COUNT = 73
_PHASE0_GAP_SOURCE_LINES = {
    "mini-zoo-venray": 82,
    "kleindierwandelpark-hedel": 87,
    "bird-park-de-lorkeershoeve": 89,
    "ten-kates-dierenpark": 90,
    "dierenpark-de-bontekoe": 91,
    "mini-animal-park-deventer": 92,
}
_PHASE0_NO_SITE_IDS = frozenset(
    {
        "mini-zoo-venray",
        "kleindierwandelpark-hedel",
        "dierenpark-de-bontekoe",
        "mini-animal-park-deventer",
    }
)
_PHASE0_BLANK_CITY_IDS = frozenset(
    {
        "bird-park-de-lorkeershoeve",
        "ten-kates-dierenpark",
        "dierenpark-de-bontekoe",
    }
)
_PHASE0_UNKNOWN_LANGUAGE_IDS = frozenset({"mini-zoo-venray"})
_PHASE0_GROUP_COMBINATIONS = frozenset(
    {
        ("core30", "germany_top10"),
        ("core30", "europe_top10"),
        ("core30", "global_top10"),
        ("regional_lab", "nrw"),
        ("regional_lab", "dutch_border"),
    }
)


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
    # Article-page selectors live in the source config as well as the archive
    # selectors.  Keeping them here makes ``ArchiveParserConfig`` a complete
    # representation of a source's parser settings for callers that construct
    # it directly, while the article parser also accepts the raw mapping.
    article_content_selector: Optional[str] = None
    article_content_exclude_selector: Optional[Union[str, List[str]]] = None

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
            article_content_selector=value.get("article_content_selector"),
            article_content_exclude_selector=value.get("article_content_exclude_selector"),
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
    # Optional injected :class:`zoofan.adapters.AdapterRegistry`.  Keeping it
    # as an opaque value avoids importing networking or persistence code while
    # allowing a Crawler/DiscoveryEngine to receive deterministic custom
    # adapters from the application.
    adapter_registry: Any = None


@dataclass(frozen=True)
class ConfigValidationIssue:
    """One deterministic, machine-readable configuration validation error."""

    path: str
    message: str
    code: str = "invalid"

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message, "code": self.code}

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message

    def __contains__(self, value: object) -> bool:
        """Allow convenient substring checks in CLI/tests while retaining structure."""

        return str(value) in str(self)


@dataclass
class ConfigValidationResult:
    """The aggregate result returned by :func:`validate_config`.

    Validation deliberately accumulates errors instead of stopping at the
    first malformed field.  This makes the result useful to a future CLI while
    still allowing ``load_config`` to retain its historical ``ValueError``
    contract through :class:`ConfigValidationError`.
    """

    errors: list[ConfigValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def ok(self) -> bool:
        """Short alias useful to callers that treat validation as a check."""

        return self.valid

    @property
    def issues(self) -> list[ConfigValidationIssue]:
        """Alias retained for callers that call all findings ``issues``."""

        return self.errors

    @property
    def error_messages(self) -> list[str]:
        return [str(issue) for issue in self.errors]

    def as_dict(self) -> dict[str, Any]:
        return {"valid": self.valid, "errors": [issue.as_dict() for issue in self.errors]}

    def __bool__(self) -> bool:
        return self.valid

    def __len__(self) -> int:
        return len(self.errors)

    def __iter__(self):
        return iter(self.errors)

    def __getitem__(self, index: Any) -> Any:
        if index == "errors":
            return self.errors
        if index == "valid":
            return self.valid
        return self.errors[index]

    to_dict = as_dict

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ConfigValidationError(self.errors, result=self)


class ConfigValidationError(ValueError):
    """A ``ValueError`` carrying all configuration errors."""

    def __init__(
        self,
        errors: Iterable[ConfigValidationIssue],
        *,
        result: Optional[ConfigValidationResult] = None,
    ) -> None:
        self.errors = list(errors)
        self.result = result or ConfigValidationResult(list(self.errors))
        # Keep the old, useful field-level wording in the exception string;
        # callers matching e.g. ``"kind is unsupported"`` continue to work.
        super().__init__("; ".join(str(issue) for issue in self.errors))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _config_records(value: Any, *, key_field: str) -> tuple[list[Mapping[str, Any]], list[ConfigValidationIssue]]:
    """Normalize list- and mapping-style YAML collections for validation."""

    if value is None:
        return [], []
    if isinstance(value, list):
        return [item if isinstance(item, Mapping) else {} for item in value], [
            ConfigValidationIssue(
                f"{key_field}[{index}]",
                "must be a mapping",
                "type",
            )
            for index, item in enumerate(value)
            if not isinstance(item, Mapping)
        ]
    if isinstance(value, Mapping):
        records: list[Mapping[str, Any]] = []
        issues: list[ConfigValidationIssue] = []
        for key, item in value.items():
            if isinstance(item, Mapping):
                record = dict(item)
                # Mapping style is a convenient legacy form where the key is
                # the stable identity.  An explicit field always wins.  Both
                # zoo and source registries historically accepted this shape,
                # so fill the display name as well as the identity.
                record.setdefault("id", str(key))
                record.setdefault("name", str(key))
                if "sources" not in key_field:
                    record.setdefault("slug", str(key))
                records.append(record)
            else:
                record = {"id": str(key), "name": str(key)}
                if "sources" not in key_field:
                    record["slug"] = str(key)
                records.append(record)
                issues.append(
                    ConfigValidationIssue(
                        f"{key_field}[{key}]",
                        "must be a mapping",
                        "type",
                    )
                )
        return records, issues
    return [], [ConfigValidationIssue(key_field, "must be a list or mapping", "type")]


def _object_mapping(value: Any) -> dict[str, Any]:
    """Return dataclass fields without assuming optional future fields exist."""

    try:
        return {item.name: getattr(value, item.name) for item in dataclass_fields(value)}
    except (TypeError, AttributeError):
        return {}


def _loaded_config_mapping(config: CrawlerConfig) -> dict[str, Any]:
    """Turn a loaded config back into validator input.

    This makes ``validate_config(load_config(...))`` useful without exposing
    persistence or network behavior and keeps the API friendly to integrations
    that construct records directly.
    """

    zoo_records = []
    for zoo in config.zoos:
        record = _object_mapping(zoo)
        # ``Zoo`` tracks whether registry provenance was explicitly supplied;
        # omit the compatibility default so strict validation can still flag
        # a programmatically constructed roster with no provenance.
        if not bool(getattr(zoo, "_list_provenance_provided", True)):
            record.pop("list_provenance", None)
        zoo_records.append(record)
    source_records = [_object_mapping(source) for source in config.sources]
    zoo_by_ref: dict[str, dict[str, Any]] = {}
    for zoo_record in zoo_records:
        zoo_record.setdefault("sources", [])
        for key in (zoo_record.get("id"), zoo_record.get("slug")):
            if key is not None and str(key).strip():
                zoo_by_ref[str(key)] = zoo_record
    # Preserve source records that cannot be attached to a configured zoo.
    # They must be reported as orphaned rather than silently disappearing from
    # validation (and, consequently, from a future CLI diagnostic).
    isolated: list[dict[str, Any]] = []
    for source in source_records:
        matched_zoo = zoo_by_ref.get(str(source.get("zoo_id", "")).strip())
        if matched_zoo is None:
            isolated.append(source)
        else:
            matched_zoo["sources"].append(source)
    return {"zoos": zoo_records, "sources": isolated}


def _read_config_value(path: Any) -> Mapping[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency declaration path
        raise RuntimeError("PyYAML is required to load crawler configuration") from exc

    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return data if isinstance(data, Mapping) else data


def _url_parts(value: Any) -> Optional[tuple[str, str]]:
    """Return ``(normalized_url, hostname)`` for a safe absolute HTTP URL."""

    if not isinstance(value, str) or not value.strip():
        return None
    if value != value.strip():
        return None
    raw = value
    try:
        parsed = urlsplit(raw)
        # Accessing ``port`` validates malformed and out-of-range ports.
        parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or not _HOST_PATTERN.fullmatch(host)
        ):
            return None
        normalized = normalize_url(raw)
    except (TypeError, ValueError):
        return None
    return (normalized, host) if normalized else None


def _host_value(value: Any) -> Optional[str]:
    """Normalize a configured official host or host alias."""

    if not isinstance(value, str) or not value.strip():
        return None
    if value != value.strip():
        return None
    raw = value
    try:
        parsed = urlsplit(raw if "://" in raw else "//" + raw)
        parsed.port
        host = (parsed.hostname or "").lower().rstrip(".")
        # Host settings are host-only.  A path, query, credentials or a
        # wildcard would make the official-domain boundary ambiguous.
        if (
            not host
            or parsed.scheme
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not _HOST_PATTERN.fullmatch(host)
        ):
            return None
    except (TypeError, ValueError):
        return None
    return host


def _host_values(
    item: Mapping[str, Any],
    *,
    path: str,
    issues: list[ConfigValidationIssue],
) -> list[str]:
    """Collect official host declarations while preserving boundary checks."""

    values: list[str] = []

    def add(value: Any, value_path: str) -> None:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = list(value)
        elif value is None:
            return
        else:
            issues.append(ConfigValidationIssue(value_path, "must be a host or list of hosts", "host"))
            return
        for index, candidate in enumerate(candidates):
            host = _host_value(candidate)
            if host is None:
                issues.append(
                    ConfigValidationIssue(
                        f"{value_path}[{index}]" if len(candidates) > 1 else value_path,
                        "must be a valid hostname or explicit host alias",
                        "host",
                    )
                )
                continue
            if host not in values:
                values.append(host)

    for key in (
        "official_host",
        "host",
        "official_hosts",
        "allowed_hosts",
        "allowed_domains",
        "host_aliases",
        "official_host_aliases",
    ):
        if key in item:
            add(item.get(key), f"{path}.{key}")
    return values


def _validate_code(
    value: Any,
    *,
    path: str,
    label: str,
    pattern: re.Pattern[str],
    supported: frozenset[str],
    issues: list[ConfigValidationIssue],
) -> None:
    if not isinstance(value, str) or not value.strip():
        issues.append(ConfigValidationIssue(path, f"{label} is required", "required"))
        return
    candidate = value.strip()
    if not pattern.fullmatch(candidate) or candidate not in supported:
        issues.append(
            ConfigValidationIssue(
                path,
                f"{label} is unsupported or malformed: {candidate!r}",
                "value",
            )
        )


def _validate_id(value: Any, *, path: str, kind: str, issues: list[ConfigValidationIssue]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        # Pre-schema control/config integrations historically identified a zoo
        # by its slug only.  Keep that legacy form loadable; ``_construct_zoo``
        # materializes the slug as the stable id.  Sources remain strict since
        # their ids are the storage primary key and have no equivalent legacy
        # key.
        if kind == "zoo" and value is None:
            return None
        issues.append(ConfigValidationIssue(path, f"{kind} id is required", "required"))
        return None
    if value != value.strip():
        issues.append(
            ConfigValidationIssue(
                path,
                f"{kind} id must not contain surrounding whitespace",
                "identifier",
            )
        )
        return None
    candidate = value
    if not _ID_PATTERN.fullmatch(candidate):
        issues.append(
            ConfigValidationIssue(
                path,
                f"{kind} id must be a stable URL-safe lowercase identifier",
                "identifier",
            )
        )
        return None
    return candidate


def _validate_groups(value: Any, *, path: str, issues: list[ConfigValidationIssue]) -> None:
    if value is None:
        # Older hand-written configurations did not carry groups.  The
        # loader supplies an empty list for those records; an explicitly null
        # value, however, is ambiguous and should be fixed by the author.
        if path.rsplit(".", 1)[-1] in {"groups"}:
            issues.append(ConfigValidationIssue(path, "must be a list of group names", "groups"))
        return
    if not isinstance(value, (list, tuple)):
        issues.append(ConfigValidationIssue(path, "must be a list of group names", "groups"))
        return
    seen: set[str] = set()
    for index, group in enumerate(value):
        if not isinstance(group, str) or not group.strip():
            issues.append(ConfigValidationIssue(f"{path}[{index}]", "must be a non-empty string", "groups"))
            continue
        normalized = group.strip()
        if normalized in seen:
            issues.append(ConfigValidationIssue(f"{path}[{index}]", "must not contain duplicates", "groups"))
        seen.add(normalized)


def _phase0_provenance_entry(item: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Return the sole structured Phase 0 provenance entry, if present."""

    provenance = item.get("list_provenance")
    if not isinstance(provenance, (list, tuple)) or len(provenance) != 1:
        return None
    entry = provenance[0]
    return entry if isinstance(entry, Mapping) else None


def _validate_phase0_provenance(
    item: Mapping[str, Any],
    *,
    context: str,
    zoo_id: str,
    issues: list[ConfigValidationIssue],
) -> bool:
    """Validate the closed local provenance required by Phase 0 gaps.

    This is deliberately narrower than the general provenance compatibility
    rules.  Existing registry fixtures may continue using a simple string
    provenance, while a null-field exception can only be authorized by the
    exact local roster snapshot and row identity.
    """

    provenance_path = f"{context}.list_provenance"
    provenance = item.get("list_provenance")
    valid = True
    if not isinstance(provenance, (list, tuple)) or len(provenance) != 1:
        issues.append(
            ConfigValidationIssue(
                provenance_path,
                "must contain exactly one local Phase 0 mapping for a null-field exception",
                "provenance",
            )
        )
        return False
    entry = provenance[0]
    if not isinstance(entry, Mapping):
        issues.append(
            ConfigValidationIssue(
                f"{provenance_path}[0]",
                "must be a local Phase 0 mapping for a null-field exception",
                "provenance",
            )
        )
        return False

    expected_values = {
        "source": _PHASE0_ROSTER_SOURCE,
        "version": _PHASE0_ROSTER_VERSION,
        "sha256": _PHASE0_ROSTER_SHA256,
        "authority": _PHASE0_ROSTER_AUTHORITY,
        "expanded_count": _PHASE0_ROSTER_COUNT,
    }
    for key, expected in expected_values.items():
        if entry.get(key) != expected:
            issues.append(
                ConfigValidationIssue(
                    f"{provenance_path}[0].{key}",
                    f"must equal the authoritative Phase 0 value {expected!r}",
                    "provenance",
                )
            )
            valid = False

    source_line = entry.get("source_line")
    expected_line = _PHASE0_GAP_SOURCE_LINES.get(zoo_id)
    if type(source_line) is not int or source_line <= 0:
        issues.append(
            ConfigValidationIssue(
                f"{provenance_path}[0].source_line",
                "must be a positive integer from the authoritative Phase 0 roster",
                "provenance",
            )
        )
        valid = False
    elif expected_line is not None and source_line != expected_line:
        issues.append(
            ConfigValidationIssue(
                f"{provenance_path}[0].source_line",
                f"must equal the authoritative Phase 0 line {expected_line}",
                "provenance",
            )
        )
        valid = False
    return valid


def _phase0_provenance_candidate(entry: Optional[Mapping[str, Any]]) -> bool:
    """Whether an entry claims the fixed Phase 0 snapshot strongly enough
    to receive its closed group-combination checks.
    """

    return bool(
        entry
        and entry.get("source") == _PHASE0_ROSTER_SOURCE
        and entry.get("version") == _PHASE0_ROSTER_VERSION
        and entry.get("sha256") == _PHASE0_ROSTER_SHA256
        and entry.get("expanded_count") == _PHASE0_ROSTER_COUNT
    )


def _phase0_import_gate(metadata: Mapping[str, Any]) -> tuple[Mapping[str, Any], set[str]]:
    gate = metadata.get("import_gate")
    if not isinstance(gate, Mapping):
        return {}, set()
    blockers_value = gate.get("blockers", [])
    blockers = {
        value.strip()
        for value in blockers_value
        if isinstance(value, str) and value.strip()
    } if isinstance(blockers_value, (list, tuple, set)) else set()
    return gate, blockers


def _validate_patterns(
    config: Mapping[str, Any],
    *,
    path: str,
    issues: list[ConfigValidationIssue],
) -> None:
    """Compile configured regexes without changing parser/runtime behavior."""

    for key in (
        "allow_regex",
        "allow_patterns",
        "article_allow",
        "exclude_regex",
        "exclude_patterns",
        "navigation_patterns",
        "navigation_exclude",
    ):
        if key not in config or config.get(key) is None:
            continue
        value = config.get(key)
        values = [value] if isinstance(value, str) else list(value) if isinstance(value, (list, tuple)) else None
        if values is None:
            issues.append(ConfigValidationIssue(f"{path}.{key}", "must be a regex or list of regexes", "regex"))
            continue
        for index, pattern in enumerate(values):
            pattern_path = f"{path}.{key}[{index}]" if len(values) > 1 else f"{path}.{key}"
            if not isinstance(pattern, str) or not pattern:
                issues.append(ConfigValidationIssue(pattern_path, "must be a non-empty regex string", "regex"))
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(ConfigValidationIssue(pattern_path, f"invalid regex: {exc}", "regex"))

    if "article_date_pattern" in config and config.get("article_date_pattern") is not None:
        article_date_pattern = config.get("article_date_pattern")
        if not isinstance(article_date_pattern, str) or not article_date_pattern:
            issues.append(ConfigValidationIssue(f"{path}.article_date_pattern", "must be a non-empty regex string", "regex"))
        else:
            try:
                re.compile(article_date_pattern, re.IGNORECASE)
            except re.error as exc:
                issues.append(ConfigValidationIssue(f"{path}.article_date_pattern", f"invalid regex: {exc}", "regex"))


def _validate_archive_config(
    config: Mapping[str, Any],
    *,
    path: str,
    issues: list[ConfigValidationIssue],
) -> None:
    """Validate CSS selector shape and archive-specific bounds."""

    selectors: dict[str, Any] = {}
    nested = config.get("selectors")
    if nested is not None:
        if not isinstance(nested, Mapping):
            issues.append(ConfigValidationIssue(f"{path}.selectors", "must be a mapping", "selector"))
        else:
            selectors.update(nested)
    for key, alias in (
        ("article_selector", "article"),
        ("link_selector", "link"),
        ("title_selector", "title"),
        ("date_selector", "date"),
        ("pagination_selector", "pagination"),
    ):
        if key in config:
            selectors[key] = config.get(key)
        elif alias in selectors:
            selectors[key] = selectors[alias]

    for key, value in selectors.items():
        if key not in {
            "article_selector",
            "link_selector",
            "title_selector",
            "date_selector",
            "pagination_selector",
            "article",
            "link",
            "title",
            "date",
            "pagination",
        }:
            continue
        selector_path = f"{path}.{key}"
        if value is None:
            if key in {"article_selector", "link_selector", "article", "link"}:
                issues.append(ConfigValidationIssue(selector_path, "must be a non-empty CSS selector", "selector"))
            continue
        if not isinstance(value, str) or not value.strip():
            issues.append(ConfigValidationIssue(selector_path, "must be a non-empty CSS selector", "selector"))
            continue
        try:
            import soupsieve
        except ImportError:  # pragma: no cover - dependency declaration path
            continue
        try:
            soupsieve.compile(value)
        except Exception as exc:
            issues.append(ConfigValidationIssue(selector_path, f"invalid CSS selector: {exc}", "selector"))

    if "max_pages" in config or "pages" in config:
        value = config.get("max_pages", config.get("pages"))
        if isinstance(value, bool):
            valid_pages = False
        else:
            try:
                valid_pages = int(value) == float(value) and int(value) > 0 and int(value) <= 100
            except (TypeError, ValueError, OverflowError):
                valid_pages = False
        if not valid_pages:
            issues.append(ConfigValidationIssue(f"{path}.max_pages", "must be an integer between 1 and 100", "bounds"))


def _validate_json_path(value: Any, *, path: str, issues: list[ConfigValidationIssue]) -> None:
    """Validate a data-only JSON path used by the generic API adapter."""

    if isinstance(value, Mapping):
        value = value.get("path", value.get("field"))
    if isinstance(value, (list, tuple)):
        parts = list(value)
        if not parts or len(parts) > 16 or any(not isinstance(part, (str, int)) or not str(part).strip() for part in parts):
            issues.append(ConfigValidationIssue(path, "must be a non-empty JSON field path", "path"))
        return
    if not isinstance(value, str) or not value.strip():
        issues.append(ConfigValidationIssue(path, "must be a non-empty JSON field path", "path"))
        return
    candidate = value.strip()
    if len(candidate) > 512 or "\x00" in candidate:
        issues.append(ConfigValidationIssue(path, "must be a bounded JSON field path", "path"))
        return
    # Dotted, JSON-pointer, and simple bracket-index forms are supported.  No
    # wildcards or expression syntax are accepted.
    if any(token in candidate for token in ("*", "(", ")", "\\")):
        issues.append(ConfigValidationIssue(path, "must not contain wildcards or expressions", "path"))
        return
    if (
        candidate.startswith(".")
        or candidate.endswith((".", "[", "]"))
        or ".." in candidate
        or ".[" in candidate
    ):
        issues.append(ConfigValidationIssue(path, "must contain valid non-empty path segments", "path"))
        return
    parts = [part for part in re.split(r"\.|\[|\]", candidate.lstrip("/")) if part]
    if not parts or len(parts) > 16 or any(part.strip() == "" for part in parts):
        issues.append(ConfigValidationIssue(path, "must contain bounded path segments", "path"))


def _validate_json_api_config(
    config: Mapping[str, Any],
    *,
    path: str,
    issues: list[ConfigValidationIssue],
) -> None:
    """Validate generic JSON API selectors without evaluating user input."""

    for key in ("items_path", "item_path", "results_path", "articles_path", "news_path", "data_path"):
        if key in config and config.get(key) is not None:
            _validate_json_path(config.get(key), path=f"{path}.{key}", issues=issues)
    for container_key in ("paths", "json_paths"):
        container = config.get(container_key)
        if isinstance(container, Mapping):
            items_path = container.get("items", container.get("items_path"))
            if items_path is not None:
                _validate_json_path(items_path, path=f"{path}.{container_key}.items", issues=issues)

    for container_key in ("fields", "paths", "field_paths", "json_paths", "selectors"):
        if container_key not in config or config.get(container_key) is None:
            continue
        container = config.get(container_key)
        if not isinstance(container, Mapping):
            issues.append(ConfigValidationIssue(f"{path}.{container_key}", "must be a mapping of field paths", "path"))
            continue
        for field_name, field_path in container.items():
            if not isinstance(field_name, str) or not field_name.strip():
                issues.append(ConfigValidationIssue(f"{path}.{container_key}", "field names must be non-empty strings", "path"))
                continue
            _validate_json_path(field_path, path=f"{path}.{container_key}.{field_name}", issues=issues)

    for key in (
        "url_path", "title_path", "date_path", "published_at_path", "updated_at_path",
        "summary_path", "author_path", "canonical_url_path",
    ):
        if key in config and config.get(key) is not None:
            _validate_json_path(config.get(key), path=f"{path}.{key}", issues=issues)
    if "max_items" in config or "limit" in config:
        value = config.get("max_items", config.get("limit"))
        try:
            valid = not isinstance(value, bool) and int(value) == float(value) and 1 <= int(value) <= 1000
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            issues.append(ConfigValidationIssue(f"{path}.max_items", "must be an integer between 1 and 1000", "bounds"))


def _validate_css_selector(
    config: Mapping[str, Any],
    *,
    key: str,
    path: str,
    issues: list[ConfigValidationIssue],
    allow_list: bool = False,
) -> None:
    """Validate a parser CSS selector used outside archive card selectors."""

    if key not in config:
        return
    value = config.get(key)
    selector_path = f"{path}.{key}"
    if allow_list and isinstance(value, (list, tuple)):
        values = list(value)
        if not values:
            issues.append(ConfigValidationIssue(selector_path, "must contain at least one CSS selector", "selector"))
            return
    else:
        values = [value]
    for index, selector in enumerate(values):
        value_path = (
            f"{selector_path}[{index}]"
            if allow_list and isinstance(value, (list, tuple))
            else selector_path
        )
        if not isinstance(selector, str) or not selector.strip():
            issues.append(ConfigValidationIssue(value_path, "must be a non-empty CSS selector", "selector"))
            continue
        try:
            import soupsieve
            soupsieve.compile(selector)
        except ImportError:  # pragma: no cover - dependency declaration path
            continue
        except Exception as exc:
            issues.append(ConfigValidationIssue(value_path, f"invalid CSS selector: {exc}", "selector"))


def _source_config(item: Mapping[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    config = dict(item.get("config", {})) if isinstance(item.get("config"), Mapping) else {}
    options = item.get("adapter_options")
    if isinstance(options, Mapping):
        # Adapter-specific options are data-only and are merged into the
        # source config so the same parser can be driven from either the
        # concise ``config`` form or the documented ``adapter_options`` form.
        for key, value in options.items():
            config.setdefault(key, value)
    adapter = item.get("adapter")
    if adapter is None:
        adapter = config.get("adapter")
    # ``custom_adapter``/``adapter_name`` are explicit data-only ways to name
    # an injected implementation.  They are not import paths.
    custom_name = item.get("custom_adapter")
    if custom_name is None:
        custom_name = config.get("custom_adapter", config.get("adapter_name"))
    if adapter is None and custom_name is not None:
        adapter = custom_name
    elif (
        isinstance(adapter, str)
        and canonical_adapter_kind(adapter) == "custom_adapter"
        and custom_name is not None
    ):
        # ``adapter: custom_adapter`` is a taxonomy marker; use the explicit
        # injected name when both spellings are present.
        adapter = custom_name
    # Selector aliases are supported for small config files, matching the
    # long-standing ArchiveParserConfig behavior.
    selectors = config.get("selectors")
    if isinstance(selectors, Mapping):
        for key, alias in (
            ("article_selector", "article"),
            ("link_selector", "link"),
            ("title_selector", "title"),
            ("date_selector", "date"),
            ("pagination_selector", "pagination"),
            ("article_content_selector", "article_content"),
            ("article_content_exclude_selector", "article_content_exclude"),
        ):
            if key not in config and alias in selectors:
                config[key] = selectors[alias]
            elif key not in config and key in selectors:
                config[key] = selectors[key]
    for selector_key in (
        "article_selector",
        "link_selector",
        "title_selector",
        "date_selector",
        "pagination_selector",
        "article_content_selector",
        "article_content_exclude_selector",
        "max_pages",
    ):
        if selector_key in item:
            config[selector_key] = item[selector_key]
    if adapter is not None:
        config.setdefault("adapter", adapter)
    if custom_name is not None:
        config.setdefault("custom_adapter", custom_name)
    return config, str(adapter).strip() if isinstance(adapter, str) else None


def _stable_source_id(zoo_id: Optional[str], source_item: Mapping[str, Any]) -> str:
    """Derive a deterministic source id for legacy entries without ``id``."""

    raw_url = source_item.get("url")
    normalized = normalize_url(raw_url) if isinstance(raw_url, str) else ""
    basis = normalized or str(source_item.get("name") or source_item.get("kind") or "source")
    owner = str(zoo_id or source_item.get("zoo_id") or source_item.get("zoo") or "zoo")
    digest = hashlib.sha256(f"{owner}\0{basis}".encode("utf-8")).hexdigest()[:16]
    return f"source-{digest}"


def _validate_raw_config(root: Any, *, strict: bool = False) -> ConfigValidationResult:
    issues: list[ConfigValidationIssue] = []
    if not isinstance(root, Mapping):
        issues.append(ConfigValidationIssue("config", "root must be a mapping", "type"))
        return ConfigValidationResult(issues)

    raw_zoos, collection_issues = _config_records(root.get("zoos", []), key_field="zoos")
    issues.extend(collection_issues)
    zoo_ids: dict[str, str] = {}
    zoo_slugs: dict[str, str] = {}
    zoo_records: list[tuple[Mapping[str, Any], str, str, str, list[str]]] = []
    official_urls: dict[str, str] = {}
    phase0_no_site_record_ids: set[str] = set()

    for index, item in enumerate(raw_zoos):
        identifier = item.get("id", item.get("slug", item.get("name", index)))
        context = f"zoo[{identifier}]"
        zoo_id = _validate_id(item.get("id"), path=f"{context}.id", kind="zoo", issues=issues)
        if strict and (not isinstance(item.get("id"), str) or not item.get("id", "").strip()):
            issues.append(ConfigValidationIssue(f"{context}.id", "must be explicitly configured in strict mode", "required"))
        if zoo_id is not None:
            if zoo_id in zoo_ids:
                issues.append(ConfigValidationIssue(f"{context}.id", f"duplicates {zoo_ids[zoo_id]}", "duplicate"))
            else:
                zoo_ids[zoo_id] = context
        raw_slug = item.get("slug", item.get("key", item.get("id", item.get("name", zoo_id or ""))))
        slug = str(raw_slug).strip() if isinstance(raw_slug, str) else ""
        if not slug or not _ID_PATTERN.fullmatch(slug):
            issues.append(ConfigValidationIssue(f"{context}.slug", "must be a stable URL-safe lowercase identifier", "identifier"))
        elif slug in zoo_slugs:
            issues.append(ConfigValidationIssue(f"{context}.slug", f"duplicates {zoo_slugs[slug]}", "duplicate"))
        else:
            zoo_slugs[slug] = context

        record_id = str(zoo_id or slug)
        metadata = item.get("metadata", {})
        if metadata is not None and not isinstance(metadata, Mapping):
            issues.append(ConfigValidationIssue(f"{context}.metadata", "must be a mapping", "type"))
            metadata = {}
        metadata = metadata if isinstance(metadata, Mapping) else {}
        import_gate, import_blockers = _phase0_import_gate(metadata)
        nested_sources_value = item.get("sources", [])
        nested_sources_empty = isinstance(nested_sources_value, list) and not nested_sources_value
        try:
            enabled_for_policy = parse_bool(item.get("enabled"), True)
        except ValueError:
            enabled_for_policy = False

        official_raw = item.get("official_url", item.get("website_url", item.get("url", item.get("base_url"))))
        official = _url_parts(official_raw)
        official_host = official[1] if official else ""
        url_keys = ("official_url", "website_url", "url", "base_url")
        official_missing = all(key not in item or item.get(key) is None for key in url_keys)
        source_status = item.get("source_status")
        source_status_normalized = source_status.strip().lower() if isinstance(source_status, str) else None
        no_site_id = record_id in _PHASE0_NO_SITE_IDS
        no_site_signal = no_site_id and (
            official_missing
            or source_status_normalized == "not_found"
            or "no_official_website_verified" in metadata
            or "no_executable_source" in metadata
        )
        if no_site_signal:
            phase0_no_site_record_ids.add(record_id)
        phase0_entry = _phase0_provenance_entry(item)
        phase0_candidate = _phase0_provenance_candidate(phase0_entry)
        missing_city = "city" in item and (
            item.get("city") is None
            or (isinstance(item.get("city"), str) and not item.get("city", "").strip())
        )
        missing_language = item.get("language") is None
        phase0_gap_shape = no_site_signal or (
            record_id in _PHASE0_BLANK_CITY_IDS and missing_city
        ) or (
            record_id in _PHASE0_UNKNOWN_LANGUAGE_IDS and missing_language
        )
        phase0_provenance_complete = False
        if phase0_gap_shape:
            phase0_provenance_complete = _validate_phase0_provenance(
                item,
                context=context,
                zoo_id=record_id,
                issues=issues,
            )
        if phase0_candidate and (strict or phase0_gap_shape):
            groups = item.get("groups")
            normalized_groups = tuple(groups) if isinstance(groups, (list, tuple)) else ()
            if normalized_groups not in _PHASE0_GROUP_COMBINATIONS:
                issues.append(
                    ConfigValidationIssue(
                        f"{context}.groups",
                        "must use one canonical Phase 0 group combination",
                        "groups",
                    )
                )

        no_site_markers = (
            metadata.get("no_official_website_verified") is not None
            or metadata.get("no_executable_source") is not None
        )
        no_site_authorized = (
            no_site_id
            and official_missing
            and source_status_normalized == "not_found"
            and enabled_for_policy is True
            and nested_sources_empty
            and metadata.get("no_official_website_verified") is True
            and metadata.get("no_executable_source") is True
            and "missing_official_website_url" in import_blockers
            and phase0_provenance_complete
        )
        if phase0_gap_shape and enabled_for_policy is not True:
            issues.append(
                ConfigValidationIssue(
                    f"{context}.enabled",
                    "must be true for an operational Phase 0 gap row",
                    "value",
                )
            )
        if official is None and not no_site_authorized:
            issues.append(ConfigValidationIssue(f"{context}.official_url", "must be a valid absolute http(s) URL", "url"))
        elif official is not None:
            if no_site_signal or no_site_markers:
                issues.append(
                    ConfigValidationIssue(
                        f"{context}.official_url",
                        "must remain null for an evidence-backed Phase 0 no-site row",
                        "gap",
                    )
                )
            elif official[0] in official_urls:
                issues.append(
                    ConfigValidationIssue(
                        f"{context}.official_url",
                        f"duplicates normalized URL declared by {official_urls[official[0]]}",
                        "duplicate",
                    )
                )
            else:
                official_urls[official[0]] = context

        _validate_code(
            item.get("country_code"),
            path=f"{context}.country_code",
            label="country_code",
            pattern=_COUNTRY_CODE,
            supported=SUPPORTED_COUNTRY_CODES,
            issues=issues,
        )
        language_missing_authorized = (
            record_id in _PHASE0_UNKNOWN_LANGUAGE_IDS
            and missing_language
            and no_site_authorized
            and "missing_language" in import_blockers
        )
        if not language_missing_authorized:
            _validate_code(
                item.get("language"),
                path=f"{context}.language",
                label="language",
                pattern=_PRIMARY_LANGUAGE,
                supported=SUPPORTED_LANGUAGES,
                issues=issues,
            )

        try:
            enabled = parse_bool(item.get("enabled"), True)
        except ValueError:
            enabled = True
            issues.append(ConfigValidationIssue(f"{context}.enabled", "expected a boolean value", "value"))
        if enabled and (not isinstance(source_status, str) or not source_status.strip()) and strict:
            issues.append(ConfigValidationIssue(f"{context}.source_status", "is required for an enabled zoo", "required"))
        elif source_status is not None and (not isinstance(source_status, str) or not source_status.strip()):
            issues.append(ConfigValidationIssue(f"{context}.source_status", "must be a non-empty string", "value"))
        elif isinstance(source_status, str) and source_status.strip().lower() not in SOURCE_STATUS_VOCABULARY:
            # Strict mode is the contract for authoritative roster imports;
            # legacy non-strict loads may retain an older descriptive value.
            if strict:
                issues.append(
                    ConfigValidationIssue(
                        f"{context}.source_status",
                        f"is unsupported: {source_status!r}",
                        "status",
                    )
                )

        if "groups" in item:
            _validate_groups(item.get("groups"), path=f"{context}.groups", issues=issues)
        for key in ("region", "city"):
            field_missing = not isinstance(item.get(key), str) or not str(item.get(key)).strip()
            city_missing_authorized = (
                record_id in _PHASE0_BLANK_CITY_IDS
                and item.get("city") is None
                and phase0_provenance_complete
                and isinstance(import_gate, Mapping)
                and import_gate.get("roster_city_is_intentionally_blank") is not False
                and (
                    import_gate.get("roster_city_is_intentionally_blank") is True
                    or "city_null_requires_validator_support" in import_blockers
                )
            )
            if key in item and field_missing and not (key == "city" and city_missing_authorized):
                issues.append(ConfigValidationIssue(f"{context}.{key}", "must be a non-empty string", "value"))
        if "list_provenance" in item:
            provenance = item.get("list_provenance")
            if isinstance(provenance, str):
                if not provenance.strip():
                    issues.append(ConfigValidationIssue(f"{context}.list_provenance", "must not be empty", "provenance"))
            elif isinstance(provenance, Mapping):
                if not provenance:
                    issues.append(ConfigValidationIssue(f"{context}.list_provenance", "must not be empty", "provenance"))
                for key, value in provenance.items():
                    if not isinstance(key, str) or not key.strip() or not isinstance(value, (str, int, float, bool, type(None))):
                        issues.append(ConfigValidationIssue(f"{context}.list_provenance", "must contain simple named values", "provenance"))
                        break
            elif isinstance(provenance, (list, tuple)):
                if not provenance:
                    issues.append(ConfigValidationIssue(f"{context}.list_provenance", "must not be empty", "provenance"))
                for index, entry in enumerate(provenance):
                    if isinstance(entry, Mapping):
                        if not entry or any(
                            not isinstance(key, str)
                            or not key.strip()
                            or not isinstance(value, (str, int, float, bool, type(None)))
                            for key, value in entry.items()
                        ):
                            issues.append(ConfigValidationIssue(f"{context}.list_provenance[{index}]", "must contain simple named values", "provenance"))
                    elif not isinstance(entry, str) or not entry.strip():
                        issues.append(ConfigValidationIssue(f"{context}.list_provenance[{index}]", "must be a non-empty provenance entry", "provenance"))
            else:
                issues.append(ConfigValidationIssue(f"{context}.list_provenance", "must be a mapping, list, or non-empty string", "provenance"))
        elif strict and enabled:
            issues.append(
                ConfigValidationIssue(
                    f"{context}.list_provenance",
                    "is required for an enabled zoo in strict mode",
                    "required",
                )
            )

        zoo_hosts = [official_host] if official_host else []
        zoo_hosts.extend(_host_values(metadata or {}, path=f"{context}.metadata", issues=issues))
        # Keep this tuple even when a zoo id was malformed so source errors can
        # still be reported with their precise path.
        nested_sources = item.get("sources", [])
        if isinstance(nested_sources, Mapping):
            nested_sources, nested_issues = _config_records(nested_sources, key_field=f"{context}.sources")
            issues.extend(nested_issues)
        elif not isinstance(nested_sources, list):
            issues.append(ConfigValidationIssue(f"{context}.sources", "must be a list or mapping", "type"))
            nested_sources = []
        zoo_records.append((item, zoo_id or slug, slug, context, zoo_hosts))
        # Attach normalized host through a local marker rather than mutating
        # caller mappings.  This is only an in-memory validation convenience.
        for source_item in nested_sources:
            # ``source_item`` is processed below after all zoo references exist.
            pass

    # Collect nested and top-level source records.  A nested source inherits
    # its zoo association; a top-level source must declare zoo_id/zoo.
    source_records: list[tuple[Mapping[str, Any], str, Optional[str], list[str]]] = []
    for item, fallback_zoo_id, _slug, context, zoo_hosts in zoo_records:
        nested_sources = item.get("sources", [])
        if isinstance(nested_sources, Mapping):
            nested_sources, _ = _config_records(nested_sources, key_field=f"{context}.sources")
        if not isinstance(nested_sources, list):
            continue
        for index, source_item in enumerate(nested_sources):
            if not isinstance(source_item, Mapping):
                continue
            source_context = f"source[{source_item.get('id', source_item.get('name', index))}]"
            source_records.append((source_item, source_context, fallback_zoo_id, zoo_hosts))

    raw_sources = root.get("sources")
    if raw_sources is not None:
        top_sources, top_issues = _config_records(raw_sources, key_field="sources")
        issues.extend(top_issues)
        for index, source_item in enumerate(top_sources):
            source_context = f"source[{source_item.get('id', source_item.get('name', index))}]"
            source_records.append((source_item, source_context, None, []))

    source_ids: dict[str, str] = {}
    zoo_lookup: dict[str, str] = {}
    for item, fallback_zoo_id, _slug, context, _hosts in zoo_records:
        if item.get("id"):
            zoo_lookup[str(item.get("id"))] = str(item.get("id"))
        if item.get("slug"):
            zoo_lookup[str(item.get("slug"))] = str(item.get("id") or item.get("slug"))

    for source_item, source_context, inherited_zoo_id, inherited_hosts in source_records:
        configured_zoo = source_item.get("zoo_id", source_item.get("zoo"))
        associated_zoo = str(configured_zoo).strip() if configured_zoo is not None else inherited_zoo_id
        raw_source_id = source_item.get("id")
        source_id = _validate_id(raw_source_id, path=f"{source_context}.id", kind="source", issues=issues) if raw_source_id is not None else None
        if strict and (not isinstance(source_item.get("id"), str) or not source_item.get("id", "").strip()):
            issues.append(ConfigValidationIssue(f"{source_context}.id", "must be explicitly configured in strict mode", "required"))
        effective_source_id = source_id or _stable_source_id(associated_zoo, source_item)
        if effective_source_id:
            if effective_source_id in source_ids:
                issues.append(ConfigValidationIssue(f"{source_context}.id", f"duplicates {source_ids[effective_source_id]}", "duplicate"))
            else:
                source_ids[effective_source_id] = source_context

        if not associated_zoo:
            issues.append(ConfigValidationIssue(f"{source_context}.zoo_id", "must reference a configured zoo", "association"))
        elif inherited_zoo_id and configured_zoo is not None and zoo_lookup.get(associated_zoo, associated_zoo) != str(inherited_zoo_id):
            # A nested source's relationship is unambiguous; an explicit,
            # conflicting zoo_id should not silently move it elsewhere.
            issues.append(ConfigValidationIssue(f"{source_context}.zoo_id", "does not match its containing zoo", "association"))
        elif associated_zoo not in zoo_lookup:
            issues.append(ConfigValidationIssue(f"{source_context}.zoo_id", f"unknown configured zoo: {associated_zoo!r}", "association"))
        if associated_zoo in phase0_no_site_record_ids:
            issues.append(
                ConfigValidationIssue(
                    f"{source_context}.url",
                    "no executable source is allowed for an evidence-backed Phase 0 no-site row",
                    "gap",
                )
            )

        _validate_code(
            source_item.get("language"),
            path=f"{source_context}.language",
            label="language",
            pattern=_PRIMARY_LANGUAGE,
            supported=SUPPORTED_LANGUAGES,
            issues=issues,
        )
        if "config" in source_item and source_item.get("config") is not None and not isinstance(source_item.get("config"), Mapping):
            issues.append(ConfigValidationIssue(f"{source_context}.config", "must be a mapping", "type"))
        config, adapter = _source_config(source_item)
        kind_value = source_item.get("kind", source_item.get("type", source_item.get("source_type")))
        if kind_value is None and adapter:
            # An unknown adapter token without a separate kind is an injected
            # custom adapter name, not an arbitrary import path.
            kind_value = canonical_adapter_kind(adapter) or "custom_adapter"
        if not isinstance(kind_value, str) or not kind_value.strip():
            kind = "rss"
            if strict:
                issues.append(ConfigValidationIssue(f"{source_context}.kind", "kind or adapter is required in strict mode", "required"))
        else:
            kind = canonical_adapter_kind(kind_value) or normalize_adapter_token(kind_value)
            if kind not in SUPPORTED_SOURCE_KINDS:
                issues.append(ConfigValidationIssue(f"{source_context}.kind", f"kind is unsupported: {kind_value!r}", "value"))

        custom_name = adapter
        if kind == "custom_adapter":
            # ``adapter: custom_adapter`` is only a taxonomy marker; a real
            # implementation name may be supplied by ``custom_adapter`` or
            # ``adapter_name`` in the source/config mapping.
            marker = {"custom_adapter", "custom", "custom-adapter", "plugin"}
            if custom_name and normalize_adapter_token(custom_name) in marker:
                custom_name = config.get("custom_adapter", config.get("adapter_name"))
            if not isinstance(custom_name, str) or not custom_name.strip():
                issues.append(ConfigValidationIssue(f"{source_context}.adapter", "custom adapter name is required", "required"))
            elif not is_custom_adapter_name(custom_name):
                issues.append(ConfigValidationIssue(f"{source_context}.adapter", "must be a safe custom adapter name", "value"))
            elif canonical_adapter_kind(custom_name) in SUPPORTED_SOURCE_KINDS:
                issues.append(ConfigValidationIssue(f"{source_context}.adapter", "custom adapter name must not be a built-in kind", "value"))
        elif adapter:
            adapter_kind = canonical_adapter_kind(adapter)
            if adapter_kind is None:
                issues.append(ConfigValidationIssue(f"{source_context}.adapter", f"is unsupported: {adapter!r}", "value"))
            elif adapter_kind != kind:
                issues.append(ConfigValidationIssue(f"{source_context}.adapter", f"does not match kind {kind!r}", "value"))
        elif source_item.get("adapter") is not None or config.get("adapter") is not None or config.get("custom_adapter") is not None:
            issues.append(ConfigValidationIssue(f"{source_context}.adapter", "must be a non-empty string", "value"))

        configured_source_status = source_item.get("source_status", source_item.get("status"))
        if configured_source_status is not None:
            if not isinstance(configured_source_status, str) or not configured_source_status.strip():
                issues.append(ConfigValidationIssue(f"{source_context}.source_status", "must be a non-empty string", "status"))
            elif strict and configured_source_status.strip().lower() not in SOURCE_STATUS_VOCABULARY:
                issues.append(ConfigValidationIssue(f"{source_context}.source_status", f"is unsupported: {configured_source_status!r}", "status"))

        source_url = _url_parts(source_item.get("url"))
        if source_url is None:
            # Explicit unsupported roster records may intentionally have no
            # source endpoint yet.  Keep the record for reporting rather than
            # forcing a fake URL or silently disabling the zoo.
            if kind != "unsupported":
                issues.append(ConfigValidationIssue(f"{source_context}.url", "must be a valid absolute http(s) URL", "url"))
        else:
            source_host = source_url[1]
            allowed_hosts = list(inherited_hosts)
            # A source-level official host/alias is explicit authorization for
            # that source and is kept separate from the zoo's website host.
            allowed_hosts.extend(_host_values(config, path=f"{source_context}.config", issues=issues))
            if not allowed_hosts or source_host not in set(allowed_hosts):
                issues.append(
                    ConfigValidationIssue(
                        f"{source_context}.url",
                        f"host {source_host!r} is not the official host or an explicit alias",
                        "host",
                    )
                )

        try:
            parse_bool(source_item.get("enabled"), True)
        except ValueError:
            issues.append(ConfigValidationIssue(f"{source_context}.enabled", "expected a boolean value", "value"))
        if not isinstance(config, Mapping):  # defensive; _source_config always returns a dict
            issues.append(ConfigValidationIssue(f"{source_context}.config", "must be a mapping", "type"))
        if kind == "archive":
            _validate_archive_config(config, path=f"{source_context}.config", issues=issues)
        elif kind == "json_api":
            _validate_json_api_config(config, path=f"{source_context}.config", issues=issues)
        _validate_css_selector(
            config,
            key="article_date_selector",
            path=f"{source_context}.config",
            issues=issues,
        )
        _validate_css_selector(
            config,
            key="article_content_selector",
            path=f"{source_context}.config",
            issues=issues,
        )
        _validate_css_selector(
            config,
            key="article_content_exclude_selector",
            path=f"{source_context}.config",
            issues=issues,
            allow_list=True,
        )
        _validate_patterns(config, path=f"{source_context}.config", issues=issues)

    return ConfigValidationResult(issues)


def validate_config(
    value: Any,
    *,
    strict: bool = False,
    raise_on_error: bool = False,
) -> ConfigValidationResult:
    """Validate a raw YAML mapping, path, or already loaded config.

    The function is intentionally pure with respect to the application: it
    reads a supplied file when given a path, but never contacts a source and
    never opens a database.
    """

    if isinstance(value, CrawlerConfig):
        root: Any = _loaded_config_mapping(value)
    elif isinstance(value, (str, bytes, Path)):
        root = _read_config_value(value)
    else:
        root = value
    result = _validate_raw_config(root, strict=strict)
    if raise_on_error:
        result.raise_for_errors()
    return result


def _model_fields(model: Any) -> set[str]:
    try:
        return {item.name for item in dataclass_fields(model)}
    except TypeError:
        return set()


def _construct_zoo(item: Mapping[str, Any]) -> Zoo:
    official_url = item.get("official_url", item.get("website_url", item.get("url", item.get("base_url"))))
    zoo_fields = _model_fields(Zoo)
    stable_id = item.get("id") or item.get("slug") or item.get("key")
    kwargs: dict[str, Any] = {
        "id": stable_id,
        "slug": str(item.get("slug", item.get("key", item.get("id", item.get("name", ""))))),
        "name": str(item.get("name", item.get("slug", ""))),
        "website_url": official_url,
        "country_code": item.get("country_code"),
        "language": item.get("language"),
        "enabled": parse_bool(item.get("enabled"), True),
        "metadata": dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
    }
    # The storage/model foundation may add these fields while older callers
    # still instantiate Zoo with the original constructor.  Only pass fields
    # supported by the current model so both revisions remain importable.
    if "official_url" in zoo_fields:
        kwargs["official_url"] = official_url
    for key in ("region", "city", "groups", "source_status", "list_provenance"):
        if key in zoo_fields and key in item:
            value = item.get(key)
            if key == "list_provenance" and isinstance(value, Mapping):
                value = [dict(value)]
            kwargs[key] = value
    if "groups" in zoo_fields and "groups" not in kwargs:
        kwargs["groups"] = []
    return Zoo(**{key: value for key, value in kwargs.items() if key in zoo_fields})


def _construct_source(source_item: Mapping[str, Any], zoo_id: str, kind: str, config: Mapping[str, Any]) -> Source:
    source_fields = _model_fields(Source)
    kwargs: dict[str, Any] = {
        "id": source_item.get("id") or _stable_source_id(zoo_id, source_item),
        "zoo_id": zoo_id,
        "url": str(source_item.get("url", "")),
        "kind": kind,
        "name": source_item.get("name"),
        "language": source_item.get("language"),
        "config": dict(config),
        "enabled": parse_bool(source_item.get("enabled"), True),
    }
    adapter = source_item.get("adapter")
    if adapter is None and isinstance(source_item.get("config"), Mapping):
        adapter = source_item["config"].get("adapter")
    if adapter is None:
        adapter = source_item.get("custom_adapter")
    if adapter is None and isinstance(source_item.get("config"), Mapping):
        adapter = source_item["config"].get("custom_adapter", source_item["config"].get("adapter_name"))
    if adapter is None:
        adapter = config.get("adapter", config.get("custom_adapter", config.get("adapter_name")))
    elif canonical_adapter_kind(adapter) == "custom_adapter":
        adapter = config.get("custom_adapter", config.get("adapter_name", adapter))
    if "adapter" in source_fields and adapter is not None:
        kwargs["adapter"] = adapter
    source_status = source_item.get("source_status", source_item.get("status"))
    if "source_status" in source_fields and source_status is not None:
        kwargs["source_status"] = str(source_status).strip().lower()
    return Source(**{key: value for key, value in kwargs.items() if key in source_fields})


def load_config(path: Any) -> CrawlerConfig:
    """Load ``zoos.yaml``-style configuration.

    The loader intentionally only maps data into domain records; it does not
    initialize storage or perform network activity.
    """

    root = _read_config_value(path)
    result = validate_config(root)
    result.raise_for_errors()
    if not isinstance(root, Mapping):  # guarded by validation; keeps typing clear
        raise ConfigValidationError(
            [ConfigValidationIssue("config", "root must be a mapping", "type")]
        )

    raw_zoos, _ = _config_records(root.get("zoos", []), key_field="zoos")
    zoos: list[Zoo] = []
    sources: list[Source] = []
    zoo_by_ref: dict[str, Zoo] = {}
    for item in raw_zoos:
        zoo = _construct_zoo(item)
        zoos.append(zoo)
        if zoo.id:
            zoo_by_ref[str(zoo.id)] = zoo
        if zoo.slug:
            zoo_by_ref[str(zoo.slug)] = zoo

    def add_source(source_item: Mapping[str, Any], inherited_zoo: Optional[Zoo] = None) -> None:
        configured_zoo = source_item.get("zoo_id", source_item.get("zoo"))
        zoo = inherited_zoo or zoo_by_ref.get(str(configured_zoo).strip())
        # Validation guarantees this relationship.  The fallback keeps this
        # helper defensive if it is ever reused by a caller bypassing it.
        if zoo is None:
            return
        config, adapter = _source_config(source_item)
        kind_value = source_item.get("kind", source_item.get("type", source_item.get("source_type")))
        if kind_value is None:
            kind_value = canonical_adapter_kind(adapter) if adapter else "rss"
            if kind_value is None:
                kind_value = "custom_adapter"
        kind = canonical_adapter_kind(kind_value) or normalize_adapter_token(kind_value) or "rss"
        sources.append(
            _construct_source(
                source_item,
                str(zoo.id or zoo.slug),
                kind,
                config,
            )
        )

    for item, zoo in zip(raw_zoos, zoos):
        raw_sources = item.get("sources", [])
        if isinstance(raw_sources, Mapping):
            raw_sources, _ = _config_records(raw_sources, key_field="sources")
        if isinstance(raw_sources, list):
            for source_item in raw_sources:
                if isinstance(source_item, Mapping):
                    add_source(source_item, zoo)

    raw_sources = root.get("sources")
    if raw_sources is not None:
        top_sources, _ = _config_records(raw_sources, key_field="sources")
        for source_item in top_sources:
            if isinstance(source_item, Mapping):
                add_source(source_item)
    # Legacy configs omitted operational source status.  Infer it only after
    # all source declarations have been collected so a zoo with no sources is
    # distinguishable from one with a configured (possibly disabled) source.
    if "source_status" in _model_fields(Zoo):
        for zoo, item in zip(zoos, raw_zoos):
            if item.get("source_status") is None:
                zoo.source_status = (
                    "configured"
                    if any(str(source.zoo_id) == str(zoo.id or zoo.slug) for source in sources)
                    else "not_found"
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
