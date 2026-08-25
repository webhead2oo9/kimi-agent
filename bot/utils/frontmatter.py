"""The one YAML-frontmatter reader for `---`-delimited documents.

Operator config fragments, skill documents, and prompt fragments all carry a
`---` header. This module owns the delimiter grammar so they agree on what a
frontmatter block *is*; what to do about a malformed one stays with the caller,
because that genuinely differs:

- Tool config and tool policy fail **open**: an unreadable fragment must not
  take a tool offline, so they catch and fall back to last-known-good.
- Operator and plugin settings fail **closed**: a malformed overlay stops
  startup rather than booting on a half-applied config.
- Skill loading is lenient: a bad header degrades the document to plain
  markdown instead of hiding the skill.

So the split here is `split_frontmatter` (lenient, returns empty metadata) vs
`split_frontmatter_strict` (raises `FrontmatterError`), over shared primitives.

The delimiter grammar tolerates CRLF and trailing spaces/tabs on the `---`
lines. Some callers previously used a stricter regex that silently treated a
CRLF document as body-only, which read as "this operator set nothing".
"""

from __future__ import annotations

import re
from typing import Any

import yaml  # type: ignore[import-untyped]

__all__ = [
    "FrontmatterError",
    "find_frontmatter",
    "parse_frontmatter_text",
    "split_frontmatter",
    "split_frontmatter_strict",
]

_OPENING_RE = re.compile(r"\A---[ \t]*\r?\n")
_CLOSING_RE = re.compile(r"^---[ \t]*(?:\r?\n|\Z)", re.MULTILINE)
# A document that is only an opening delimiter: an unclosed block, not a body.
_BARE_OPENING_RE = re.compile(r"\A---[ \t]*\r?\Z")


class FrontmatterError(ValueError):
    """The document opens a frontmatter block it does not close or parse."""


def find_frontmatter(text: str) -> tuple[str, str] | None:
    """Split raw frontmatter from body, or `None` when there is no block.

    Raises `FrontmatterError` when a block is opened but never closed; that is
    a malformed document, never a body that happens to start with `---`.
    """

    opening = _OPENING_RE.match(text)
    if opening is None:
        if _BARE_OPENING_RE.match(text):
            raise FrontmatterError("YAML frontmatter is not closed")
        return None

    closing = _CLOSING_RE.search(text, opening.end())
    if closing is None:
        raise FrontmatterError("YAML frontmatter is not closed")

    return text[opening.end() : closing.start()], text[closing.end() :]


def parse_frontmatter_text(raw: str) -> Any:
    """YAML-load one frontmatter block, without requiring a mapping.

    A block holding only comments or blank lines is an empty mapping rather
    than `None`: an operator commenting out every key means "no overrides",
    not "malformed".
    """

    try:
        meta: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FrontmatterError("invalid YAML frontmatter") from exc

    if meta is None:
        comment_or_blank_only = all(
            not line.strip() or line.lstrip().startswith("#") for line in raw.splitlines()
        )
        if comment_or_blank_only:
            return {}
    return meta


def split_frontmatter_strict(text: str) -> tuple[dict[str, Any], str]:
    """Return `(frontmatter, body)`, raising `FrontmatterError` on anything bad.

    A document with no block yields empty metadata and the whole text as body;
    a block that is present must be closed, parse, and be a mapping.
    """

    found = find_frontmatter(text)
    if found is None:
        return {}, text

    raw, body = found
    meta = parse_frontmatter_text(raw)
    if not isinstance(meta, dict):
        raise FrontmatterError("YAML frontmatter must be a mapping")
    return meta, body


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Lenient `(frontmatter, body)`; anything malformed yields `({}, body)`.

    The body is stripped, matching how prompt and config fragments are
    rendered.
    """

    try:
        found = find_frontmatter(text)
    except FrontmatterError:
        return {}, text.strip()
    if found is None:
        return {}, text.strip()

    raw, body = found
    try:
        meta = parse_frontmatter_text(raw)
    except FrontmatterError:
        return {}, body.strip()
    if not isinstance(meta, dict):
        return {}, body.strip()
    return meta, body.strip()
