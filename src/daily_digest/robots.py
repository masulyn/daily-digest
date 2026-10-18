"""robots.txt checks (NFR-2).

Python's built-in RobotFileParser treats a 401 or 403 on robots.txt as "everything
is disallowed". The standard (RFC 9309) says a 4xx means robots.txt is unavailable and
the crawler may proceed, while a server error means be cautious. We follow the standard.
"""

from collections.abc import Callable
from urllib.robotparser import RobotFileParser
from urllib.parse import urlsplit

# fetch(url) -> (status code, body bytes)
FetchFn = Callable[[str], tuple[int, bytes]]


class RobotsChecker:
    def __init__(self, fetch: FetchFn, user_agent: str) -> None:
        self._fetch = fetch
        self._user_agent = user_agent
        self._cache: dict[str, RobotFileParser | bool] = {}

    def _rules_for(self, origin: str) -> RobotFileParser | bool:
        """A parsed ruleset, True (no restrictions), or False (treat as disallowed)."""
        if origin in self._cache:
            return self._cache[origin]
        try:
            status, body = self._fetch(f"{origin}/robots.txt")
        except Exception:
            result: RobotFileParser | bool = False  # cannot tell: be cautious
        else:
            if 200 <= status < 300:
                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                result = parser
            elif 400 <= status < 500:
                result = True  # robots.txt unavailable: no restrictions
            else:
                result = False  # server error: be cautious
        self._cache[origin] = result
        return result

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rules = self._rules_for(f"{parts.scheme}://{parts.netloc}")
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self._user_agent, url)
