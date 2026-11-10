"""Small rate-limited HTTP helper. One limiter per host; honours Retry-After on 429."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class QuotaExhausted(Exception):
    """The service asked us to wait longer than is sensible inside one run (daily budget used up)."""


class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval:
            time.sleep(self.min_interval - delta)
        self._last = time.monotonic()


class Client:
    def __init__(self, user_agent: str, min_interval: float = 0.0, headers: dict[str, str] | None = None,
                 max_retries: int = 4, timeout: float = 60.0, max_wait: float = 60.0):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        if headers:
            self.session.headers.update(headers)
        self.limiter = RateLimiter(min_interval)
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_wait = max_wait

    def get(self, url: str, params: dict[str, Any] | None = None, stream: bool = False) -> requests.Response:
        backoff = 2.0
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout, stream=stream)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                log.warning("GET %s failed (%s); retrying in %.0fs", url, e, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else backoff
                if resp.status_code == 429 and wait > self.max_wait:
                    raise QuotaExhausted(f"{url}: rate limited, Retry-After={wait:.0f}s")
                if attempt == self.max_retries:
                    resp.raise_for_status()
                log.warning("GET %s -> %s; retrying in %.0fs", url, resp.status_code, wait)
                time.sleep(min(wait, self.max_wait))
                backoff *= 2
                continue
            return resp
        raise RuntimeError("unreachable")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.get(url, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        resp = self.get(url, params=params)
        resp.raise_for_status()
        return resp.text
