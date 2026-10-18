import httpx
import pytest

from daily_digest import fetch as fetch_module
from daily_digest.contracts import ErrorCode
from daily_digest.fetch import MAX_BYTES, MAX_REDIRECTS, FetchError, HttpFetcher

PUBLIC = "8.8.8.8"


def make_fetcher(handler, resolver=None, min_interval=0.0) -> HttpFetcher:
    return HttpFetcher(
        transport=httpx.MockTransport(handler),
        resolver=resolver or (lambda host, port: [PUBLIC]),
        min_interval=min_interval,
    )


def test_fetch_returns_status_and_body():
    fetcher = make_fetcher(lambda request: httpx.Response(200, content=b"hello"))
    assert fetcher.fetch("https://feeds.example.com/rss") == (200, b"hello")


def test_connection_is_pinned_to_the_checked_address():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    make_fetcher(handler).fetch("https://feeds.example.com/rss?x=1")
    request = seen[0]
    assert request.url.host == PUBLIC
    assert request.url.query == b"x=1"
    assert request.headers["Host"] == "feeds.example.com"
    assert request.extensions["sni_hostname"] == "feeds.example.com"


def test_sends_a_descriptive_user_agent():
    seen = []
    make_fetcher(lambda r: seen.append(r) or httpx.Response(200)).fetch("https://feeds.example.com/rss")
    assert seen[0].headers["User-Agent"].startswith("daily-digest/")


@pytest.mark.parametrize(
    "addresses",
    [[], ["127.0.0.1"], ["10.0.0.1"], ["::1"], [PUBLIC, "10.0.0.1"]],  # last: any private answer blocks
)
def test_hosts_resolving_to_non_public_addresses_are_blocked(addresses):
    requests = []
    fetcher = make_fetcher(lambda r: requests.append(r) or httpx.Response(200), lambda host, port: addresses)
    with pytest.raises(FetchError) as e:
        fetcher.fetch("https://feeds.example.com/rss")
    assert e.value.code == ErrorCode.UNSAFE_URL
    assert requests == []


def test_redirect_to_an_unsafe_host_is_blocked():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://internal.example.com/secret"})

    def resolver(host, port):
        return ["10.0.0.1"] if host == "internal.example.com" else [PUBLIC]

    with pytest.raises(FetchError) as e:
        make_fetcher(handler, resolver).fetch("https://feeds.example.com/rss")
    assert e.value.code == ErrorCode.UNSAFE_URL
    assert len(requests) == 1  # only the first hop was ever requested


def test_redirect_to_plain_http_is_blocked():
    def handler(request):
        return httpx.Response(302, headers={"Location": "http://feeds.example.com/rss"})

    with pytest.raises(FetchError) as e:
        make_fetcher(handler).fetch("https://feeds.example.com/rss")
    assert e.value.code == ErrorCode.UNSAFE_URL


def test_relative_redirects_are_followed():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new"})
        return httpx.Response(200, content=b"moved")

    assert make_fetcher(handler).fetch("https://feeds.example.com/old") == (200, b"moved")


def test_redirect_loop_is_cut_off():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://feeds.example.com/again"})

    with pytest.raises(FetchError, match="too many redirects"):
        make_fetcher(handler).fetch("https://feeds.example.com/rss")
    assert len(requests) == MAX_REDIRECTS + 1


def test_oversized_response_is_rejected():
    fetcher = make_fetcher(lambda r: httpx.Response(200, content=b"x" * (MAX_BYTES + 1)))
    with pytest.raises(FetchError) as e:
        fetcher.fetch("https://feeds.example.com/rss")
    assert e.value.code == ErrorCode.RESPONSE_TOO_LARGE


def test_network_errors_become_fetch_errors():
    def handler(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(FetchError) as e:
        make_fetcher(handler).fetch("https://feeds.example.com/rss")
    assert e.value.code == ErrorCode.FEED_UNREACHABLE


def test_requests_to_the_same_host_are_spaced_out(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fetch_module.time, "sleep", sleeps.append)
    fetcher = make_fetcher(lambda r: httpx.Response(200), min_interval=1.0)
    fetcher.fetch("https://feeds.example.com/a")
    fetcher.fetch("https://feeds.example.com/b")
    assert len(sleeps) == 1 and 0.5 < sleeps[0] <= 1.0


def test_retry_after_stops_further_requests_to_that_host():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"})

    fetcher = make_fetcher(handler)
    assert fetcher.fetch("https://feeds.example.com/a")[0] == 429
    with pytest.raises(FetchError, match="back off"):
        fetcher.fetch("https://feeds.example.com/b")
    assert len(requests) == 1
    assert fetcher.fetch("https://other.example.com/a")[0] == 429  # other hosts are unaffected
