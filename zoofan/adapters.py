"""Source-adapter taxonomy and injected custom adapter registry.

The crawler has a deliberately small adapter surface.  Built-in adapters are
identified by a canonical taxonomy; configuration may use a few historical
spellings, but every loaded :class:`~zoofan.models.Source` carries the
canonical kind.  Site-specific adapters are supplied by an explicit registry
so configuration never imports arbitrary Python modules or evaluates code.

Adapters only discover/parse source responses.  Fetching policy, retries,
robots checks, logging, persistence, and article de-duplication remain owned
by the surrounding crawler layers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable

from .fetcher import Fetcher, RequestBoundaryError, RequestPolicy, scoped_fetch


# ``archive`` is the historical name for the generic server-rendered HTML
# list adapter.  Keep it canonical so existing configs and persisted rows do
# not change, while accepting the newer descriptive aliases.
CANONICAL_ADAPTER_KINDS = frozenset(
    {
        "rss",
        "atom",
        "sitemap",
        "archive",
        "json_api",
        "custom_adapter",
        "registry_only",
        "unsupported",
    }
)

# Public alias retained for callers which call the taxonomy "source kinds".
CANONICAL_SOURCE_KINDS = CANONICAL_ADAPTER_KINDS

ADAPTER_ALIASES: dict[str, str] = {
    # Feeds.  ``feed`` is intentionally mapped to rss: parse_feed detects
    # Atom documents from their root element while old callers keep seeing a
    # stable source kind.
    "feed": "rss",
    "rss_feed": "rss",
    "rssfeed": "rss",
    "atom_feed": "atom",
    "atomfeed": "atom",
    # Generic HTML/archive spellings.
    "generic_html": "archive",
    "generic-html": "archive",
    "html": "archive",
    "html_list": "archive",
    "html-list": "archive",
    "news_archive": "archive",
    "news-archive": "archive",
    # Health-check-only source spellings.
    "health_check": "registry_only",
    "health-check": "registry_only",
    "healthcheck": "registry_only",
    "health_check_only": "registry_only",
    "healthcheck_only": "registry_only",
    "health-check-only": "registry_only",
    "health_only": "registry_only",
    "check_only": "registry_only",
    "health": "registry_only",
    "registry": "registry_only",
    "press": "registry_only",
    # JSON/API spellings.
    "json": "json_api",
    "json-api": "json_api",
    "api": "json_api",
    "api_json": "json_api",
    # Explicit custom/unsupported spellings.
    "custom": "custom_adapter",
    "custom-adapter": "custom_adapter",
    "plugin": "custom_adapter",
    "not_supported": "unsupported",
    "not-supported": "unsupported",
    "unavailable": "unsupported",
    "disabled": "unsupported",
}

# Add canonical names to the lookup map so callers can use one function for
# both aliases and already-canonical values.  This mapping is public because
# config tooling uses it to display the accepted vocabulary.
ADAPTER_ALIASES.update({name: name for name in CANONICAL_ADAPTER_KINDS})

# Compatibility names used by older code/tests.
SOURCE_ADAPTER_ALIASES = ADAPTER_ALIASES
SOURCE_ADAPTERS = ADAPTER_ALIASES

_CUSTOM_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


def normalize_adapter_token(value: Any) -> str:
    """Normalize a human/config spelling without deciding whether it exists."""

    if value is None:
        return ""
    return str(value).strip().lower().replace(" ", "_")


def canonical_adapter_kind(value: Any) -> Optional[str]:
    """Return a canonical built-in kind, or ``None`` for an unknown token.

    Unknown values are *not* silently converted to ``unsupported``.  An
    unknown kind in ordinary configuration is a configuration error; only an
    explicit ``unsupported`` kind (or one of its documented aliases) gets the
    unsupported classification.
    """

    token = normalize_adapter_token(value)
    if not token:
        return None
    return ADAPTER_ALIASES.get(token)


# Common spellings make this name discoverable to integrations.
canonicalize_kind = canonical_adapter_kind
normalize_source_kind = canonical_adapter_kind
resolve_adapter_kind = canonical_adapter_kind


def is_canonical_adapter_kind(value: Any) -> bool:
    return normalize_adapter_token(value) in CANONICAL_ADAPTER_KINDS


def is_custom_adapter_name(value: Any) -> bool:
    """Validate registry names without importing or executing anything."""

    return isinstance(value, str) and bool(_CUSTOM_NAME_PATTERN.fullmatch(value.strip()))


@runtime_checkable
class ZooNewsAdapter(Protocol):
    """Stable protocol for injected source-specific discovery adapters.

    A custom adapter may accept the complete context (``source``, ``zoo``,
    a source-scoped ``fetcher`` and request policy) or a smaller compatible
    subset.  The discovery layer inspects the bound signature before
    invocation, which avoids masking a genuine ``TypeError`` raised by
    adapter code.  The injected fetcher is an :class:`AdapterFetchContext`,
    not the crawler's raw HTTP client.
    """

    name: str

    def discover(
        self,
        source: Any,
        *,
        zoo: Any = None,
        fetcher: Any = None,
        request_policy: Any = None,
    ) -> Iterable[Any]:
        ...


class _RobotsEnforcingFetcher(Fetcher):
    """Internal Fetcher-shaped proxy used by :func:`scoped_fetch`.

    ``scoped_fetch`` deliberately gives a real ``Fetcher`` responsibility for
    following and validating redirects.  Keep that behavior while overriding
    the one option a custom adapter must never inherit from its application
    fetcher: ``respect_robots=False``.
    """

    def __init__(self, wrapped: Fetcher) -> None:
        self._wrapped = wrapped

    def fetch(
        self,
        url: str,
        *,
        timeout: Optional[float] = None,
        respect_robots: Optional[bool] = None,
        request_policy: Optional[RequestPolicy] = None,
        cache_bypass: bool = False,
    ) -> Any:
        if request_policy is None:
            raise RequestBoundaryError("adapter fetch proxy requires a request policy")
        if respect_robots is False:
            raise RequestBoundaryError("adapter fetch proxy requires robots checks")
        return self._wrapped.fetch(
            url,
            request_policy=request_policy,
            respect_robots=True,
            timeout=timeout,
            cache_bypass=cache_bypass,
        )


class AdapterFetchContext:
    """Small, source-scoped HTTP facade exposed to custom adapters.

    Custom adapters are untrusted extensions from the crawler's point of
    view.  Giving one the crawler's raw :class:`~zoofan.fetcher.Fetcher`
    would let it omit ``request_policy`` (and, for a test/configured fetcher,
    disable robots checks) or request another host.  This context deliberately
    exposes only ``fetch`` and ``get``.  Every call is validated against the
    fixed source policy; request options cannot replace or weaken that policy.

    ``Fetcher`` has an explicit ``respect_robots`` test escape hatch.  When a
    raw instance is wrapped here, the context always passes
    ``respect_robots=True`` so the adapter cannot inherit a disabled setting.
    Other injected clients must opt into ``scoped_fetch``'s policy-aware
    protocol, which keeps their response URL within the requested hop.
    """

    __slots__ = ("_fetcher", "_request_policy")

    def __init__(self, fetcher: Any, request_policy: RequestPolicy) -> None:
        self._fetcher = (
            _RobotsEnforcingFetcher(fetcher)
            if isinstance(fetcher, Fetcher)
            else fetcher
        )
        self._request_policy = request_policy

    @property
    def request_policy(self) -> RequestPolicy:
        """The immutable policy applied to every request."""

        return self._request_policy

    def _fetch(self, url: str, **options: Any) -> Any:
        supplied_policy = options.pop("request_policy", self._request_policy)
        if supplied_policy is not self._request_policy:
            # A custom adapter may have been written against the historical
            # ``fetch(url, request_policy=...)`` protocol.  Reusing the exact
            # fixed policy is safe; replacing it (including with ``None``)
            # would make the request unscoped.
            raise RequestBoundaryError(
                f"source {self._request_policy.source_id} adapter fetch context "
                "rejects an alternate request policy"
            )
        requested_robots = options.pop("respect_robots", True)
        if requested_robots is not True:
            # In particular, reject ``respect_robots=False`` instead of
            # silently ignoring the test/configuration bypass.
            raise RequestBoundaryError(
                f"source {self._request_policy.source_id} adapter fetch context "
                "requires robots checks"
            )
        if options:
            raise RequestBoundaryError(
                f"source {self._request_policy.source_id} adapter fetch context "
                "does not accept request options"
            )
        target = self._request_policy.validate(url)
        # ``scoped_fetch`` validates the target and response boundary.  The
        # internal Fetcher-shaped proxy above preserves Fetcher's redirect
        # handling while forcing the normal fail-closed robots behavior.
        return scoped_fetch(self._fetcher, target, self._request_policy)

    def fetch(self, url: str, **options: Any) -> Any:
        """Fetch one official-host URL under the source policy."""

        return self._fetch(url, **options)

    def get(self, url: str, **options: Any) -> Any:
        """Compatibility alias for adapters written against ``get`` APIs."""

        return self._fetch(url, **options)


# Descriptive aliases make the narrow boundary discoverable without breaking
# integrations that call the object a scoped fetcher/context.
ScopedAdapterFetcher = AdapterFetchContext
CustomAdapterContext = AdapterFetchContext


# Friendly aliases for callers that use the shorter protocol name.
CustomAdapter = ZooNewsAdapter
AdapterProtocol = ZooNewsAdapter


class UnsupportedAdapterError(RuntimeError):
    """Raised when a configured adapter cannot be resolved safely."""

    classification = "unsupported"


@dataclass(frozen=True)
class AdapterRegistration:
    """A named adapter entry, useful in diagnostics and tests."""

    name: str
    adapter: Any


class AdapterRegistry:
    """Explicit in-memory registry for custom adapters.

    The registry accepts instances or callables implementing ``discover``.
    It intentionally has no module-path loading method: an application must
    construct and inject the adapter object itself.
    """

    def __init__(self, adapters: Optional[Mapping[str, Any]] = None, **named: Any) -> None:
        self._adapters: dict[str, Any] = {}
        if adapters:
            for name, adapter in adapters.items():
                self.register(name, adapter)
        for name, adapter in named.items():
            self.register(name, adapter)

    def register(self, name: str, adapter: Any) -> Any:
        if not is_custom_adapter_name(name):
            raise ValueError("custom adapter name must be 1-128 URL-safe characters")
        if adapter is None or not callable(getattr(adapter, "discover", adapter)):
            raise TypeError("custom adapter must provide a discover(...) method")
        key = str(name).strip()
        self._adapters[key] = adapter
        return adapter

    def unregister(self, name: str) -> None:
        self._adapters.pop(str(name).strip(), None)

    def get(self, name: Optional[str], default: Any = None) -> Any:
        if not name:
            return default
        return self._adapters.get(str(name).strip(), default)

    def resolve(self, name: Optional[str]) -> Any:
        return self.get(name)

    def has(self, name: Optional[str]) -> bool:
        return self.get(name) is not None

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def registrations(self) -> tuple[AdapterRegistration, ...]:
        return tuple(AdapterRegistration(name, self._adapters[name]) for name in self.names())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def __iter__(self):
        return iter(self._adapters)


# A fresh registry is exposed for applications that want process-local global
# registration.  DiscoveryEngine still defaults to an empty registry, so
# tests and independent crawlers cannot accidentally share mutable adapters.
DEFAULT_ADAPTER_REGISTRY = AdapterRegistry()
CustomAdapterRegistry = AdapterRegistry


__all__ = [
    "ADAPTER_ALIASES",
    "AdapterProtocol",
    "AdapterFetchContext",
    "AdapterRegistration",
    "AdapterRegistry",
    "CANONICAL_ADAPTER_KINDS",
    "CANONICAL_SOURCE_KINDS",
    "CustomAdapter",
    "CustomAdapterContext",
    "CustomAdapterRegistry",
    "DEFAULT_ADAPTER_REGISTRY",
    "SOURCE_ADAPTER_ALIASES",
    "SOURCE_ADAPTERS",
    "ScopedAdapterFetcher",
    "UnsupportedAdapterError",
    "ZooNewsAdapter",
    "canonical_adapter_kind",
    "canonicalize_kind",
    "is_canonical_adapter_kind",
    "is_custom_adapter_name",
    "normalize_adapter_token",
    "normalize_source_kind",
    "resolve_adapter_kind",
]
