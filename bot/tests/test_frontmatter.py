"""Direct tests for ``utils/frontmatter.py``.

Every operator fragment (guild, channel, thread, tool-config) is parsed through
``split_frontmatter``, and the loaders above it are fail-open by design: a
fragment this function mis-parses degrades silently rather than raising. That
makes its edge cases worth pinning here rather than only through a loader.

``split_frontmatter_strict`` is the same grammar with the opposite error
policy, used where a malformed document must stop startup instead.
"""

from __future__ import annotations

import pytest

from utils.frontmatter import FrontmatterError, split_frontmatter, split_frontmatter_strict


def test_absent_frontmatter_returns_empty_meta_and_stripped_body() -> None:
    meta, body = split_frontmatter("\n  just prose  \n")

    assert meta == {}
    assert body == "just prose"


def test_frontmatter_is_parsed_and_body_is_stripped() -> None:
    meta, body = split_frontmatter(
        "---\npinned_tools: [a, b]\nthread_handoff: true\n---\n\nBody text\n"
    )

    assert meta == {"pinned_tools": ["a", "b"], "thread_handoff": True}
    assert body == "Body text"


def test_malformed_yaml_degrades_to_empty_meta_without_raising() -> None:
    """Fail-open: the loaders treat unreadable frontmatter as 'nothing set'."""
    meta, body = split_frontmatter("---\npinned_tools: [unclosed\n---\nBody\n")

    assert meta == {}
    assert body == "Body"


def test_non_mapping_frontmatter_is_rejected_as_empty() -> None:
    """A bare list or scalar is not a settings mapping; ``.get`` must stay safe."""
    for header in ("- a\n- b", "just a string", "42"):
        meta, body = split_frontmatter(f"---\n{header}\n---\nBody\n")

        assert meta == {}, header
        assert body == "Body"


def test_empty_frontmatter_block_is_empty_meta() -> None:
    meta, body = split_frontmatter("---\n\n---\nBody\n")

    assert meta == {}
    assert body == "Body"


def test_delimiter_must_open_the_document() -> None:
    """A ``---`` further down is horizontal-rule prose, not a header."""
    text = "Intro\n---\npinned_tools: [a]\n---\nMore\n"

    meta, body = split_frontmatter(text)

    assert meta == {}
    assert body == text.strip()


def test_body_may_contain_further_delimiters() -> None:
    meta, body = split_frontmatter("---\nthread_handoff: false\n---\nBefore\n---\nAfter\n")

    assert meta == {"thread_handoff": False}
    assert body == "Before\n---\nAfter"


def test_frontmatter_only_document_has_an_empty_body() -> None:
    meta, body = split_frontmatter("---\nbot_active: true\n---\n")

    assert meta == {"bot_active": True}
    assert body == ""


def test_crlf_documents_are_parsed_rather_than_read_as_body() -> None:
    """A CRLF fragment used to fall through as body-only, reading as "unset"."""
    meta, body = split_frontmatter("---\r\npinned_tools: [a]\r\n---\r\nBody\r\n")

    assert meta == {"pinned_tools": ["a"]}
    assert body.strip() == "Body"


def test_trailing_whitespace_on_delimiters_is_tolerated() -> None:
    meta, _ = split_frontmatter("---  \nbot_active: true\n---\t\nBody\n")

    assert meta == {"bot_active": True}


def test_comment_only_frontmatter_is_no_overrides_not_malformed() -> None:
    """An operator commenting out every key means "set nothing", not "broken"."""
    meta, body = split_frontmatter("---\n# every key commented out\n---\nBody\n")

    assert meta == {}
    assert body == "Body"


def test_strict_raises_where_lenient_degrades() -> None:
    for text in (
        "---\npinned_tools: [unclosed\n---\nBody\n",
        "---\n- a\n- b\n---\nBody\n",
        "---\nnever closed\n",
    ):
        with pytest.raises(FrontmatterError):
            split_frontmatter_strict(text)

        meta, _ = split_frontmatter(text)
        assert meta == {}, text


def test_strict_keeps_the_body_unstripped() -> None:
    meta, body = split_frontmatter_strict("---\nbot_active: true\n---\nBody\n")

    assert meta == {"bot_active": True}
    assert body == "Body\n"


def test_strict_accepts_a_document_with_no_block() -> None:
    meta, body = split_frontmatter_strict("just prose\n")

    assert meta == {}
    assert body == "just prose\n"
