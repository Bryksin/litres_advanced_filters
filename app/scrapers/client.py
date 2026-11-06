"""Rate-limited HTTP client for LitRes (Phase 3.1)."""

import random
import time
from typing import Any

import httpx

from app.config.config import Config

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; rv:131.0) Gecko/20100101 Firefox/131.0"

# RE doc §2.8: headers required by the JSON API (api.litres.ru).
DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-version": "2",
    "app-id": "115",
    "client-host": "www.litres.ru",
    "user-agent": _USER_AGENT,
    "accept-language": "ru-RU",
    "ui-currency": "RUB",
    "ui-language-code": "ru",
}
API_HEADERS = DEFAULT_HEADERS

# Minimal browser-like headers for www.litres.ru HTML pages. The LitRes WAF
# (ddos-guard) answers 403 to HTML requests that carry the API header set, and
# (verified live 2026-11-06) also to a plain ``accept-language`` header sent
# from this client. UA + HTML accept is what passes. Do not add headers here
# without re-checking a series page live.
HTML_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "user-agent": _USER_AGENT,
}

BASE_HTML = "https://www.litres.ru"
BASE_API = "https://api.litres.ru"


class RateLimitedClient:
    """
    HTTP client that enforces a minimum delay between requests to LitRes.
    Default: ~2 req/s (0.5s min delay + 0.1s jitter). Use for all LitRes requests (NFR-1, NFR-2).
    """

    def __init__(
        self,
        min_delay_seconds: float = Config.SCRAPER_MIN_DELAY_SECONDS,
        jitter_seconds: float = Config.SCRAPER_JITTER_SECONDS,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ):
        self.min_delay = min_delay_seconds
        self.jitter = jitter_seconds
        self._headers = {**API_HEADERS, **(headers or {})}
        self._html_headers = {**HTML_HEADERS, **(headers or {})}
        self._last_request_time: float = 0.0
        # Persistent client — reuses TCP/TLS connections across requests (HTTP keep-alive).
        # Creating a new client per request adds 300-700 ms TLS handshake overhead each time,
        # reducing effective throughput from ~4 req/s to ~1 req/s even at min_delay=0.25s.
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _wait(self) -> None:
        """Enforce delay since last request (with optional jitter)."""
        elapsed = time.monotonic() - self._last_request_time
        delay = self.min_delay - elapsed
        if self.jitter > 0:
            delay += random.uniform(0, self.jitter)
        if delay > 0:
            time.sleep(delay)
        self._last_request_time = time.monotonic()

    def _prepare_headers(
        self, referer: str | None = None, *, html: bool = False,
    ) -> dict[str, str]:
        out = dict(self._html_headers if html else self._headers)
        if referer:
            if not referer.startswith("http"):
                referer = BASE_HTML + referer if referer.startswith("/") else BASE_HTML + "/" + referer
            out["referer"] = referer
        return out

    def get_html(
        self,
        path_or_url: str,
        *,
        referer: str | None = None,
    ) -> str:
        """
        GET a LitRes HTML page. Use path (e.g. /genre/.../) or full URL.
        Returns response text. Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        self._wait()
        url = path_or_url if path_or_url.startswith("http") else BASE_HTML + (path_or_url if path_or_url.startswith("/") else "/" + path_or_url)
        # Only send a referer when the caller supplies one: a self-referer on a
        # first navigation is not what a browser sends.
        headers = self._prepare_headers(referer, html=True)
        resp = self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text

    def get_api(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        referer: str | None = None,
    ) -> dict[str, Any]:
        """
        GET a LitRes API endpoint. Path is relative to api.litres.ru.
        Returns JSON body. Raises httpx.HTTPStatusError on 4xx/5xx.
        """
        self._wait()
        url = path if path.startswith("http") else BASE_API + (path if path.startswith("/") else "/" + path)
        headers = self._prepare_headers(referer or BASE_HTML + "/")
        resp = self._client.get(url, headers=headers, params=params or {})
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        """Close the underlying HTTP client and release connections."""
        self._client.close()

    def __enter__(self) -> "RateLimitedClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
