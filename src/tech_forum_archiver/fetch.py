"""Polite, rate-limited HTTP client.

The forum sits behind an F5 BigIP that throttles clients: without cookies a
client may make 1 request per 30 seconds, and with a session cookie 10
requests per 30 seconds. Requests made while throttled still count, so we
keep our own sliding-window limit below the server's and back off for the
full penalty window when we see HTTP 429.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from collections import deque
from urllib.parse import urljoin

import httpx
import truststore

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://tech.churchofjesuschrist.org/forum/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; tech-forum-archiver/0.1; "
    "+https://github.com/williamjacksn/tech-forum-archiver)"
)


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        max_requests: int = 10,
        window: float = 32.0,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 60.0,
        max_retries: int = 6,
    ) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.max_requests = max_requests
        self.window = window
        # spread requests evenly rather than in bursts: the server's accounting
        # is stricter than a plain sliding window
        self.min_interval = window / max_requests
        self.max_retries = max_retries
        self.sent: deque[float] = deque()
        self.request_count = 0
        self.client = httpx.Client(
            # use the operating system's trust store (works behind TLS-inspecting
            # proxies)
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
            headers={"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.8"},
            timeout=timeout,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def url(self, path: str) -> str:
        return urljoin(self.base_url, path.removeprefix("./"))

    def export_cookies(self) -> list[dict]:
        return [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.client.cookies.jar
            if not c.is_expired()
        ]

    def import_cookies(self, cookies: list[dict]) -> None:
        """Reuse a previous session.

        The very first request of a run would otherwise be cookieless, and
        cookieless clients get only 1 request per 30 s per IP address.
        """
        for c in cookies:
            self.client.cookies.set(
                c["name"], c["value"], domain=c["domain"], path=c["path"]
            )

    def export_recent_requests(self) -> list[float]:
        """Times of requests still inside the rate-limit window, so that a run started
        right after this one does not exceed the limit."""
        now = time.time()
        return [t for t in self.sent if now - t < self.window]

    def import_recent_requests(self, times: list[float]) -> None:
        now = time.time()
        self.sent.extend(sorted(t for t in times if 0 <= now - t < self.window))

    def _wait_for_slot(self) -> None:
        while True:
            now = time.time()
            while self.sent and now - self.sent[0] >= self.window:
                self.sent.popleft()
            wait = 0.0
            if self.sent:
                wait = self.sent[-1] + self.min_interval - now
            if len(self.sent) >= self.max_requests:
                wait = max(wait, self.sent[0] + self.window - now)
            if wait <= 0:
                break
            time.sleep(wait + 0.01)
        self.sent.append(time.time())

    def _penalty(self, resp: httpx.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after) + 2
        m = re.search(r"throttle_timeout=(\d+)", resp.text)
        return (float(m.group(1)) if m else 30.0) + 5

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        """GET a page. Returns the response for 2xx/3xx/4xx (except 429)."""
        url = self.url(path)
        delay = 5.0
        attempt = 0
        throttled = 0
        while attempt < self.max_retries:
            self._wait_for_slot()
            self.request_count += 1
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError as e:
                attempt += 1
                log.warning("network error on %s (attempt %d): %s", url, attempt, e)
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            if resp.status_code == 429:
                # being throttled is not a failure of the request; just wait it out
                throttled += 1
                wait = self._penalty(resp) * min(throttled, 4)
                details = " ".join(
                    re.findall(
                        r"\b(?:cookieless|throttle_count|throttle_max)=\w+", resp.text
                    )
                )
                log.warning("throttled by server (%s); sleeping %.0fs", details, wait)
                time.sleep(wait)
                self.sent.clear()
                continue
            attempt += 1
            if resp.status_code >= 500:
                log.warning(
                    "HTTP %d on %s (attempt %d)", resp.status_code, url, attempt
                )
                time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            log.debug("GET %s -> %d", resp.url, resp.status_code)
            return resp
        raise FetchError(f"giving up on {url} after {self.max_retries} attempts")
