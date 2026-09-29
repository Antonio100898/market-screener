"""Layer 1: SEC EDGAR fetch. Fetch only; no interpretation."""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_MIN_INTERVAL = 0.11  # SEC cap: 10 req/s
_RETRIES = 4


class EdgarError(Exception):
    pass


class UnknownTickerError(EdgarError):
    pass


class NoXbrlDataError(EdgarError):
    """Filer exists in EDGAR but has never submitted structured XBRL data.
    Common for ADR shells and pre-2009 registrants; not an outage."""


@dataclass(frozen=True)
class EdgarTransportResult:
    data: bytes
    url: str
    media_type: str | None
    etag: str | None
    last_modified: str | None


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: str | Path | None = None,
        ttl_seconds: int = 86400,
    ):
        # SEC mandates a User-Agent identifying the caller with contact details
        self.user_agent = user_agent or os.environ.get(
            "SEC_USER_AGENT", "GrahamScreener am.worker.15@gmail.com"
        )
        self.cache_dir = Path(cache_dir or Path.home() / ".cache" / "graham-screener")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_seconds
        self._http = httpx.Client(
            headers={"User-Agent": self.user_agent}, timeout=30.0, follow_redirects=True
        )
        self._last_request = 0.0
        # FastAPI sync endpoints run on a threadpool; the SEC cap is per source,
        # so the wait-and-stamp must be atomic across threads
        self._rate_lock = threading.Lock()

    def cik_for(self, ticker: str) -> str:
        mapping = self._cached("company_tickers", TICKER_MAP_URL)
        wanted = ticker.upper().replace(".", "-")  # SEC maps BRK.B as BRK-B
        for row in mapping.values():
            if row["ticker"].upper() == wanted:
                return f"{int(row['cik_str']):010d}"
        raise UnknownTickerError(ticker)

    def company_facts(self, cik: str) -> dict:
        return self._cached(f"companyfacts_{cik}", FACTS_URL.format(cik=cik))

    def submissions(self, cik: str) -> dict:
        """The filer's filing history header — the only source for how long the
        company has been an SEC filer at all, which XBRL facts cannot show."""
        return self._get(f"https://data.sec.gov/submissions/CIK{cik}.json")

    def fetch(self, url: str) -> EdgarTransportResult:
        """Fetch exact response bytes and source metadata through the SEC limiter."""
        response = self._request(url)
        return EdgarTransportResult(
            data=bytes(response.content),
            url=str(response.url),
            media_type=response.headers.get("content-type"),
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )

    def _cached(self, key: str, url: str) -> dict:
        # Filings are immutable once filed; a day-long TTL is the main performance lever.
        path = self.cache_dir / f"{key}.json"
        if path.exists() and time.time() - path.stat().st_mtime < self.ttl_seconds:
            try:
                return json.loads(path.read_text())
            except ValueError:
                pass  # unreadable cache entry == cache miss
        data = self._get(url)
        path.write_text(json.dumps(data))
        return data

    def _get_text(self, url: str) -> str:
        """Plain-text fetch for index files, under the same rate limit and retries."""
        return self._request(url).text

    def _get(self, url: str) -> dict:
        resp = self._request(url)
        try:
            return resp.json()
        except ValueError:
            raise EdgarError(f"non-JSON body on HTTP 200 for {url}")

    def _request(self, url: str) -> httpx.Response:
        error = "unknown"
        for attempt in range(_RETRIES):
            with self._rate_lock:
                gap = self._last_request + _MIN_INTERVAL - time.monotonic()
                if gap > 0:
                    time.sleep(gap)
                self._last_request = time.monotonic()
            try:
                resp = self._http.get(url)
            except httpx.HTTPError as exc:
                error = repr(exc)
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404:
                    raise NoXbrlDataError(f"not found: {url}")
                error = f"HTTP {resp.status_code}"
                if resp.status_code != 429 and resp.status_code < 500:
                    raise EdgarError(f"{error} for {url}")
            if attempt < _RETRIES - 1:
                time.sleep(2**attempt)  # exponential backoff on 429/5xx/network
        raise EdgarError(f"failed after {_RETRIES} attempts: {url} ({error})")
