from zoofan.normalization import normalize_url
from zoofan.config import parse_bool


def test_normalize_url_removes_tracking_fragment_and_sorts_query():
    assert normalize_url(
        "HTTPS://Example.COM/news///?utm_source=mail&b=2&a=1#comments"
    ) == "https://example.com/news?a=1&b=2"


def test_normalize_url_keeps_meaningful_duplicate_query_values():
    assert normalize_url("https://example.com/search?tag=z&tag=a") == (
        "https://example.com/search?tag=a&tag=z"
    )


def test_parse_bool_does_not_treat_false_string_as_true():
    assert parse_bool("false") is False
    assert parse_bool("yes") is True
