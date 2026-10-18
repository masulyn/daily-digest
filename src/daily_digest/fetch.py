"""HTTP fetching for untrusted URLs.

Every hop, redirects included, must resolve to public addresses, and the connection
goes to the exact address that was checked, so a host cannot answer DNS differently
the second time (DNS rebinding).
"""

import ipaddress
import os
import socket
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .contracts import ErrorCode

MAX_REDIRECTS = 5
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 15.0
DNS_TIMEOUT_SECONDS = 5.0
MIN_SECONDS_BETWEEN_REQUESTS = 1.0
DEFAULT_RETRY_AFTER_SECONDS = 60
MAX_RETRY_AFTER_SECONDS = 3600

# fetch(url) -> (status code, body bytes)
FetchFn = Callable[[str], tuple[int, bytes]]
# resolve(host, port) -> address strings; empty if the host cannot be resolved
Resolver = Callable[[str, int], list[str]]


class FetchError(Exception):
    """A fetch failed. `code` is one of the shared error codes."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def user_agent() -> str:
    contact = os.environ.get("DIGEST_CONTACT", "contact-not-set")
    return f"daily-digest/0.1 (personal research project; {contact})"


def resolve_addresses(host: str, port: int) -> list[str]:
    # getaddrinfo has no timeout of its own, so run it in a thread we can stop waiting on.
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        infos = pool.submit(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP).result(
            timeout=DNS_TIMEOUT_SECONDS
        )
    except (socket.gaierror, TimeoutError):
        return []
    finally:
        pool.shutdown(wait=False)
    return [info[4][0] for info in infos]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 must be judged as 127.0.0.1
    return ip.is_global


def _public_address(url: str, resolve: Resolver) -> str | None:
    """The address to connect to, or None unless this is an https URL on public addresses only."""
    parts = urlsplit(url)
    try:
        port = parts.port or 443
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.hostname:
        return None
    addresses = resolve(parts.hostname, port)
    if not addresses or not all(_is_public(a) for a in addresses):
        return None
    return addresses[0]


def is_safe_url(url: str) -> bool:
    return _public_address(url, resolve_addresses) is not None


def _pin_to_address(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Rewrite the URL to the checked address, keeping the original host for HTTP and TLS."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    ip_host = f"[{address}]" if ":" in address else address
    pinned = urlunsplit((parts.scheme, ip_host + port, parts.path or "/", parts.query, ""))
    return pinned, {"Host": parts.hostname + port}, {"sni_hostname": parts.hostname}


class HttpFetcher:
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = resolve_addresses,
        min_interval: float = MIN_SECONDS_BETWEEN_REQUESTS,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent()},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,  # we follow redirects ourselves so each hop is checked
            trust_env=False,  # an environment proxy would resolve hosts itself, skipping our check
            transport=transport,
        )
        self._resolve = resolver
        self._min_interval = min_interval
        self._last_request: dict[str, float] = {}
        self._backoff_until: dict[str, float] = {}

    def __enter__(self) -> "HttpFetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _wait_for_turn(self, host: str) -> None:
        if time.monotonic() < self._backoff_until.get(host, 0.0):
            raise FetchError(ErrorCode.FEED_UNREACHABLE, f"{host} asked us to back off")
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request[host] = time.monotonic()

    def _note_retry_after(self, host: str, response: httpx.Response) -> None:
        try:
            seconds = int(response.headers.get("retry-after", ""))
        except ValueError:
            seconds = DEFAULT_RETRY_AFTER_SECONDS  # HTTP-date form is not parsed
        seconds = min(max(seconds, 0), MAX_RETRY_AFTER_SECONDS)
        self._backoff_until[host] = time.monotonic() + seconds

    def fetch(self, url: str) -> tuple[int, bytes]:
        """GET a URL. Returns (status code, body). Raises FetchError on unsafe or failed fetches."""
        for _ in range(MAX_REDIRECTS + 1):
            address = _public_address(url, self._resolve)
            if address is None:
                raise FetchError(ErrorCode.UNSAFE_URL, f"blocked unsafe URL: {url}")
            host = urlsplit(url).hostname
            self._wait_for_turn(host)
            pinned, headers, extensions = _pin_to_address(url, address)
            try:
                with self._client.stream("GET", pinned, headers=headers, extensions=extensions) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise FetchError(ErrorCode.FEED_UNREACHABLE, "redirect without location")
                        url = urljoin(url, location)
                        continue
                    if response.status_code in (429, 503):
                        self._note_retry_after(host, response)
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise FetchError(ErrorCode.RESPONSE_TOO_LARGE, f"response over {MAX_BYTES} bytes")
                    return response.status_code, bytes(body)
            except httpx.HTTPError as e:
                raise FetchError(ErrorCode.FEED_UNREACHABLE, str(e)) from e
        raise FetchError(ErrorCode.FEED_UNREACHABLE, "too many redirects")
