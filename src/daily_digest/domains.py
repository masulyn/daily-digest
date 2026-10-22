"""Which hosts a feed's links may point to.

Links in a feed are untrusted: a hostile or compromised feed could point the article fetch at any
site, or put any link into a digest's Sources. Each feed therefore has a list of allowed domains, and
a link is followed or published only if its host is one of them or a subdomain of one.
"""

import ipaddress
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # contracts imports this module for its validators
    from .contracts import CandidateItem, Source

MAX_SAFE_HOST_CHARS = 80
_PORTS = (None, 80, 443)
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
# Two-label public suffixes, so "news.bbc.co.uk" gives "bbc.co.uk" and not "co.uk". There is no public
# suffix list here (no new dependency); a feed on a suffix not listed should name its domains in
# `allowed_domains`, which always wins.
_TWO_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "edu.au", "co.nz", "co.jp", "ne.jp",
    "or.jp", "com.br", "com.cn", "com.hk", "com.sg", "com.tw", "co.in", "co.kr", "co.za", "com.mx", "com.tr",
})


def parse_host(url: str) -> str | None:
    """The host of an http(s) URL in lowercase ASCII, or None for anything that is not a plain one.

    No userinfo, no port other than 80 or 443, no empty or invalid host. Internationalised names are
    compared in their ASCII (punycode) form.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or port not in _PORTS:
        return None
    host = (parts.hostname or "").rstrip(".")
    if not host or any(c.isspace() or not c.isprintable() for c in host):
        return None
    try:
        return host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None


def registrable_domain(host: str) -> str:
    """The host's registrable domain: its last two labels, or three under a known two-label suffix."""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    keep = 3 if ".".join(labels[-2:]) in _TWO_LABEL_SUFFIXES else 2
    return ".".join(labels[-keep:])


def normalize_domain(entry: str) -> str:
    """One `allowed_domains` entry as a lowercase ASCII domain. Raises ValueError saying what is wrong."""
    text = entry.strip().lower().rstrip(".")
    if not text:
        raise ValueError("an empty domain")
    if any(c in text for c in "/:@*?#\\ \t") or not text.isprintable():
        raise ValueError("must be a bare domain such as example.com: no scheme, path, port, wildcard or spaces")
    try:
        ascii_text = text.encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("not a valid domain name") from None
    labels = ascii_text.split(".")
    if len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise ValueError("must be a domain with at least two labels, such as example.com")
    try:
        ipaddress.ip_address(ascii_text)
    except ValueError:
        return ascii_text
    raise ValueError("an IP address is not a domain")


def allowed_domains_for(source: "Source") -> tuple[str, ...]:
    """The feed's own `allowed_domains`, or by default its registrable domain (and so its subdomains)."""
    if source.allowed_domains is not None:
        return tuple(source.allowed_domains)
    host = parse_host(source.url)
    return (registrable_domain(host),) if host else ()


def allowed_by_source(sources: "Iterable[Source]") -> dict[str, tuple[str, ...]]:
    return {source.name: allowed_domains_for(source) for source in sources}


def host_allowed(url: str, allowed: Iterable[str]) -> bool:
    host = parse_host(url)
    return host is not None and any(host == d or host.endswith("." + d) for d in allowed)


def item_link_allowed(item: "CandidateItem", allowed: Mapping[str, Iterable[str]]) -> bool:
    """False for an item whose feed is unknown or whose link is off that feed's list."""
    return item.source in allowed and host_allowed(item.canonical_url, allowed[item.source])


def safe_text(text: str) -> str:
    """Text for a log line: control characters removed and clipped."""
    clean = "".join(c if c.isprintable() else "?" for c in text)
    return clean if len(clean) <= MAX_SAFE_HOST_CHARS else clean[: MAX_SAFE_HOST_CHARS - 1] + "…"


def safe_host(url: str) -> str:
    """The URL's host for a log line. Nothing else of the URL."""
    try:
        return safe_text(urlsplit(url).hostname or "")
    except ValueError:
        return ""
