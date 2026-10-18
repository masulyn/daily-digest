"""Polite, safe HTTP fetching (NFR-2, NFR-3).

Rules enforced here: https only, no private/loopback/link-local addresses (checked
before every request and every redirect), redirects capped, response size capped,
timeouts, one request per second per domain, and a descriptive User-Agent.
"""

import ipaddress
import os
import socket
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .contracts import ErrorCode

MAX_REDIRECTS = 5
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.0


class FetchError(Exception):
    """A fetch failed. `code` is one of the shared error codes."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def user_agent() -> str:
    contact = os.environ.get("DIGEST_CONTACT", "contact-not-set")
    return f"daily-digest/0.1 (personal research project; {contact})"


def is_safe_url(url: str) -> bool:
    """True only for https URLs whose host resolves to public addresses."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


class HttpFetcher:
    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,  # we follow redirects ourselves so each hop is checked
        )
        self._last_request: dict[str, float] = {}

    def _wait_for_turn(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)
        self._last_request[host] = time.monotonic()

    def fetch(self, url: str) -> tuple[int, bytes]:
        """GET a URL. Returns (status code, body). Raises FetchError on unsafe or failed fetches."""
        for _ in range(MAX_REDIRECTS + 1):
            if not is_safe_url(url):
                raise FetchError(ErrorCode.UNSAFE_URL, f"blocked unsafe URL: {url}")
            self._wait_for_turn(urlsplit(url).hostname or "")
            try:
                with self._client.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(ErrorCode.FEED_UNREACHABLE, "redirect without location")
                        url = urljoin(url, location)
                        continue
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise FetchError(ErrorCode.FEED_INVALID, "response too large")
                    return response.status_code, bytes(body)
            except httpx.HTTPError as e:
                raise FetchError(ErrorCode.FEED_UNREACHABLE, str(e)) from e
        raise FetchError(ErrorCode.FEED_UNREACHABLE, "too many redirects")
