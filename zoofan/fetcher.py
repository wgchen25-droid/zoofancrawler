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


class FetchError(RuntimeError):
    """Base class for fetch failures."""


class TransportError(FetchError):
    """The injected/default transport could not obtain a response."""


class RobotsDisallowed(FetchError):
    """robots.txt disallows the requested path."""


class RobotsUnavailable(FetchError):
    """robots.txt could not be fetched or parsed; fetching fails closed."""


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


class Fetcher:
    """HTTP client with robots checks, per-domain serialization and retries.

    ``transport`` is injectable.  It may accept a :class:`FetchRequest`, a
    ``(url, headers, timeout)`` triplet, ``(url, timeout)`` or simply ``url``;
    return a :class:`FetchResponse`, a requests-like response, or a
    ``(status, content[, headers[, final_url]])`` tuple.
    """

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
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
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

    def _robots_parser(self, origin: str) -> Optional[urllib.robotparser.RobotFileParser]:
        if origin in self._robots:
            return self._robots[origin]
        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        transport = self.robots_transport or self.transport
        try:
            # robots requests are subject to the same per-domain politeness
            # budget as content requests, including injected transports.
            self._wait_for_domain(self._domain(robots_url))
            response = self._call_transport(
                transport,
                robots_url,
                {"User-Agent": self.user_agent, "Accept": "text/plain,*/*;q=0.1"},
            )
        except FetchError as exc:
            raise RobotsUnavailable(f"unable to fetch robots.txt for {origin}") from exc
        if response.status_code == 404:
            self._robots[origin] = None
            return None
        if response.status_code >= 400:
            raise RobotsUnavailable(f"robots.txt returned HTTP {response.status_code} for {origin}")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        self._robots[origin] = parser
        return parser

    def _allowed_by_robots(self, url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        if not parsed.netloc or parsed.scheme not in {"http", "https"}:
            return True
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots_parser(origin)
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
    ) -> FetchResponse:
        """Fetch one URL, returning 404/other terminal HTTP responses intact."""

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
                    if not self._allowed_by_robots(url):
                        raise RobotsDisallowed(f"robots.txt disallows {url}")
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
                        except TransportError:
                            if attempts >= self.retries:
                                raise
                            time.sleep(self.backoff_factor * (2**attempts))
                            attempts += 1
                            continue
                    finally:
                        if owns_request_lock:
                            request_lock.release()
                    response.history = tuple(history)
                    if 300 <= response.status_code < 400:
                        location = _header(response, "Location")
                        if location and len(history) < self.max_redirects:
                            next_url = urllib.parse.urljoin(current_url, location)
                            next_domain = self._domain(next_url)
                            # Every redirect target gets an independent
                            # robots check; this also fail-closes cross-domain
                            # redirects whose robots endpoint is unavailable.
                            if not self._allowed_by_robots(next_url):
                                raise RobotsDisallowed(f"robots.txt disallows redirect target {next_url}")
                            history.append(current_url)
                            current_url = next_url
                            current_domain = next_domain
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
