"""Turn feed text into clean, comparable plain text."""

import hashlib
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_EXTRACT_CHARS = 1200

# Query parameters that only track where a click came from.
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid"}


def canonicalize_url(url: str) -> str:
    """Normalize a URL so that the same article compares equal."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in ("p", "br", "li", "div", "h1", "h2", "h3", "h4"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def strip_html(html: str) -> str:
    """HTML to plain text with collapsed whitespace."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    text = unescape("".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()


def make_extract(text: str, limit: int = MAX_EXTRACT_CHARS) -> str:
    """Truncate to at most `limit` characters, ellipsis included, on a word boundary if possible."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    # Text without spaces (e.g. Chinese or Japanese) has no word boundary to cut at.
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:-") + "…"


def content_hash(title: str, body: str) -> str:
    """Stable hash used to catch the same story published at a different URL."""
    normalized = re.sub(r"\s+", " ", f"{title}\n{body}".lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def item_id(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:16]


MIN_FEED_TEXT_CHARS = 300

# Endings that mean "the rest is on the website". Add a pattern here to teach the app a new one.
_TEASER_ENDINGS = [
    re.compile(r"(\.\.\.|…|\[\s*(\.\.\.|…)\s*\])$"),
    re.compile(r"\b(read more|continue reading|read the full (article|story)|click to read)\b[^.]{0,40}$", re.I),
    re.compile(r"\bthe post\b.{1,200}\bappeared first on\b.{1,100}$", re.I),
]


def needs_extraction(feed_text: str) -> bool:
    """True if the article is worth fetching: the feed text is short, reads like a teaser,
    or is longer than MAX_EXTRACT_CHARS (so make_extract cuts it mid-article).

    Pass the full feed body, not the extract.
    """
    text = feed_text.strip()
    return (
        len(text) < MIN_FEED_TEXT_CHARS
        or len(text) > MAX_EXTRACT_CHARS
        or any(p.search(text) for p in _TEASER_ENDINGS)
    )
