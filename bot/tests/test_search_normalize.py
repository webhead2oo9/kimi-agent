from __future__ import annotations

from search.normalize import canonical_url, filter_results, unique_content
from search.types import SearchResult


def test_canonical_url_removes_only_identity_noise() -> None:
    assert (
        canonical_url("HTTP://WWW.Example.COM:80/Path/?b=2&utm_source=news&a=1#part")
        == "https://example.com/Path?a=1&b=2"
    )
    assert canonical_url("https://example.com/Path") != canonical_url("https://example.com/path")
    assert canonical_url("https://example.com:bad/path") == ""
    assert canonical_url("javascript:alert(1)") == ""


def test_unique_content_keeps_the_more_complete_version() -> None:
    assert unique_content(["Short passage", "Short passage with more detail", "  "]) == (
        "Short passage with more detail",
    )


def test_filters_enforce_domains_and_require_known_dates_for_date_constraints() -> None:
    results = (
        SearchResult("keep", "https://docs.example.com/guide", published_at="2026-08-20"),
        SearchResult("old", "https://docs.example.com/old", published_at="2025-01-01"),
        SearchResult("undated", "https://docs.example.com/new"),
        SearchResult("other", "https://other.example/guide", published_at="2026-08-20"),
    )

    filtered = filter_results(
        results,
        include_domains=("example.com",),
        exclude_domains=("docs.example.com/old",),
        start_date="2026-08-01",
    )

    assert [item.title for item in filtered] == ["keep"]
