"""Polite, testable HTTP fetching primitives."""

from __future__ import annotations

import inspect
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping, Optional

from .models import FetchResponse
from .normalization import normalize_url


class FetchError(RuntimeError):
    """Base class for fetch failures."""


class TransportError(FetchError):
    """The injected/default transport could not obtain a response."""


class RobotsDisallowed(FetchError):
    """robots.txt disallows the requested path."""


class RobotsUnavailable(FetchError):
    """robots.txt could not be fetched or parsed; fetching fails closed."""


class RequestBoundaryError(FetchError):
    """A source-scoped request target was rejected before transport."""


@dataclass(frozen=True)
class RequestPolicy:
    """Validate network targets, independently of article-path selection.

    Hosts are exact configured official hosts (including explicit aliases).
    Article allow/exclude regexes deliberately have no bearing on this gate.
    """

    source_id: str
    official_hosts: tuple[str, ...]

    @classmethod
    def from_zoo_source(cls, zoo: Any, source: Any) -> "RequestPolicy":
        zoo_metadata = dict(getattr(zoo, "metadata", {}) or {})
        source_config = dict(getattr(source, "config", {}) or {})
        config = {**zoo_metadata, **source_config}
        hosts: list[str] = []
        for key in ("official_host", "host"):
            value = config.get(key)
            if value:
                hosts.append(str(value))
        for key in ("official_hosts", "allowed_hosts", "allowed_domains", "host_aliases", "official_host_aliases"):
            value = config.get(key) or ()
            if isinstance(value, str):
                value = [value]
            hosts.extend(str(item) for item in value if item)
        if not hosts:
            website = getattr(zoo, "website_url", None) or getattr(zoo, "url", None)
            source_url = getattr(source, "url", None)
            host = urllib.parse.urlsplit(str(website or source_url or "")).hostname
            if host:
                hosts.append(host)
        normalized_hosts: list[str] = []
        for value in hosts:
            parsed = urllib.parse.urlsplit(value if "://" in value else "//" + value)
            host = (parsed.hostname or "").lower().strip(".")
            if host and host not in normalized_hosts:
                normalized_hosts.append(host)
        return cls(str(getattr(source, "id", None) or "unknown-source"), tuple(normalized_hosts))

    @staticmethod
    def safe_target(url: str) -> str:
        """Return a diagnostic target without credentials or query secrets."""

        try:
            parsed = urllib.parse.urlsplit(str(url).strip())
            host = (parsed.hostname or "").lower().rstrip(".")
            if parsed.port is not None:
                host = f"{host}:{parsed.port}"
            return urllib.parse.urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))
        except (TypeError, ValueError):
            return "<invalid-url>"

    @staticmethod
    def _transport_url(url: str) -> str:
        """Canonicalize a request URL without changing server-visible semantics.

        Unlike article identity normalization, this preserves trailing slashes,
        path escaping, and the complete query. Only surrounding whitespace,
        host/scheme case, default ports, and the fragment are normalized.
        """

        parsed = urllib.parse.urlsplit(str(url).strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port  # validates numeric range
        rendered_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        default_port = (parsed.scheme.lower() == "http" and port == 80) or (
            parsed.scheme.lower() == "https" and port == 443
        )
        if port is not None and not default_port:
            rendered_host += f":{port}"
        return urllib.parse.urlunsplit(
            (parsed.scheme.lower(), rendered_host, parsed.path or "/", parsed.query, "")
        )

    def validate(self, url: str) -> str:
        safe = self.safe_target(url)
        try:
            normalized = self._transport_url(url)
            parsed = urllib.parse.urlsplit(str(url).strip())
            host = (parsed.hostname or "").lower().rstrip(".")
            # Accessing port is itself validation: urllib raises for
            # non-numeric and out-of-range values.
            parsed.port
            valid = (
                bool(normalized)
                and parsed.scheme in {"http", "https"}
                and bool(parsed.netloc)
                and bool(host)
                and parsed.username is None
                and parsed.password is None
                and host in self.official_hosts
            )
        except (TypeError, ValueError):
            valid = False
            normalized = ""
        if not valid:
            raise RequestBoundaryError(
                f"source {self.source_id} rejected request target {safe}"
            )
        return normalized


class HTTPStatusError(FetchError):
    """Raised when ``FetchResponse.raise_for_status`` sees an HTTP error."""

    def __init__(self, status_code: int, url: str, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.url = url
        self.reason = reason
        message = f"HTTP {status_code} for {url}"
        if reason:
            message += f": {reason}"
        super().__init__(message)


@dataclass
class FetchRequest:
    url: str
    headers: Mapping[str, str]
    timeout: float


Transport = Callable[..., Any]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Make urllib obey the transport's one-hop response contract."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def _header(response: FetchResponse, name: str) -> Optional[str]:
    wanted = name.lower()
    for key, value in response.headers.items():
        if key.lower() == wanted:
            return str(value)
    return None


def _coerce_response(value: Any, requested_url: str) -> FetchResponse:
    if isinstance(value, FetchResponse):
        return value
    if isinstance(value, tuple):
        if len(value) == 2:
            status, content = value
            headers = {}
            final_url = requested_url
        elif len(value) >= 3:
            status, content, headers = value[:3]
            final_url = value[3] if len(value) > 3 else requested_url
        else:
            raise TransportError("transport returned an empty tuple")
        if isinstance(content, str):
            content = content.encode("utf-8")
        return FetchResponse(str(final_url), int(status), bytes(content or b""), headers or {})
    # requests.Response and urllib responses expose these attributes.
    status = getattr(value, "status_code", getattr(value, "status", None))
    if status is None:
        raise TransportError("transport did not return a response or status")
    content = getattr(value, "content", None)
    if content is None:
        content = getattr(value, "read", lambda: b"")()
    if isinstance(content, str):
        content = content.encode("utf-8")
    headers = getattr(value, "headers", {}) or {}
    final_url = getattr(value, "url", requested_url) or requested_url
    reason = getattr(value, "reason", None)
    return FetchResponse(str(final_url), int(status), bytes(content or b""), headers, reason=reason)


def scoped_fetch(fetcher: Any, url: str, policy: RequestPolicy) -> Any:
    """Call an injected fetcher under the same one-hop request boundary.

    Injected fetchers must explicitly declare ``supports_request_policy`` and
    accept ``request_policy=``. Their response URL must identify the exact
    requested hop. :class:`Fetcher` is special because it validates and
    follows each redirect hop internally before returning the final response.
    """

    target = policy.validate(url)
    if isinstance(fetcher, Fetcher):
        return fetcher.fetch(target, request_policy=policy)
    if not bool(getattr(fetcher, "supports_request_policy", False)):
        raise RequestBoundaryError(
            f"source {policy.source_id} rejected untrusted injected fetcher before request to {policy.safe_target(target)}"
        )
    method = getattr(fetcher, "fetch", None) or getattr(fetcher, "get", None)
    if method is None:
        raise TypeError("policy-aware fetcher must provide fetch(url, request_policy=...)")
    try:
        response = method(target, request_policy=policy)
    except TypeError as exc:
        raise TypeError(
            "policy-aware fetcher must accept fetch(url, request_policy=...)"
        ) from exc
    response_url: Optional[str] = None
    if isinstance(response, FetchResponse):
        response_url = response.url
    elif isinstance(response, tuple) and len(response) > 3:
        response_url = str(response[3])
    elif not isinstance(response, (str, bytes)):
        value = getattr(response, "url", None)
        if value:
            response_url = str(value)
    if response_url is None:
        raise RequestBoundaryError(
            f"source {policy.source_id} policy-aware fetcher returned no one-hop response URL for {policy.safe_target(target)}"
        )
    if policy.validate(response_url) != target:
        raise RequestBoundaryError(
            f"source {policy.source_id} transport response URL changed unexpectedly to {policy.safe_target(response_url)}"
        )
    return response


class Fetcher:
    """HTTP client with robots checks, per-domain serialization and retries.

    ``transport`` is injectable.  It may accept a :class:`FetchRequest`, a
    ``(url, headers, timeout)`` triplet, ``(url, timeout)`` or simply ``url``;
    return a :class:`FetchResponse`, a requests-like response, or a
    ``(status, content[, headers[, final_url]])`` tuple.
    """

    supports_request_policy = True

    def __init__(
        self,
        user_agent: str = "ZooFanCrawler/0.1 (+https://github.com/zoofancrawler)",
        timeout: float = 20.0,
        delay: float = 1.0,
        retries: int = 3,
        backoff_factor: float = 0.5,
        transport: Optional[Transport] = None,
        respect_robots: bool = True,
        robots_transport: Optional[Transport] = None,
        max_redirects: int = 5,
    ) -> None:
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.delay = max(0.0, float(delay))
        self.retries = max(0, int(retries))
        self.backoff_factor = max(0.0, float(backoff_factor))
        self.transport = transport or self._urllib_transport
        self.robots_transport = robots_transport
        self.respect_robots = respect_robots
        self.max_redirects = max(0, int(max_redirects))
        self._domain_locks: dict[str, threading.Lock] = {}
        self._lock_guard = threading.Lock()
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def _domain_lock(self, domain: str) -> threading.Lock:
        with self._lock_guard:
            return self._domain_locks.setdefault(domain, threading.Lock())

    @staticmethod
    def _domain(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return parsed.netloc.lower()

    def _urllib_transport(self, request: Any, headers=None, timeout=None) -> FetchResponse:
        if isinstance(request, FetchRequest):
            url, headers, timeout = request.url, request.headers, request.timeout
        else:
            url = request
        req = urllib.request.Request(url, headers=dict(headers or {}), method="GET")
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(req, timeout=timeout or self.timeout) as response:
                return FetchResponse(
                    url=response.geturl(),
                    status_code=int(response.getcode() or 200),
                    content=response.read(),
                    headers=dict(response.headers.items()),
                    reason=getattr(response, "reason", None),
                )
        except urllib.error.HTTPError as exc:
            return FetchResponse(
                url=exc.geturl() or url,
                status_code=int(exc.code),
                content=exc.read() if hasattr(exc, "read") else b"",
                headers=dict(exc.headers.items()) if exc.headers else {},
                reason=str(exc.reason),
            )

    def _call_transport(self, transport: Transport, url: str, headers: Mapping[str, str]) -> FetchResponse:
        request = FetchRequest(url=url, headers=headers, timeout=self.timeout)
        try:
            signature = inspect.signature(transport)
        except (TypeError, ValueError):
            signature = None
        try:
            if signature is not None:
                parameters = list(signature.parameters.values())
                positional = [parameter for parameter in parameters if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)]
                has_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in parameters)
                names = {parameter.name.lower() for parameter in parameters}
                if has_varargs or len(positional) >= 3:
                    # Most test callbacks use (url, headers, timeout).
                    value = transport(url, headers, self.timeout)
                elif len(positional) == 2:
                    if {"request", "req"} & names:
                        value = transport(request, self.timeout)
                    else:
                        value = transport(url, self.timeout)
                elif len(positional) == 1:
                    parameter_name = positional[0].name.lower()
                    value = transport(request if parameter_name in {"request", "req"} else url)
                else:
                    value = transport()
            else:
                value = transport(request)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(str(exc)) from exc
        return _coerce_response(value, url)

    def _validate_response_url(self, response: FetchResponse, requested_url: str, policy: Optional[RequestPolicy]) -> None:
        requested = policy.validate(requested_url) if policy else normalize_url(requested_url)
        actual = policy.validate(response.url) if policy else normalize_url(response.url)
        if not actual or actual != requested:
            source = policy.source_id if policy else "unscoped"
            safe = policy.safe_target(response.url) if policy else RequestPolicy.safe_target(response.url)
            raise RequestBoundaryError(
                f"source {source} transport response URL changed unexpectedly to {safe}"
            )

    def _robots_parser(self, origin: str, policy: Optional[RequestPolicy]) -> Optional[urllib.robotparser.RobotFileParser]:
        if origin in self._robots:
            return self._robots[origin]
        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        transport = self.robots_transport or self.transport
        try:
            current_url = policy.validate(robots_url) if policy else normalize_url(robots_url)
            history: list[str] = []
            while True:
                # robots requests are subject to the same politeness budget.
                self._wait_for_domain(self._domain(current_url))
                response = self._call_transport(
                    transport, current_url,
                    {"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.1"},
                )
                self._validate_response_url(response, current_url, policy)
                if not 300 <= response.status_code < 400:
                    break
                location = (_header(response, "Location") or "").strip()
                if not location:
                    raise RequestBoundaryError(
                        f"source {policy.source_id if policy else 'unscoped'} robots redirect missing Location at {RequestPolicy.safe_target(current_url)}"
                    )
                if len(history) >= self.max_redirects:
                    raise RequestBoundaryError(
                        f"source {policy.source_id if policy else 'unscoped'} robots redirect limit exceeded at {RequestPolicy.safe_target(current_url)}"
                    )
                next_url = urllib.parse.urljoin(current_url, location)
                current_url = policy.validate(next_url) if policy else normalize_url(next_url)
                history.append(current_url)
        except FetchError as exc:
            if isinstance(exc, RequestBoundaryError):
                raise
            raise RobotsUnavailable(f"unable to fetch robots.txt for {origin}") from exc
        if response.status_code == 404:
            self._robots[origin] = None
            return None
        if response.status_code >= 400:
            raise RobotsUnavailable(f"robots.txt returned HTTP {response.status_code} for {origin}")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(current_url)
        parser.parse(response.text.splitlines())
        self._robots[origin] = parser
        return parser

    def _allowed_by_robots(self, url: str, policy: Optional[RequestPolicy]) -> bool:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.netloc or parsed.scheme not in {"http", "https"}:
            return True
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots_parser(origin, policy)
        return parser is None or parser.can_fetch(self.user_agent, url)

    def _wait_for_domain(self, domain: str) -> None:
        now = time.monotonic()
        previous = self._last_request.get(domain)
        if previous is not None and self.delay:
            remaining = self.delay - (now - previous)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request[domain] = time.monotonic()

    @staticmethod
    def _retry_after(response: FetchResponse) -> Optional[float]:
        value = _header(response, "Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                dt = parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    def fetch(
        self,
        url: str,
        *,
        timeout: Optional[float] = None,
        respect_robots: Optional[bool] = None,
        request_policy: Optional[RequestPolicy] = None,
    ) -> FetchResponse:
        """Fetch one URL, returning 404/other terminal HTTP responses intact."""

        if request_policy is not None:
            url = request_policy.validate(url)
        if not url or not urllib.parse.urlsplit(url).scheme:
            raise ValueError(f"absolute URL required: {url!r}")
        domain = self._domain(url)
        lock = self._domain_lock(domain)
        original_timeout = self.timeout
        if timeout is not None:
            self.timeout = float(timeout)
        try:
            with lock:
                if respect_robots if respect_robots is not None else self.respect_robots:
                    if not self._allowed_by_robots(url, request_policy):
                        source = request_policy.source_id if request_policy else "unscoped"
                        raise RobotsDisallowed(
                            f"source {source} robots.txt disallows {RequestPolicy.safe_target(url)}"
                        )
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                current_url = url
                current_domain = domain
                history: list[str] = []
                attempts = 0
                while True:
                    request_domain = self._domain(current_url)
                    request_lock = self._domain_lock(request_domain)
                    owns_request_lock = request_lock is not lock
                    if owns_request_lock:
                        request_lock.acquire()
                    try:
                        self._wait_for_domain(request_domain)
                        try:
                            response = self._call_transport(self.transport, current_url, headers)
                        except TransportError as exc:
                            if attempts >= self.retries:
                                if request_policy:
                                    raise TransportError(
                                        f"source {request_policy.source_id} transport failed for {request_policy.safe_target(current_url)}"
                                    ) from exc
                                raise
                            time.sleep(self.backoff_factor * (2**attempts))
                            attempts += 1
                            continue
                    finally:
                        if owns_request_lock:
                            request_lock.release()
                    response.history = tuple(history)
                    self._validate_response_url(response, current_url, request_policy)
                    if 300 <= response.status_code < 400:
                        location = (_header(response, "Location") or "").strip()
                        source = request_policy.source_id if request_policy else "unscoped"
                        if not location:
                            raise RequestBoundaryError(
                                f"source {source} redirect missing Location at {RequestPolicy.safe_target(current_url)}"
                            )
                        if len(history) >= self.max_redirects:
                            raise RequestBoundaryError(
                                f"source {source} redirect limit exceeded at {RequestPolicy.safe_target(current_url)}"
                            )
                        next_url = urllib.parse.urljoin(current_url, location)
                        next_url = request_policy.validate(next_url) if request_policy else normalize_url(next_url)
                        # Validate first, then robots, then content transport.
                        if respect_robots if respect_robots is not None else self.respect_robots:
                            if not self._allowed_by_robots(next_url, request_policy):
                                raise RobotsDisallowed(
                                    f"source {source} robots.txt disallows redirect target {RequestPolicy.safe_target(next_url)}"
                                )
                        history.append(current_url)
                        current_url = next_url
                        current_domain = self._domain(next_url)
                        continue
                    if response.status_code in {429, 500, 502, 503, 504, 507, 508, 520, 521, 522, 523, 524} and attempts < self.retries:
                        wait = self._retry_after(response)
                        if wait is None:
                            wait = self.backoff_factor * (2**attempts)
                        if wait:
                            time.sleep(wait)
                        attempts += 1
                        continue
                    return response
        finally:
            self.timeout = original_timeout

    get = fetch
