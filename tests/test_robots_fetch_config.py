import pytest

from daily_digest.config import load_sources
from daily_digest.fetch import is_safe_url
from daily_digest.robots import RobotsChecker

UA = "daily-digest-test"


def checker(status: int, body: str = "", raises: bool = False) -> RobotsChecker:
    def fetch(url: str):
        if raises:
            raise RuntimeError("network down")
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


def test_robots_server_error_is_cautious():
    assert not checker(500).allowed("https://example.com/feed")


def test_robots_unreachable_is_cautious():
    assert not checker(0, raises=True).allowed("https://example.com/feed")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/feed",  # not https
        "https://127.0.0.1/feed",  # loopback
        "https://localhost/feed",
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://10.0.0.5/feed",  # private range
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
