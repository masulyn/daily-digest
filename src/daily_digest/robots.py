"""robots.txt checks.

Python's built-in RobotFileParser.read() treats a 401 or 403 on robots.txt as "everything
is disallowed". The standard (RFC 9309) says a 4xx means robots.txt is unavailable and
the crawler may proceed, while a server error means be cautious. We fetch the file
ourselves and follow the standard, except that 429 is treated as a server-side
"slow down" rather than as a missing file.
"""

from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from .fetch import FetchError, FetchFn


class RobotsChecker:
    def __init__(self, fetch: FetchFn, user_agent: str) -> None:
        self._fetch = fetch
        self._user_agent = user_agent
        self._cache: dict[str, RobotFileParser | bool] = {}
        self._unavailable: set[str] = set()

    def _rules_for(self, origin: str) -> RobotFileParser | bool:
        """A parsed ruleset, True (no restrictions), or False (treat as disallowed)."""
        if origin in self._cache:
            return self._cache[origin]
        try:
            status, body = self._fetch(f"{origin}/robots.txt")
        except FetchError:
            result: RobotFileParser | bool = False  # cannot tell: be cautious
            self._unavailable.add(origin)
        else:
            if 200 <= status < 300:
                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                result = parser
            elif status != 429 and 400 <= status < 500:
                result = True
            else:
                result = False
                self._unavailable.add(origin)
        self._cache[origin] = result
        return result

    def crawl_delay(self, url: str) -> float | None:
        """Seconds the site asks us to wait between requests, if its robots.txt says so."""
        parts = urlsplit(url)
        rules = self._rules_for(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool):
            return None
        delay = rules.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None

    def unavailable(self, url: str) -> bool:
        """True when `allowed` is False only because robots.txt could not be read (a passing problem)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        self._rules_for(origin)
        return origin in self._unavailable

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rules = self._rules_for(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self._user_agent, url)
