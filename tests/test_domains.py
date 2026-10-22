import logging
import textwrap
from datetime import datetime, timezone

import httpx
import pytest

from daily_digest.config import SettingsError, load_sources
from daily_digest.contracts import ErrorCode, Source
from daily_digest.domains import (
    allowed_domains_for,
    host_allowed,
    normalize_domain,
    parse_host,
    registrable_domain,
    safe_host,
)
from daily_digest.extract import extract_articles
from daily_digest.fetch import HttpFetcher
from daily_digest.ingest import ingest
from daily_digest.robots import RobotsChecker
from daily_digest.store import Store
from helpers import guarded_fetch, sample_item
from test_extract import PAGE, allow_all
from test_ingest import allow_all as robots_allow_all
from test_ingest import feed_xml

CIO = ["cio.com"]


# --- host matching ---------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://cio.com/a", "https://www.cio.com/a", "https://WWW.CIO.COM/a", "http://news.cio.com:80/a",
    "https://cio.com:443/a", "https://cio.com./a", "https://a.b.cio.com/a",
])
def test_the_domain_and_its_subdomains_match_in_any_case(url):
    assert host_allowed(url, CIO)


@pytest.mark.parametrize("url", [
    "https://evil-cio.com/a",           # a longer name that ends the same way
    "https://cio.com.evil.com/a",       # the domain used as a prefix
    "https://notcio.com/a",
    "https://evil.com/cio.com",         # the domain only in the path
    "https://evil.com/?u=cio.com",
    "https://cio.com@evil.com/a",       # userinfo: the host is evil.com
    "https://user@cio.com@evil.com/a",
    "https://evil.com\\@cio.com/a",
    "https://cio.com:8443/a",           # a non-standard port
    "https://cio.com:22/a",
    "https://www.cio.com:8080/a",
    "ftp://cio.com/a",
    "file://cio.com/etc/passwd",
    "javascript:alert(1)",
    "https:///a",
    "//cio.com/a",
    "https://%63io.com.evil.com/a",
    "https://1.2.3.4/a",
    "https://[::1]/a",
    "https://evil.com\\.cio.com/a",      # a backslash is a slash to a browser, so the host is evil.com
    "https://evil.com;.cio.com/a",
    "https://evil.com!.cio.com/a",
    "https://evil.com%2f.cio.com/a",
    "https://evil.com .cio.com/a",
    "https://-evil.cio.com/a",
    "https://a..cio.com/a",
])
def test_lookalike_hosts_userinfo_ports_and_other_schemes_do_not_match(url):
    assert not host_allowed(url, CIO)


def test_internationalised_hosts_are_compared_in_their_ascii_form():
    assert parse_host("https://bücher.example/a") == "xn--bcher-kva.example"
    assert host_allowed("https://www.bücher.example/a", ["xn--bcher-kva.example"])
    assert host_allowed("https://bücher.example/a", [normalize_domain("BÜCHER.example")])
    assert not host_allowed("https://xn--bcher-kva.example.evil.com/a", ["xn--bcher-kva.example"])


def test_ordinary_hosts_with_underscores_digits_and_hyphens_still_match():
    assert host_allowed("https://my_feed-2.cio.com/a", CIO) and host_allowed("https://1.cio.com/a", CIO)


def test_an_unparseable_url_is_not_allowed_and_not_a_crash():
    for url in ("", "https://[bad", "http://", "https://exa mple.com/", "https://cio.com:notaport/"):
        assert not host_allowed(url, CIO)


# --- the default list and configured entries ------------------------------------------

@pytest.mark.parametrize("host, domain", [
    ("www.cio.com", "cio.com"), ("sloanreview.mit.edu", "mit.edu"), ("cloudblog.withgoogle.com", "withgoogle.com"),
    ("news.bbc.co.uk", "bbc.co.uk"), ("example.com", "example.com"), ("a.b.c.example.com.au", "example.com.au"),
])
def test_the_default_is_the_feeds_registrable_domain(host, domain):
    assert registrable_domain(host) == domain
    source = Source(name="f", url=f"https://{host}/feed", category="c")
    assert allowed_domains_for(source) == (domain,)


def test_listed_domains_replace_the_default_and_a_feed_with_a_bad_url_allows_nothing():
    assert allowed_domains_for(Source(name="f", url="https://a.example.com/x", category="c", allowed_domains=["Other.org"])) == ("other.org",)
    assert allowed_domains_for(Source(name="f", url="https://user@a.example.com/x", category="c")) == ()


def test_the_five_starter_feeds_allow_the_links_found_in_real_data():
    """The hosts below are the ones the starter feeds' stored items actually link to."""
    seen = {
        "CIO.com AI": "https://www.cio.com/article/1/x.html",
        "Computerworld generative AI": "https://www.computerworld.com/article/2/y.html",
        "Google Cloud AI blog": "https://cloud.google.com/blog/products/ai-machine-learning/z",
        "MIT Sloan Management Review AI": "https://sloanreview.mit.edu/article/w/",
        "MIT Technology Review AI": "https://www.technologyreview.com/2026/10/01/v/",
    }
    sources = {s.name: s for s in load_sources()}
    assert set(seen) == set(sources)
    for name, link in seen.items():
        assert host_allowed(link, allowed_domains_for(sources[name])), name
    assert not host_allowed("https://evil.example.org/x", allowed_domains_for(sources["CIO.com AI"]))


@pytest.mark.parametrize("entry", ["", "  ", "https://cio.com", "cio.com/path", "cio.com:443", "*.cio.com", "user@cio.com",
                                   "cio", "1.2.3.4", "cio..com", "-cio.com", "cio com", "cio.com?x"])
def test_a_malformed_allowed_domains_entry_is_rejected(entry):
    with pytest.raises(ValueError):
        normalize_domain(entry)


def test_a_malformed_allowed_domains_value_fails_startup_naming_the_feed_and_the_key(tmp_path):
    path = tmp_path / "feeds.toml"
    path.write_text(textwrap.dedent('''
        [[feeds]]
        name = "Fine"
        url = "https://a.example.com/feed"
        category = "c"

        [[feeds]]
        name = "Broken Feed"
        url = "https://b.example.com/feed"
        category = "c"
        allowed_domains = ["good.example.com", "https://bad.example.com/path"]
    '''))
    with pytest.raises(SettingsError) as caught:
        load_sources(path)
    message = str(caught.value)
    assert "'Broken Feed'" in message and "allowed_domains: entry 2: must be a bare domain" in message and "bad.example.com" not in message
    path.write_text(path.read_text().replace('["good.example.com", "https://bad.example.com/path"]', '"not-a-list"'))
    with pytest.raises(SettingsError, match="Broken Feed.*allowed_domains"):
        load_sources(path)


def test_a_feeds_file_with_a_row_that_is_not_a_table_fails_with_a_clear_message(tmp_path):
    path = tmp_path / "feeds.toml"
    path.write_text('feeds = ["not a table"]\n')
    with pytest.raises(SettingsError, match="invalid feeds file feeds.toml"):
        load_sources(path)


def test_two_feeds_with_the_same_name_are_rejected_because_items_are_matched_to_their_feed_by_name(tmp_path):
    path = tmp_path / "feeds.toml"
    block = '[[feeds]]\nname = "Same"\nurl = "https://{}.example.com/feed"\ncategory = "c"\n'
    path.write_text(block.format("a") + block.format("b"))
    with pytest.raises(SettingsError, match="'Same': the name is used by more than one feed"):
        load_sources(path)


def test_a_disabled_feed_is_loaded_on_request_so_its_stored_items_keep_their_domains(tmp_path):
    path = tmp_path / "feeds.toml"
    path.write_text('[[feeds]]\nname = "Off"\nurl = "https://a.example.com/feed"\ncategory = "c"\nenabled = false\n')
    assert load_sources(path) == [] and [s.name for s in load_sources(path, include_disabled=True)] == ["Off"]


# --- ingest ----------------------------------------------------------------------------

def feed_with_links(*links: str) -> bytes:
    return feed_xml(*[(f"Story {n}", link, "text") for n, link in enumerate(links)])


def run_ingest(source: Source, body: bytes, caplog=None):
    store = Store(":memory:")
    reports = ingest([source], lambda url: (200, body), robots_allow_all(), store)
    return store, reports[0]


def test_an_item_linking_off_the_feeds_domain_is_dropped_counted_and_logged(caplog):
    source = Source(name="Wire", url="https://www.cio.com/feed", category="c")
    body = feed_with_links("https://www.cio.com/a", "https://evil.example.org/b", "https://news.cio.com/c")
    with caplog.at_level(logging.WARNING, logger="daily_digest.ingest"):
        store, report = run_ingest(source, body)
    assert (report.entries, report.stored, report.blocked) == (3, 2, 1)
    assert sorted(i.canonical_url for i in store.items_published_between(
        datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc))) == ["https://news.cio.com/c", "https://www.cio.com/a"]
    assert [r.getMessage() for r in caplog.records] == ["DOMAIN_NOT_ALLOWED feed=Wire host=evil.example.org"]


def test_the_same_item_is_kept_once_its_domain_is_listed():
    body = feed_with_links("https://www.cio.com/a", "https://evil.example.org/b")
    _, report = run_ingest(Source(name="Wire", url="https://www.cio.com/feed", category="c", allowed_domains=["cio.com", "evil.example.org"]), body)
    assert (report.stored, report.blocked) == (2, 0)


def test_the_logged_host_is_clipped_and_free_of_control_characters_and_nothing_else_is_logged(caplog):
    host = "a" * 200 + ".example.org"
    body = feed_with_links(f"https://{host}/SECRET-PATH-TEXT")
    with caplog.at_level(logging.WARNING, logger="daily_digest.ingest"):
        run_ingest(Source(name="Wire\x1b[31m", url="https://www.cio.com/feed", category="c"), body)
    line = caplog.records[0].getMessage()
    assert "\x1b" not in line and "SECRET-PATH-TEXT" not in line and "Story" not in line and len(line) < 200 and "…" in line
    assert safe_host("https://exa\x07mple.org/x") == "exa?mple.org"


def test_a_userinfo_link_is_dropped_at_ingest():
    body = feed_with_links("https://www.cio.com@evil.example.org/a")
    _, report = run_ingest(Source(name="Wire", url="https://www.cio.com/feed", category="c"), body)
    assert (report.stored, report.blocked) == (0, 1)


# --- extract ---------------------------------------------------------------------------

def store_with_link(url: str, source="s") -> Store:
    store = Store(":memory:")
    store.add_if_new(sample_item("a").model_copy(update={"canonical_url": url, "source": source}), needs_extraction=True)
    return store


FEED = Source(name="s", url="https://www.cio.com/feed", category="c")


def test_extract_refuses_an_article_url_off_the_list_and_keeps_the_feed_text(caplog):
    store = store_with_link("https://evil.example.org/page")
    fetch = guarded_fetch(lambda url: (200, PAGE))
    with caplog.at_level(logging.WARNING, logger="daily_digest.extract"):
        report = extract_articles(store, fetch, allow_all(), sources=[FEED])
    assert report.failures == {str(ErrorCode.DOMAIN_NOT_ALLOWED): 1} and fetch.calls == []
    assert store.pending_extraction() == []  # final: not tried again
    assert [r.getMessage() for r in caplog.records] == ["DOMAIN_NOT_ALLOWED feed=s host=evil.example.org"]


def test_extract_makes_no_dns_lookup_or_request_for_a_host_off_the_list():
    store = store_with_link("https://evil.example.org/page")
    lookups, requests = [], []
    with HttpFetcher(
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200, content=PAGE)),
        resolver=lambda host, port: lookups.append(host) or ["8.8.8.8"], min_interval=0.0,
    ) as fetcher:
        report = extract_articles(store, fetcher.fetch, RobotsChecker(fetcher.fetch, "test"), sources=[FEED])
    assert report.failures == {str(ErrorCode.DOMAIN_NOT_ALLOWED): 1} and lookups == [] and requests == []


def test_a_redirect_to_a_host_off_the_list_is_final_and_that_host_is_never_contacted():
    store = store_with_link("https://www.cio.com/article")
    contacted, lookups = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append((request.headers["Host"], request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://evil.example.org/landing"})

    with HttpFetcher(
        transport=httpx.MockTransport(handler), resolver=lambda host, port: lookups.append(host) or ["8.8.8.8"], min_interval=0.0,
    ) as fetcher:
        report = extract_articles(store, fetcher.fetch, RobotsChecker(fetcher.fetch, "test"), sources=[FEED])
    assert report.failures == {str(ErrorCode.DOMAIN_NOT_ALLOWED): 1} and report.extracted == 0
    assert all(host == "www.cio.com" for host, _ in contacted) and set(lookups) == {"www.cio.com"}
    assert store.pending_extraction() == []


def test_a_redirect_to_another_host_on_the_list_is_followed():
    store = store_with_link("https://www.cio.com/article")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.headers["Host"] == "www.cio.com":
            return httpx.Response(301, headers={"Location": "https://news.cio.com/article"})
        return httpx.Response(200, content=PAGE)

    with HttpFetcher(transport=httpx.MockTransport(handler), resolver=lambda h, p: ["8.8.8.8"], min_interval=0.0) as fetcher:
        report = extract_articles(store, fetcher.fetch, RobotsChecker(fetcher.fetch, "test"), sources=[FEED])
    assert report.extracted == 1


def test_an_item_whose_feed_left_the_list_is_not_fetched_and_no_attempt_is_used_up():
    store = store_with_link("https://www.cio.com/article", source="Removed feed")
    fetch = guarded_fetch(lambda url: (200, PAGE))
    for _ in range(5):  # more runs than there are attempts
        report = extract_articles(store, fetch, allow_all(), sources=[FEED])
        assert report.failures == {str(ErrorCode.DOMAIN_NOT_ALLOWED): 1}
    assert fetch.calls == [] and len(store.pending_extraction()) == 1
    assert extract_articles(store, fetch, allow_all(), sources=[FEED.model_copy(update={"name": "Removed feed"})]).extracted == 1


def test_a_robots_txt_that_redirects_off_the_list_is_not_followed_and_its_host_is_never_looked_up():
    store = store_with_link("https://www.cio.com/article")
    contacted, lookups = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        contacted.append((request.headers["Host"], request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(302, headers={"Location": "https://evil.example.org/robots.txt"})
        return httpx.Response(200, content=PAGE)

    with HttpFetcher(transport=httpx.MockTransport(handler), resolver=lambda h, p: lookups.append(h) or ["8.8.8.8"], min_interval=0.0) as fetcher:
        robots = RobotsChecker(fetcher.fetch, "test", confine_redirects=True)
        report = extract_articles(store, fetcher.fetch, robots, sources=[FEED])
    assert report.extracted == 0 and all(host == "www.cio.com" for host, _ in contacted) and set(lookups) == {"www.cio.com"}


def test_a_robots_txt_may_still_redirect_within_its_own_domain():
    store = store_with_link("https://www.cio.com/article")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt" and request.headers["Host"] == "www.cio.com":
            return httpx.Response(301, headers={"Location": "https://cio.com/robots.txt"})
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=PAGE)

    with HttpFetcher(transport=httpx.MockTransport(handler), resolver=lambda h, p: ["8.8.8.8"], min_interval=0.0) as fetcher:
        robots = RobotsChecker(fetcher.fetch, "test", confine_redirects=True)
        assert extract_articles(store, fetcher.fetch, robots, sources=[FEED]).extracted == 1
