import pytest

from daily_digest.config import data_dir, load_sources
from daily_digest.contracts import ErrorCode
from daily_digest.fetch import FetchError, is_safe_url
from daily_digest.robots import RobotsChecker

UA = "daily-digest-test"


def checker(status: int, body: str = "", raises: bool = False) -> RobotsChecker:
    def fetch(url: str):
        if raises:
            raise FetchError(ErrorCode.FEED_UNREACHABLE, "network down")
        return status, body.encode()

    return RobotsChecker(fetch, UA)


def test_robots_disallow_rule_is_respected():
    c = checker(200, "User-agent: *\nDisallow: /private/")
    assert c.allowed("https://example.com/feed")
    assert not c.allowed("https://example.com/private/feed")


def test_robots_403_means_unrestricted():
    # Python's built-in parser would wrongly treat this as "disallow everything".
    assert checker(403).allowed("https://example.com/feed")


def test_robots_404_means_unrestricted():
    assert checker(404).allowed("https://example.com/feed")


@pytest.mark.parametrize("status", [429, 500, 503])
def test_robots_rate_limit_or_server_error_is_cautious(status):
    assert not checker(status).allowed("https://example.com/feed")


def test_robots_unreachable_is_cautious():
    assert not checker(0, raises=True).allowed("https://example.com/feed")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/feed",  # not https
        "https://127.0.0.1/feed",  # loopback
        "https://localhost/feed",
        "https://[::1]/feed",  # IPv6 loopback
        "https://[::ffff:127.0.0.1]/feed",  # IPv4-mapped loopback
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/feed",  # private range
        "https://100.64.0.1/feed",  # carrier-grade NAT
        "https://example.com:notaport/feed",
        "file:///etc/passwd",
        "ftp://example.com/feed",
        "https:///nohost",
    ],
)
def test_unsafe_urls_are_blocked(url):
    assert not is_safe_url(url)


def test_starter_feeds_load_and_are_https():
    sources = load_sources()
    assert len(sources) == 5
    assert all(s.url.startswith("https://") for s in sources)


def test_data_dir_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("DIGEST_DATA_DIR", str(tmp_path))
    assert data_dir() == tmp_path
