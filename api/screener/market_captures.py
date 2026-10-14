"""Retain Yahoo request evidence and replay it through the existing price owner."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from .artifacts import ArtifactRepository, EvidenceArtifact, store_evidence
from .object_store import ImmutableObjectStore


RECIPE_REVISION = 1
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RECIPE_BYTES = 8 * 1024 * 1024
MAX_REQUESTS = 20000
RECIPE_MEDIA_TYPE = "application/vnd.market-screener.yahoo-capture+json"
_FIELDS = {"requested_url", "response_url", "started_at", "captured_at", "status_code",
           "media_type", "etag", "last_modified", "body_sha256", "failure"}
_FAILURES = {None, "TIMEOUT", "TRANSPORT_ERROR", "RESPONSE_TOO_LARGE"}


class MarketCaptureError(RuntimeError):
    """Broken or incomplete evidence is not ordinary provider unavailability."""


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketCaptureError("capture clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _url(value, *, response=False):
    if not isinstance(value, str) or len(value) > 2048:
        raise MarketCaptureError("invalid Yahoo request identity")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise MarketCaptureError("invalid Yahoo request identity") from exc
    hosts = {"query1.finance.yahoo.com", "query2.finance.yahoo.com"} if response else {"query1.finance.yahoo.com"}
    if (parts.scheme != "https" or parts.netloc not in hosts or parts.fragment
            or not re.fullmatch(r"/v8/finance/chart/[A-Z0-9.^=\-]+", unquote(parts.path))):
        raise MarketCaptureError("invalid Yahoo request identity")
    params = parse_qs(parts.query, keep_blank_values=True)
    if (set(params) != {"range", "interval", "includePrePost", "events"}
            or any(len(values) != 1 for values in params.values())
            or params["range"][0] not in {"1d", "5y", "10y"}
            or params["interval"][0] not in {"5m", "1wk"}
            or params["includePrePost"][0] not in {"true", "false"}
            or params["events"] != ["splits"]):
        raise MarketCaptureError("unsupported Yahoo request identity")
    return str(httpx.URL(value).copy_with(path=unquote(parts.path)))


def _validate(record):
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise MarketCaptureError("invalid market capture metadata")
    _url(record["requested_url"])
    if record["response_url"] is not None:
        _url(record["response_url"], response=True)
        # Redirects may change host, but cannot change the requested chart.
        requested = urlsplit(record["requested_url"])
        response = urlsplit(record["response_url"])
        if unquote(requested.path) != unquote(response.path) or parse_qs(requested.query) != parse_qs(response.query):
            raise MarketCaptureError("response identity differs from requested chart")
    try:
        started, captured = (_utc(datetime.fromisoformat(record[key]))
                             for key in ("started_at", "captured_at"))
    except (TypeError, ValueError) as exc:
        raise MarketCaptureError("invalid market capture clock") from exc
    if captured < started:
        raise MarketCaptureError("capture precedes request")
    status = record["status_code"]
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise MarketCaptureError("invalid market response status")
    failure = record["failure"]
    if (failure is not None and not isinstance(failure, str)) or failure not in _FAILURES:
        raise MarketCaptureError("invalid market failure provenance")
    digest = record["body_sha256"]
    if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise MarketCaptureError("invalid market body reference")
    if failure is None and (status is None or digest is None or record["response_url"] is None):
        raise MarketCaptureError("response requires status, identity and retained body")
    if failure is not None and digest is not None:
        raise MarketCaptureError("failed transport cannot claim a complete response body")
    for key in ("media_type", "etag", "last_modified"):
        value = record[key]
        if value is not None and (not isinstance(value, str) or len(value) > 1024 or "\n" in value or "\r" in value):
            raise MarketCaptureError("invalid market response headers")
    return captured


def _read_artifact(digest, objects, repository, maximum):
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise MarketCaptureError("invalid market artifact identity")
    try:
        artifact = repository.get(digest)
    except (ValueError, KeyError) as exc:
        raise MarketCaptureError("retained market artifact is unavailable") from exc
    if (artifact.content_sha256 != digest or artifact.object_key != f"raw/sha256/{digest}"
            or type(artifact.byte_size) is not int or not 0 <= artifact.byte_size <= maximum):
        raise MarketCaptureError("invalid or oversized market artifact")
    return objects.read_verified(artifact.object_key, digest, artifact.byte_size)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise MarketCaptureError("duplicate field in market recipe")
        value[key] = item
    return value


def retain_response(*, requested_url, response_url, started_at, captured_at, status_code,
                    body, objects: ImmutableObjectStore, repository: ArtifactRepository,
                    media_type=None, etag=None, last_modified=None, failure=None):
    """Retain raw inputs, never a parsed quote/history or caller financial output."""
    if body is not None and (not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES):
        raise MarketCaptureError("invalid or oversized market response")
    if (failure is None) != (body is not None):
        raise MarketCaptureError("complete response and failure provenance disagree")
    record = dict(requested_url=_url(requested_url), response_url=response_url,
                  started_at=_utc(started_at).isoformat(), captured_at=_utc(captured_at).isoformat(),
                  status_code=status_code, media_type=media_type, etag=etag,
                  last_modified=last_modified, body_sha256=None if body is None else "0" * 64,
                  failure=failure)
    _validate(record)
    if body is not None:
        record["body_sha256"] = store_evidence(body, media_type or "application/octet-stream",
                                               objects, repository).content_sha256
    return record


def retain_recipe(records, objects: ImmutableObjectStore, repository: ArtifactRepository) -> EvidenceArtifact:
    records = list(records)
    if not 1 <= len(records) <= MAX_REQUESTS:
        raise MarketCaptureError("market recipe request count is out of bounds")
    seen = set()
    for record in records:
        _validate(record)
        url = record["requested_url"]
        if url in seen:
            raise MarketCaptureError("duplicate request in market recipe")
        seen.add(url)
        if record["body_sha256"] is not None:
            _read_artifact(record["body_sha256"], objects, repository, MAX_RESPONSE_BYTES)
    data = json.dumps({"recipe_revision": RECIPE_REVISION, "provider": "yahoo",
                       "captures": records}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(data) > MAX_RECIPE_BYTES:
        raise MarketCaptureError("market recipe exceeds byte bound")
    return store_evidence(data, RECIPE_MEDIA_TYPE, objects, repository)


class MarketReplay:
    """HTTP-shaped offline reader; a missing recipe/request raises, never fetches."""

    def __init__(self, recipe_sha256, objects: ImmutableObjectStore, repository: ArtifactRepository):
        self.objects, self.repository = objects, repository
        try:
            recipe = json.loads(_read_artifact(recipe_sha256, objects, repository, MAX_RECIPE_BYTES),
                                object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError) as exc:
            raise MarketCaptureError("market recipe is unavailable or invalid") from exc
        if (not isinstance(recipe, dict) or set(recipe) != {"recipe_revision", "provider", "captures"}
                or type(recipe["recipe_revision"]) is not int or recipe["recipe_revision"] != RECIPE_REVISION
                or recipe["provider"] != "yahoo" or not isinstance(recipe["captures"], list)
                or not 1 <= len(recipe["captures"]) <= MAX_REQUESTS):
            raise MarketCaptureError("unsupported market recipe")
        # ponytail: one captured response per URL; ordered attempt recipes if replaying retries is required.
        self.records, self.requests = {}, []
        self._clock = None
        for record in recipe["captures"]:
            _validate(record)
            url = record["requested_url"]
            if url in self.records:
                raise MarketCaptureError("duplicate request in market recipe")
            self.records[url] = record
        # Identity FX makes no HTTP request; use a retained clock even when it runs first.
        self._clock = max(_validate(record) for record in self.records.values())

    def now(self):
        if self._clock is None:
            raise MarketCaptureError("no captured request clock is available")
        return self._clock

    def get(self, url):
        url = _url(url)
        record = self.records.get(url)
        if record is None:
            raise MarketCaptureError("uncaptured market request prohibited")
        self._clock = _validate(record)
        self.requests.append(url)
        request = httpx.Request("GET", url)
        if record["failure"] == "RESPONSE_TOO_LARGE":
            raise MarketCaptureError("captured market response exceeded size bound")
        if record["failure"] is not None:
            error = httpx.ReadTimeout if record["failure"] == "TIMEOUT" else httpx.TransportError
            raise error(f"retained market failure: {record['failure']}", request=request)
        body = _read_artifact(record["body_sha256"], self.objects, self.repository, MAX_RESPONSE_BYTES)
        return httpx.Response(record["status_code"], content=body, request=request,
                              headers={"content-type": record["media_type"] or "application/octet-stream"})

    def close(self):
        pass


class MarketCaptureHTTP:
    """Bounded recorder supplied as YahooPriceProvider's optional HTTP/clock."""

    def __init__(self, objects: ImmutableObjectStore, repository: ArtifactRepository, *,
                 timeout=20.0, http=None, clock=None):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise MarketCaptureError("market timeout must be within 60 seconds")
        self.objects, self.repository, self.timeout = objects, repository, timeout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._owns_http = http is None
        self.http = http if http is not None else httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        self.records = []
        self._clock = None

    def now(self):
        if self._clock is None:
            raise MarketCaptureError("no captured request clock is available")
        return self._clock

    def get(self, url):
        url = _url(url)
        if len(self.records) >= MAX_REQUESTS or any(record["requested_url"] == url for record in self.records):
            raise MarketCaptureError("duplicate or excessive market capture request")
        started = _utc(self.clock())
        deadline = time.monotonic() + self.timeout
        response_url = status = media_type = etag = last_modified = None
        body, failure, original_error = None, None, None
        try:
            with self.http.stream("GET", url, timeout=self.timeout) as response:
                response_url, status = str(response.url), response.status_code
                media_type = response.headers.get("content-type")
                etag, last_modified = response.headers.get("etag"), response.headers.get("last-modified")
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout("market capture read deadline exceeded", request=response.request)
                chunks, size = [], 0
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        failure = "RESPONSE_TOO_LARGE"
                        break
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout("market capture read deadline exceeded", request=response.request)
                    chunks.append(chunk)
                if failure is None:
                    if time.monotonic() > deadline:
                        raise httpx.ReadTimeout("market capture read deadline exceeded", request=response.request)
                    body = b"".join(chunks)
        except httpx.HTTPError as exc:
            original_error = exc
            failure = "TIMEOUT" if isinstance(exc, httpx.TimeoutException) else "TRANSPORT_ERROR"
        self._clock = _utc(self.clock())
        record = retain_response(requested_url=url, response_url=response_url,
            started_at=started, captured_at=self._clock, status_code=status, body=body,
            media_type=media_type, etag=etag, last_modified=last_modified, failure=failure,
            objects=self.objects, repository=self.repository)
        self.records.append(record)
        if failure == "RESPONSE_TOO_LARGE":
            raise MarketCaptureError("market response exceeded size bound")
        if original_error is not None:
            raise original_error
        return httpx.Response(status, content=body, request=httpx.Request("GET", url),
                              headers={"content-type": media_type or "application/octet-stream"})

    def finish_recipe(self):
        return retain_recipe(self.records, self.objects, self.repository)

    def close(self):
        if self._owns_http:
            self.http.close()
