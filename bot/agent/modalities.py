from __future__ import annotations

import re

_IMAGE_OUTPUT_RE = re.compile(
    r"\b(generate|draw|create|make|render|paint|illustrate)\b.{0,48}"
    r"\b(image|picture|photo|illustration|drawing|wallpaper|avatar|icon)\b|"
    r"\b(image|picture|photo|illustration|drawing|wallpaper|avatar|icon)\b.{0,48}"
    r"\b(generate|draw|create|make|render|paint|illustrate)\b",
    re.IGNORECASE,
)
_VISUAL_CREATION_VERB_RE = re.compile(
    r"\b(draw|render|paint|illustrate)\b",
    re.IGNORECASE,
)


def wants_image_output(text: str) -> bool:
    return bool(_IMAGE_OUTPUT_RE.search(text) or _VISUAL_CREATION_VERB_RE.search(text))
