from __future__ import annotations

from datetime import date
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from search.types import SearchResult

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "ref_src",
}


def clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = "\n".join(_SPACE_RE.sub(" ", line).strip() for line in text.splitlines())
    return _BLANK_LINES_RE.sub("\n\n", text).strip()


def unique_content(values: list[object]) -> tuple[str, ...]:
    kept: list[str] = []
    normalized: list[str] = []
    for raw in values:
        value = clean_text(raw)
        if not value:
            continue
        folded = " ".join(value.casefold().split())
        if any(folded == prior or folded in prior for prior in normalized):
            continue
        kept = [item for item, prior in zip(kept, normalized, strict=True) if prior not in folded]
        normalized = [prior for prior in normalized if prior not in folded]
        kept.append(value)
        normalized.append(folded)
    return tuple(kept)


def canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parsed.port
    except ValueError:
        return ""
    if port is not None and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
    ]
    query.sort()
    return urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))


def filter_results(
    results: tuple[SearchResult, ...],
    *,
    include_domains: tuple[str, ...] = (),
    exclude_domains: tuple[str, ...] = (),
    start_date: str | None = None,
    end_date: str | None = None,
) -> tuple[SearchResult, ...]:
    kept: list[SearchResult] = []
    for result in results:
        if include_domains and not any(
            _domain_matches(result.url, item) for item in include_domains
        ):
            continue
        if exclude_domains and any(_domain_matches(result.url, item) for item in exclude_domains):
            continue
        published = _published_date(result.published_at)
        if start_date and (published is None or published < date.fromisoformat(start_date)):
            continue
        if end_date and (published is None or published > date.fromisoformat(end_date)):
            continue
        kept.append(result)
    return tuple(kept)


def _domain_matches(url: str, pattern: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    raw = pattern.strip().casefold()
    if "://" in raw:
        candidate = urlsplit(raw)
        raw = (candidate.hostname or "") + candidate.path
    raw = raw.removeprefix("www.").rstrip("/")
    domain, _, path = raw.partition("/")
    if domain.startswith("*."):
        host_match = host.endswith(domain[1:]) and host != domain[2:]
    else:
        host_match = host == domain or host.endswith(f".{domain}")
    return host_match and (not path or parsed.path.startswith(f"/{path}"))


def _published_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
