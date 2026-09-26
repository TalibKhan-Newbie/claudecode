"""Polite HTTP: robots.txt, per-host rate limiting, caching, size caps.

Every outbound fetch goes through :class:`Fetcher`. It refuses a URL that
robots.txt disallows, never retries a 403/404, caps response size, and sends a
User-Agent that identifies the bot and points at a contact URL so a site owner
can tell you to stop.

There is deliberately no CAPTCHA solving and no headless-browser fallback. When
a platform puts a contact behind a human check, that check is the answer.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "creator-contacts/0.1 (+https://github.com/TalibKhan-Newbie/claudecode; "
    "business-outreach research; contact: set OUTREACH_CONTACT_URL)"
)

MAX_BYTES = 2_000_000
TEXTUAL_TYPES = ("text/html", "text/plain", "application/xhtml", "application/xml", "text/xml", "+xml")


class FetchBlocked(Exception):
    """robots.txt disallows this URL for our User-Agent."""


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    content_type: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and bool(self.text)


class _HostThrottle:
    """One token per ``delay`` seconds, per host."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        with self._lock:
            now = time.monotonic()
            earliest = self._next.get(host, 0.0)
            sleep_for = max(earliest - now, 0.0)
            self._next[host] = max(now, earliest) + self.delay
        if sleep_for:
            time.sleep(sleep_for)


@dataclass
class Fetcher:
    user_agent: str = DEFAULT_UA
    delay: float = 1.5
    timeout: float = 15.0
    max_retries: int = 2
    obey_robots: bool = True
    #: Optional callables for persisting responses between runs; see ``store.py``.
    cache_get: object | None = None
    cache_put: object | None = None

    _client: httpx.Client = field(init=False, repr=False)
    _throttle: _HostThrottle = field(init=False, repr=False)
    _robots: dict[str, urllib.robotparser.RobotFileParser | None] = field(
        init=False, default_factory=dict, repr=False
    )
    _robots_lock: threading.Lock = field(init=False, default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=self.timeout,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._throttle = _HostThrottle(self.delay)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- robots -------------------------------------------------------------

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]

        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            self._throttle.wait(parsed.netloc)
            response = self._client.get(urljoin(origin, "/robots.txt"), timeout=8.0)
            if response.status_code < 400 and response.text:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except httpx.HTTPError as exc:
            # No reachable robots.txt is treated as "no restrictions stated".
            log.debug("robots.txt unavailable for %s: %s", origin, exc)

        with self._robots_lock:
            self._robots[origin] = parser
        return parser

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float | None:
        parser = self._robots_for(url) if self.obey_robots else None
        if parser is None:
            return None
        try:
            value = parser.crawl_delay(self.user_agent)
        except AttributeError:
            return None
        return float(value) if value else None

    # -- fetching -----------------------------------------------------------

    def get(self, url: str, *, use_cache: bool = True) -> FetchResult:
        """Fetch ``url``, or raise :class:`FetchBlocked` if robots.txt says no."""
        if use_cache and callable(self.cache_get):
            cached = self.cache_get(url)
            if cached is not None:
                status, text, content_type = cached
                return FetchResult(url, status, text, content_type, from_cache=True)

        if not self.allowed(url):
            raise FetchBlocked(f"robots.txt disallows {url}")

        host = urlparse(url).netloc
        polite = self.crawl_delay(url)
        if polite and polite > self.delay:
            time.sleep(polite - self.delay)

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle.wait(host)
            try:
                with self._client.stream("GET", url) as response:
                    content_type = response.headers.get("content-type", "")
                    # A 4xx is the server's final answer; only 429/5xx deserve a retry.
                    if response.status_code in (429,) or response.status_code >= 500:
                        if attempt < self.max_retries:
                            time.sleep(2.0 * (attempt + 1))
                            continue
                    if not any(t in content_type.lower() for t in TEXTUAL_TYPES) and content_type:
                        return FetchResult(url, response.status_code, "", content_type)

                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= MAX_BYTES:
                            break
                    body = b"".join(chunks)
                    text = body.decode(response.encoding or "utf-8", errors="replace")
                    result = FetchResult(url, response.status_code, text, content_type)

                if result.ok and use_cache and callable(self.cache_put):
                    self.cache_put(url, result.status, result.text, result.content_type)
                return result

            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.max_retries:
                    time.sleep(2.0 * (attempt + 1))

        log.warning("giving up on %s: %s", url, last_error)
        return FetchResult(url, 0, "", "")

    def get_json(self, url: str, *, params: dict | None = None) -> dict | list | None:
        """Fetch a JSON API. Bypasses robots.txt, which governs crawlers not APIs."""
        host = urlparse(url).netloc
        for attempt in range(self.max_retries + 1):
            self._throttle.wait(host)
            try:
                response = self._client.get(url, params=params)
                if response.status_code in (429,) or response.status_code >= 500:
                    if attempt < self.max_retries:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                if response.status_code >= 400:
                    log.warning("%s returned %s: %s", url, response.status_code, response.text[:300])
                    return None
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("json fetch failed for %s: %s", url, exc)
                if attempt < self.max_retries:
                    time.sleep(2.0 * (attempt + 1))
        return None


def candidate_contact_urls(base_url: str, paths: Iterable[str]) -> list[str]:
    """``base_url`` plus its likely contact pages, deduped, homepage first."""
    parsed = urlparse(base_url)
    if not parsed.scheme:
        base_url = "https://" + base_url.lstrip("/")
        parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    urls = [base_url if parsed.path not in ("", "/") else origin + "/"]
    urls.extend(origin + path for path in paths)

    out: dict[str, None] = {}
    for url in urls:
        out.setdefault(url.rstrip("/") or url, None)
    return list(out)
