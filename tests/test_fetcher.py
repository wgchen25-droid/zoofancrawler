import pytest

from zoofan.fetcher import (
    Fetcher,
    RequestBoundaryError,
    RequestPolicy,
    RobotsDisallowed,
    RobotsUnavailable,
)
from zoofan.models import FetchResponse, Source, Zoo


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _policy(config=None):
    zoo = Zoo(
        id="z1",
        slug="example",
        website_url="https://official.example/",
        metadata={"official_host": "official.example"},
    )
    source = Source(
        id="source-one",
        zoo_id="z1",
        url="https://official.example/feed",
        config=dict(config or {}),
    )
    return RequestPolicy.from_zoo_source(zoo, source)


def test_valid_robots_allow_rules_permit_content_request():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /\n")
        return FetchResponse(url, 200, b"article")

    response = Fetcher(transport=transport, delay=0, retries=0).fetch(
        "https://official.example/news/story", request_policy=_policy()
    )

    assert response.content == b"article"
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/news/story",
    ]


def test_valid_robots_disallow_rules_deny_content_request():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, 200, b"User-agent: *\nDisallow: /private\n")
        return FetchResponse(url, 200, b"must not fetch")

    with pytest.raises(RobotsDisallowed):
        Fetcher(transport=transport, delay=0, retries=0).fetch(
            "https://official.example/private/story", request_policy=_policy()
        )
    assert calls == ["https://official.example/robots.txt"]


def test_robots_bypass_is_explicit_and_does_not_fetch_robots():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        return FetchResponse(url, 200, b"explicit test bypass")

    response = Fetcher(transport=transport, delay=0, retries=0).fetch(
        "https://official.example/story", respect_robots=False
    )
    assert response.content == b"explicit test bypass"
    assert calls == ["https://official.example/story"]


@pytest.mark.parametrize(
    "status, payload",
    [
        (404, b"not found"),
        (503, b"temporarily unavailable"),
        (200, b""),
        (200, b"not a robots document"),
        (200, b"User-agent *\nAllow: /"),
        (200, b"User-agent: *\nCrawl-delay: not-a-number"),
    ],
)
def test_missing_invalid_or_unparseable_robots_fails_closed(status, payload):
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return FetchResponse(url, status, payload)
        return FetchResponse(url, 200, b"must not fetch")

    with pytest.raises(RobotsUnavailable):
        Fetcher(transport=transport, delay=0, retries=0).fetch(
            "https://official.example/news/story", request_policy=_policy()
        )
    assert calls == ["https://official.example/robots.txt"]


def test_redirect_target_is_checked_against_its_own_robots_rules():
    calls = []

    def transport(url, headers, timeout):
        calls.append(url)
        if url == "https://official.example/robots.txt":
            return FetchResponse(url, 200, b"User-agent: *\nAllow: /start\nDisallow: /private\n")
        if url == "https://official.example/start":
            return FetchResponse(url, 302, b"", {"Location": "/private/final"})
        if url == "https://official.example/private/final":
            return FetchResponse(url, 200, b"must not fetch")
        raise AssertionError(url)

    fetcher = Fetcher(transport=transport, delay=0, retries=0)
    # The same-origin robots document is cached, so this checks the redirect
    # target's path without issuing an unguarded content request.
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://official.example/start", request_policy=_policy())
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/start",
    ]


@pytest.mark.parametrize(
    "key, value",
    [
        ("official_host", "https://official.example"),
        ("official_host", "official.example/path"),
        ("official_host", "official.example:443"),
        ("official_host", "user@official.example"),
        ("official_host", "official.example?token=secret"),
        ("official_host", None),
        ("official_hosts", []),
        ("host_aliases", ["official.example", "https://cdn.example"]),
        ("allowed_hosts", 42),
    ],
)
def test_request_policy_rejects_malformed_direct_model_host_declarations(key, value):
    with pytest.raises(RequestBoundaryError):
        _policy({key: value})


def test_request_policy_validates_zoo_and_source_host_layers_independently():
    zoo = Zoo(
        id="z1",
        website_url="https://official.example/",
        metadata={"official_host": "official.example/path"},
    )
    source = Source(
        id="source-one",
        zoo_id="z1",
        url="https://official.example/feed",
        config={"official_host": "official.example"},
    )

    with pytest.raises(RequestBoundaryError):
        RequestPolicy.from_zoo_source(zoo, source)


@pytest.mark.parametrize(
    "url",
    [
        "relative/path",
        "/relative/path",
        "ftp://official.example/file",
        "https:///missing-host",
        "https://user:password@official.example/private",
        "https://official.example:not-a-port/file",
    ],
)
def test_unscoped_fetch_rejects_non_http_absolute_or_userinfo_targets(url):
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"must not fetch")

    with pytest.raises(ValueError):
        Fetcher(transport=transport, respect_robots=False, delay=0).fetch(url)
    assert calls == []


def test_unscoped_fetch_accepts_http_absolute_target_with_netloc():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"ok")

    response = Fetcher(transport=transport, respect_robots=False, delay=0).fetch(
        "HTTPS://Official.example/story"
    )
    assert response.content == b"ok"
    assert calls == ["https://official.example/story"]


def test_transient_transport_errors_use_bounded_exponential_backoff():
    clock = _FakeClock()
    calls = []
    failures_left = [2]

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if failures_left[0]:
            failures_left[0] -= 1
            raise OSError("temporary network failure")
        return FetchResponse(request_url, 200, b"ok")

    response = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        retries=2,
        backoff_factor=0.5,
        sleep=clock.sleep,
        clock=clock,
    ).fetch("https://official.example/story")

    assert response.content == b"ok"
    assert len(calls) == 3
    assert clock.sleeps == [0.5, 1.0]


def test_transient_http_responses_retry_and_retry_after_overrides_backoff():
    clock = _FakeClock()
    calls = []
    statuses = [503, 429, 200]

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        status = statuses.pop(0)
        headers = {"Retry-After": "3"} if status == 503 else {}
        return FetchResponse(request_url, status, b"ok", headers)

    response = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        retries=2,
        backoff_factor=0.5,
        sleep=clock.sleep,
        clock=clock,
    ).fetch("https://official.example/story")

    assert response.status_code == 200
    assert len(calls) == 3
    assert clock.sleeps == [3.0, 1.0]


def test_non_retryable_http_response_is_returned_without_sleep_or_extra_attempt():
    clock = _FakeClock()
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 404, b"missing")

    response = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        retries=3,
        sleep=clock.sleep,
        clock=clock,
    ).fetch("https://official.example/missing")

    assert response.status_code == 404
    assert calls == ["https://official.example/missing"]
    assert clock.sleeps == []


def test_transient_robots_transport_error_retries_then_allows_content():
    clock = _FakeClock()
    calls = []
    robots_failures = [1]

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url.endswith("/robots.txt"):
            if robots_failures[0]:
                robots_failures[0] -= 1
                raise OSError("temporary robots failure")
            return FetchResponse(request_url, 200, b"User-agent: *\nAllow: /\n")
        return FetchResponse(request_url, 200, b"article")

    response = Fetcher(
        transport=transport,
        delay=0,
        retries=1,
        backoff_factor=0.25,
        sleep=clock.sleep,
        clock=clock,
    ).fetch("https://official.example/story", request_policy=_policy())

    assert response.content == b"article"
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/robots.txt",
        "https://official.example/story",
    ]
    assert clock.sleeps == [0.25]


def test_transient_robots_http_failure_fails_closed_after_retry_limit():
    clock = _FakeClock()
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url.endswith("/robots.txt"):
            return FetchResponse(request_url, 503, b"unavailable")
        return FetchResponse(request_url, 200, b"must not fetch")

    with pytest.raises(RobotsUnavailable):
        Fetcher(
            transport=transport,
            delay=0,
            retries=2,
            backoff_factor=0.5,
            sleep=clock.sleep,
            clock=clock,
        ).fetch("https://official.example/story", request_policy=_policy())
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/robots.txt",
        "https://official.example/robots.txt",
    ]
    assert clock.sleeps == [0.5, 1.0]


def test_malformed_robots_is_not_retried():
    clock = _FakeClock()
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"not a robots document")

    with pytest.raises(RobotsUnavailable):
        Fetcher(
            transport=transport,
            delay=0,
            retries=3,
            sleep=clock.sleep,
            clock=clock,
        ).fetch("https://official.example/story", request_policy=_policy())
    assert calls == ["https://official.example/robots.txt"]
    assert clock.sleeps == []


def test_redirect_target_rechecks_robots_for_explicit_official_alias():
    clock = _FakeClock()
    calls = []
    policy = _policy({"official_hosts": ["official.example", "cdn.official.example"]})

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url == "https://official.example/robots.txt":
            return FetchResponse(request_url, 200, b"User-agent: *\nAllow: /\n")
        if request_url == "https://cdn.official.example/robots.txt":
            return FetchResponse(request_url, 200, b"User-agent: *\nDisallow: /\n")
        if request_url == "https://official.example/start":
            return FetchResponse(request_url, 302, b"", {"Location": "https://cdn.official.example/final"})
        raise AssertionError(request_url)

    with pytest.raises(RobotsDisallowed):
        Fetcher(transport=transport, delay=0, retries=0, sleep=clock.sleep, clock=clock).fetch(
            "https://official.example/start", request_policy=policy
        )
    assert calls == [
        "https://official.example/robots.txt",
        "https://official.example/start",
        "https://cdn.official.example/robots.txt",
    ]


def test_response_cache_is_enabled_by_conservative_default():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"fresh")

    fetcher = Fetcher(transport=transport, respect_robots=False, delay=0)
    fetcher.fetch("https://official.example/story")
    fetcher.fetch("https://official.example/story")

    assert fetcher.cache_ttl == Fetcher.DEFAULT_CACHE_TTL
    assert fetcher.cache_ttl > 0
    assert calls == ["https://official.example/story"]


def test_response_cache_can_be_disabled_with_explicit_zero_ttl():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"fresh")

    fetcher = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        cache_ttl=0,
    )
    fetcher.fetch("https://official.example/story")
    fetcher.fetch("https://official.example/story")

    assert calls == [
        "https://official.example/story",
        "https://official.example/story",
    ]


def test_bounded_cache_hits_expires_entries_and_refresh_bypasses():
    clock = _FakeClock()
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url.endswith("/robots.txt"):
            return FetchResponse(request_url, 200, b"User-agent: *\nAllow: /\n")
        return FetchResponse(request_url, 200, request_url.encode())

    fetcher = Fetcher(
        transport=transport,
        delay=0,
        retries=0,
        cache_ttl=10,
        cache_max_entries=2,
        sleep=clock.sleep,
        clock=clock,
    )
    first = fetcher.fetch("https://official.example/story?item=1", request_policy=_policy())
    second = fetcher.fetch("https://official.example/story?item=1", request_policy=_policy())
    assert first.content == second.content
    assert calls.count("https://official.example/story?item=1") == 1

    fetcher.fetch("https://official.example/story?item=2", request_policy=_policy())
    assert calls.count("https://official.example/story?item=2") == 1
    clock.now += 11
    fetcher.fetch("https://official.example/story?item=1", request_policy=_policy())
    assert calls.count("https://official.example/story?item=1") == 2

    # A bypass is a fresh request and replaces the expired entry; a following
    # normal request therefore hits that refreshed successful response.
    fetcher.fetch(
        "https://official.example/story?item=1",
        request_policy=_policy(),
        cache_bypass=True,
    )
    assert calls.count("https://official.example/story?item=1") == 3
    fetcher.fetch("https://official.example/story?item=1", request_policy=_policy())
    assert calls.count("https://official.example/story?item=1") == 3


def test_bounded_cache_evicts_least_recently_used_entry():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, request_url.encode())

    fetcher = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        cache_ttl=60,
        cache_max_entries=2,
    )
    first = "https://official.example/first"
    second = "https://official.example/second"
    third = "https://official.example/third"

    fetcher.fetch(first)
    fetcher.fetch(second)
    fetcher.fetch(first)  # Make ``second`` the least-recently-used entry.
    fetcher.fetch(third)
    fetcher.fetch(first)
    fetcher.fetch(second)

    assert calls == [first, second, third, second]


def test_cache_checks_robots_before_reusing_a_cached_response():
    calls = []
    robots_disallow = [False]

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url.endswith("/robots.txt"):
            payload = "User-agent: *\nDisallow: /story\n" if robots_disallow[0] else "User-agent: *\nAllow: /\n"
            return FetchResponse(request_url, 200, payload.encode())
        return FetchResponse(request_url, 200, b"article")

    fetcher = Fetcher(transport=transport, delay=0, retries=0, cache_ttl=60)
    policy = _policy()
    url = "https://official.example/story"
    # Populate the cache without loading a robots parser, then require the
    # next request to perform its normal fail-closed robots check.
    fetcher.fetch(url, request_policy=policy, respect_robots=False)
    robots_disallow[0] = True
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch(url, request_policy=policy)

    assert calls == ["https://official.example/story", "https://official.example/robots.txt"]


def test_cache_checks_official_url_policy_before_reusing_a_cached_response():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, 200, b"article")

    fetcher = Fetcher(transport=transport, respect_robots=False, delay=0, cache_ttl=60)
    fetcher.fetch("https://official.example/story")

    other_zoo = Zoo(
        id="z2",
        slug="other",
        website_url="https://other.example/",
        metadata={"official_host": "other.example"},
    )
    other_source = Source(
        id="source-two",
        zoo_id="z2",
        url="https://other.example/feed",
        config={},
    )
    with pytest.raises(RequestBoundaryError):
        fetcher.fetch(
            "https://official.example/story",
            request_policy=RequestPolicy.from_zoo_source(other_zoo, other_source),
            respect_robots=False,
        )

    assert calls == ["https://official.example/story"]


@pytest.mark.parametrize("status", [404, 503])
def test_non_success_responses_are_never_cached(status):
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(request_url, status, b"not cached")

    fetcher = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        retries=0,
        cache_ttl=60,
    )
    url = "https://official.example/story"
    assert fetcher.fetch(url).status_code == status
    assert fetcher.fetch(url).status_code == status
    assert calls == [url, url]


def test_redirect_responses_are_not_cached_and_policy_is_reapplied():
    clock = _FakeClock()
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        if request_url.endswith("/robots.txt"):
            return FetchResponse(request_url, 200, b"User-agent: *\nAllow: /\n")
        if request_url.endswith("/start"):
            return FetchResponse(request_url, 302, b"", {"Location": "/final"})
        return FetchResponse(request_url, 200, b"final")

    fetcher = Fetcher(
        transport=transport,
        delay=0,
        retries=0,
        cache_ttl=60,
        sleep=clock.sleep,
        clock=clock,
    )
    fetcher.fetch("https://official.example/start", request_policy=_policy())
    fetcher.fetch("https://official.example/start", request_policy=_policy())
    assert calls.count("https://official.example/start") == 2
    assert calls.count("https://official.example/final") == 2


def test_transport_redirect_history_also_prevents_cache_reuse():
    calls = []

    def transport(request_url, headers, timeout):
        calls.append(request_url)
        return FetchResponse(
            request_url,
            200,
            b"final",
            history=("https://official.example/redirect",),
        )

    fetcher = Fetcher(
        transport=transport,
        respect_robots=False,
        delay=0,
        cache_ttl=60,
    )
    url = "https://official.example/story"
    fetcher.fetch(url)
    fetcher.fetch(url)

    assert calls == [url, url]
