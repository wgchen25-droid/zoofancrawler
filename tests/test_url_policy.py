import pytest

from zoofan.discovery import URLPolicy
from zoofan.fetcher import RequestPolicy
from zoofan.models import Source, Zoo


def _configured_source() -> tuple[Zoo, Source]:
    zoo = Zoo(id="z1", slug="example", website_url="https://example.org/")
    source = Source(
        id="news",
        zoo_id="z1",
        kind="rss",
        url="https://news.example.net/feed",
        config={
            "official_host": "example.org",
            "host_aliases": ["news.example.net"],
            "official_host_aliases": ["cdn.example.org"],
            "allow_regex": r"/news/",
        },
    )
    return zoo, source


def test_explicit_host_alias_allows_source_and_candidate_for_both_policies():
    zoo, source = _configured_source()
    url_policy = URLPolicy.from_zoo_source(zoo, source)
    request_policy = RequestPolicy.from_zoo_source(zoo, source)

    assert request_policy.validate(source.url) == source.url
    assert url_policy.accepts(
        "https://news.example.net/news/story?utm_source=feed",
        source_url=source.url,
    )
    assert url_policy.accepts(
        "https://cdn.example.org/news/story",
        source_url=source.url,
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.org/news/story",
        "https://foo.news.example.net/news/story",
        "https://badexample.org/news/story",
        "https://example.org.evil.test/news/story",
        "ftp://news.example.net/news/story",
        "https://attacker@news.example.net/news/story",
        "https://news.example.net:bad/news/story",
        "https://third-party.test/news/story",
    ],
)
def test_url_policy_rejects_undeclared_subdomains_spoofed_suffixes_and_unsafe_urls(url):
    zoo, source = _configured_source()
    policy = URLPolicy.from_zoo_source(zoo, source)

    assert not policy.accepts(url, source_url=source.url)


def test_explicit_allow_cannot_override_domain_scheme_or_exclude():
    policy = URLPolicy(
        official_hosts=("example.org", "news.example.net"),
        allow_patterns=(r"/events/",),
        exclude_patterns=(r"/events/private",),
    )

    assert policy.accepts("https://news.example.net/events/public")
    assert not policy.accepts("https://news.example.net/events/private")
    assert not policy.accepts("https://foo.news.example.net/events/public")
    assert not policy.accepts("https://news.example.net.evil/events/public")
    assert not policy.accepts("ftp://news.example.net/events/public")
    assert not policy.accepts("https://attacker@news.example.net/events/public")


def test_broad_allow_cannot_accept_homepage_or_source_but_accepts_article():
    source_url = "https://example.org/feed"
    policy = URLPolicy(official_hosts=("example.org",), allow_patterns=(r".*",))

    assert not policy.accepts("https://example.org/", source_url=source_url)
    assert not policy.accepts(source_url, source_url=source_url)
    assert policy.accepts("https://example.org/news/story", source_url=source_url)


def test_allow_matching_source_url_still_rejects_source():
    source_url = "https://example.org/feed"
    policy = URLPolicy(official_hosts=("example.org",), allow_patterns=(r"/feed",))

    assert not policy.accepts(source_url, source_url=source_url)


def test_malformed_allow_pattern_fails_closed_without_literal_substring_fallback():
    policy = URLPolicy(official_hosts=("example.org",), allow_patterns=(r"/news/[",))

    assert not policy.accepts("https://example.org/news/[")
