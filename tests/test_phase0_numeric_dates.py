"""Regression coverage for numeric dates used by enabled German archives."""

from datetime import datetime, timezone

import pytest

from zoofan.parsers import parse_archive_page, parse_datetime


_PHASE0_ARCHIVE_HOSTS = {
    "zoo-leipzig-news": "www.zoo-leipzig.de",
    "zoo-frankfurt-press-news": "www.zoo-frankfurt.de",
    "tierpark-fossilium-bochum-news": "www.tierpark-bochum.de",
    "aquazoo-loebbecke-museum-news": "aquazoo-duesseldorf.de",
}


@pytest.mark.parametrize(
    ("source_id", "value", "expected"),
    [
        ("zoo-leipzig-news", "03.04.2025", datetime(2025, 4, 3, tzinfo=timezone.utc)),
        ("zoo-frankfurt-press-news", "3.4.2025", datetime(2025, 4, 3, tzinfo=timezone.utc)),
        ("tierpark-fossilium-bochum-news", "14.08.2026", datetime(2026, 8, 14, tzinfo=timezone.utc)),
        ("aquazoo-loebbecke-museum-news", "31.12.2025", datetime(2025, 12, 31, tzinfo=timezone.utc)),
    ],
)
def test_enabled_phase0_archive_numeric_dates_are_day_month_year(
    source_id: str, value: str, expected: datetime
) -> None:
    """The four enabled archive plans expose German dotted day-first dates."""

    host = _PHASE0_ARCHIVE_HOSTS[source_id]
    payload = f"""
        <article class="card">
          <a href="/news/article">A dated article</a>
          <h2>A dated article</h2>
          <span class="date">{value}</span>
        </article>
    """
    items = parse_archive_page(
        payload,
        {
            "article_selector": "article.card",
            "link_selector": "a[href]",
            "title_selector": "h2",
            "date_selector": ".date",
        },
        f"https://{host}/news",
    )

    assert len(items) == 1
    assert items[0].published_at == expected
    assert items[0].metadata["published_at_raw"] == value
    assert "date_parse_error" not in items[0].metadata.get("error_classifications", [])


def test_parse_datetime_accepts_dotted_leap_day_and_keeps_day_first_order() -> None:
    assert parse_datetime("29.02.2024") == datetime(2024, 2, 29, tzinfo=timezone.utc)
    # The dotted German form is DMY: this is March 4, not April 3.
    assert parse_datetime("04.03.2025") == datetime(2025, 3, 4, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    [
        "29.02.2023",
        "31.04.2025",
        "00.01.2025",
        "01.00.2025",
        "01.13.2025",
        "01.01.25",
        "01-01-2025",
    ],
)
def test_parse_datetime_rejects_invalid_or_non_dotted_numeric_dates(value: str) -> None:
    assert parse_datetime(value) is None


@pytest.mark.parametrize("value", ["04/03/2025", "13/04/2025"])
def test_parse_datetime_rejects_slash_dates_without_a_locale_contract(value: str) -> None:
    """Slash dates are ambiguous and the parser has no source-locale argument."""

    assert parse_datetime(value) is None


@pytest.mark.parametrize("date_markup", ["", '<span class="date">31.02.2025</span>'])
def test_archive_missing_or_unparseable_dates_remain_null(date_markup: str) -> None:
    payload = f"""
        <article class="card">
          <a href="/news/article">An article</a>
          <h2>An article</h2>
          {date_markup}
        </article>
    """
    item = parse_archive_page(
        payload,
        {
            "article_selector": "article.card",
            "link_selector": "a[href]",
            "title_selector": "h2",
            "date_selector": ".date",
        },
        "https://www.zoo-leipzig.de/news",
    )[0]

    assert item.published_at is None
    if date_markup:
        assert item.metadata["published_at_raw"] == "31.02.2025"
        assert "date_parse_error" in item.metadata["error_classifications"]
    else:
        assert "published_at_raw" not in item.metadata

