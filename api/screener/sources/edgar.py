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


class EdgarPayloadTooLarge(EdgarError):
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

    def close(self) -> None:
        self._http.close()

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

    def fetch(self, url: str, *, maximum_bytes: int | None = None) -> EdgarTransportResult:
        """Fetch exact response bytes and source metadata through the SEC limiter."""
        if maximum_bytes is not None:
            return self._request(url, maximum_bytes=maximum_bytes)
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

    def _bounded_fetch_attempt(self, url: str, maximum_bytes: int):
        request = self._http.build_request("GET", url, headers={"Accept-Encoding": "identity"})
        for hop in range(self._http.max_redirects + 1):
            if hop:
                self._wait_for_rate_limit()
            response = self._http.send(request, stream=True, follow_redirects=False)
            try:
                if response.next_request is not None and self._http.follow_redirects:
                    request = response.next_request
                    continue
                if response.status_code != 200:
                    return response, None
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise EdgarError("bounded SEC fetch requires identity Content-Encoding")
                length = response.headers.get("content-length")
                if length is not None and not (length.isascii() and length.isdecimal()):
                    raise EdgarError("invalid Content-Length for bounded SEC fetch")
                payload = bytearray()
                for chunk in response.iter_raw():
                    if len(payload) + len(chunk) > maximum_bytes:
                        raise EdgarPayloadTooLarge(f"SEC payload exceeds {maximum_bytes} bytes for {url}")
                    payload.extend(chunk)
                return response, EdgarTransportResult(
                    bytes(payload), str(response.url), response.headers.get("content-type"),
                    response.headers.get("etag"), response.headers.get("last-modified"))
            finally:
                response.close()
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)

    def _wait_for_rate_limit(self):
        with self._rate_lock:
            gap = self._last_request + _MIN_INTERVAL - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self._last_request = time.monotonic()

    def _request(self, url: str, *, maximum_bytes: int | None = None) -> httpx.Response | EdgarTransportResult:
        if maximum_bytes is not None and (type(maximum_bytes) is not int or maximum_bytes <= 0):
            raise ValueError("maximum_bytes must be a positive integer")
        error = "unknown"
        for attempt in range(_RETRIES):
            self._wait_for_rate_limit()
            try:
                bounded = None
                if maximum_bytes is None:
                    resp = self._http.get(url)
                else:
                    resp, bounded = self._bounded_fetch_attempt(url, maximum_bytes)
            except httpx.HTTPError as exc:
                error = repr(exc)
            else:
                if resp.status_code == 200:
                    return resp if bounded is None else bounded
                if resp.status_code == 404:
                    raise NoXbrlDataError(f"not found: {url}")
                error = f"HTTP {resp.status_code}"
                if resp.status_code != 429 and resp.status_code < 500:
                    raise EdgarError(f"{error} for {url}")
            if attempt < _RETRIES - 1:
                time.sleep(2**attempt)  # exponential backoff on 429/5xx/network
        raise EdgarError(f"failed after {_RETRIES} attempts: {url} ({error})")
