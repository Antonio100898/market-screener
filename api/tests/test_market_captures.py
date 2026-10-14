from dataclasses import asdict, replace
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from screener.artifacts import store_evidence
from screener.market_captures import (MarketCaptureError, MarketCaptureHTTP, MarketReplay,
    MAX_RESPONSE_BYTES, RECIPE_MEDIA_TYPE, retain_recipe, retain_response)
from screener.object_store import ImmutableObjectStore, ObjectVerificationError
from screener.sources.prices import YahooPriceProvider, YAHOO_URL
from test_artifacts import MemoryRepository
from test_object_store import MemoryS3


NOW = datetime(2026, 10, 4, 8, 20, tzinfo=timezone.utc)


class Repository(MemoryRepository):
    def get(self, digest):
        try:
            return self.rows[digest]
        except KeyError:
            raise ValueError("artifact missing") from None


@pytest.fixture
def storage():
    return ImmutableObjectStore(MemoryS3(), "market-tests"), Repository()


def url(symbol="ABT", range_="1d", interval="5m", pre=True):
    return YAHOO_URL.format(symbol=symbol, range=range_, interval=interval,
                            include_pre_post=str(pre).lower())


def chart(currency="USD", price=10):
    return {"chart": {"result": [{"meta": {"regularMarketPrice": price,
        "regularMarketTime": int(NOW.timestamp()), "currency": currency},
        "timestamp": [int(NOW.timestamp())], "indicators": {"quote": [{"close": [price]}]},
        "events": {"splits": {"one": {"date": int(NOW.timestamp()), "numerator": 1, "denominator": 10}}}}]}}


def retained(storage, *, request_url=None, data=None, status=200, failure=None):
    objects, repository = storage
    request_url = request_url or url()
    return retain_response(requested_url=request_url, response_url=request_url if status else None,
        started_at=NOW, captured_at=NOW, status_code=status,
        body=data if data is not None else (None if failure else json.dumps(chart()).encode()),
        failure=failure, media_type="application/json", objects=objects, repository=repository)


def replay(storage, records):
    objects, repository = storage
    artifact = retain_recipe(records, objects, repository)
    transport = MarketReplay(artifact.content_sha256, objects, repository)
    return transport, YahooPriceProvider(http=transport, clock=transport.now)


def test_optional_provider_transport_clock_and_external_ownership(storage):
    objects, repository = storage
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=chart()))) as http:
        capture = MarketCaptureHTTP(objects, repository, http=http, clock=lambda: NOW)
        provider = YahooPriceProvider(http=capture, clock=capture.now)
        quote = provider.quote("ABT")
        assert quote.price == 10 and quote.market_state_asof == NOW
        provider.close()
        assert not http.is_closed
        transport = MarketReplay(capture.finish_recipe().content_sha256, objects, repository)
        assert YahooPriceProvider(http=transport, clock=transport.now).quote("ABT") == quote
        assert len(capture.records) == 1


def test_encoded_index_symbol_and_share_class_keep_request_identity(storage):
    records = [retained(storage, request_url=url("^GSPC").replace("^", "%5E")), retained(storage, request_url=url("BRK-B"))]
    transport, provider = replay(storage, records)
    assert provider.quote("^GSPC").price == 10
    assert provider.quote("BRK.B").price == 10
    assert "/^GSPC?" in transport.requests[0]
    assert "/BRK-B?" in transport.requests[1]


def test_history_splits_fx_inverse_and_wrong_currency_use_existing_owner(storage):
    records = [retained(storage, request_url=url("ABT", "5y", "1wk", False)), retained(storage),
        retained(storage, request_url=url("USDJPY=X", "10y", "1wk", False), status=404, data=b'{"chart":{"error":"missing pair"}}'),
        retained(storage, request_url=url("JPYUSD=X", "10y", "1wk", False), data=json.dumps(chart("USD", 0.01)).encode())]
    transport, provider = replay(storage, records)
    assert provider.exchange_rate_history("JPY", "JPY").quote.asof == NOW
    assert provider.history("ABT").splits[0][1] == 10
    assert provider.quote("ABT", expected_currency="JPY") is None
    fx = provider.exchange_rate_history("USD", "JPY")
    assert fx.quote.price == 100 and fx.closes[0][1] == 100
    assert fx.quote.source == "yahoo-fx:JPYUSD=X:inverted"
    assert provider.exchange_rate_history("JPY", "JPY").quote.asof == NOW
    assert len(transport.requests) == 5


@pytest.mark.parametrize("data,status,failure", [(b'{"chart":{"result":null,"error":{"code":"Not Found"}}}', 200, None),
    (b"upstream unavailable", 503, None), (b"not JSON", 200, None), (None, None, "TIMEOUT"),
    (None, None, "TRANSPORT_ERROR")])
def test_captured_absence_http_and_transport_failures_remain_retained(storage, data, status, failure):
    record = retained(storage, data=data, status=status, failure=failure)
    transport, provider = replay(storage, [record])
    assert provider.quote("ABT") is None
    assert record["status_code"] == status and record["failure"] == failure
    assert (record["body_sha256"] is None) == (failure is not None)
    assert transport.now() == NOW


def test_recording_network_failure_and_size_bound_are_explicit(storage, monkeypatch):
    objects, repository = storage
    def fail(request):
        raise httpx.ReadTimeout("synthetic timeout", request=request)
    with httpx.Client(transport=httpx.MockTransport(fail)) as http:
        capture = MarketCaptureHTTP(objects, repository, http=http, clock=lambda: NOW)
        assert YahooPriceProvider(http=capture, clock=capture.now).quote("ABT") is None
        record = capture.records[0]
        assert record["failure"] == "TIMEOUT" and record["body_sha256"] is None
        offline = MarketReplay(capture.finish_recipe().content_sha256, objects, repository)
        assert YahooPriceProvider(http=offline, clock=offline.now).quote("ABT") is None
    monkeypatch.setattr("screener.market_captures.MAX_RESPONSE_BYTES", 5)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"123456"))) as http:
        capture = MarketCaptureHTTP(objects, repository, http=http, clock=lambda: NOW)
        with pytest.raises(MarketCaptureError, match="size"):
            YahooPriceProvider(http=capture, clock=capture.now).quote("ABT")
        assert capture.records[0]["failure"] == "RESPONSE_TOO_LARGE"
        offline = MarketReplay(capture.finish_recipe().content_sha256, objects, repository)
        with pytest.raises(MarketCaptureError, match="size"):
            YahooPriceProvider(http=offline, clock=offline.now).quote("ABT")


@pytest.mark.parametrize("headers_at,completed_at,body,expired", [
    (2.0, 2.0, b"", True),
    (0.0, 2.0, b"", True),
    (0.0, 2.0, b"{}", True),
    (1.0, 1.0, b"", False),
    (0.0, 1.0, b"{}", False),
])
def test_total_read_deadline_covers_headers_and_empty_or_completed_body(
        storage, monkeypatch, headers_at, completed_at, body, expired):
    objects, repository = storage
    elapsed = [0.0]
    class Response:
        status_code, headers = 200, {"content-type": "application/json"}
        request = httpx.Request("GET", url())
        url = request.url

        def iter_bytes(self, chunk_size):
            if body:
                yield body
            elapsed[0] = completed_at

    class HTTP:
        @contextmanager
        def stream(self, method, request_url, timeout):
            elapsed[0] = headers_at
            yield Response()

    monkeypatch.setattr("screener.market_captures.time.monotonic", lambda: elapsed[0])
    capture = MarketCaptureHTTP(objects, repository, http=HTTP(), timeout=1, clock=lambda: NOW)
    if expired:
        with pytest.raises(httpx.ReadTimeout, match="deadline"):
            capture.get(url())
        record = capture.records[0]
        assert record["failure"] == "TIMEOUT" and record["body_sha256"] is None
        assert record["status_code"] == 200 and record["response_url"] == url()
        assert repository.rows == {}
        offline = MarketReplay(capture.finish_recipe().content_sha256, objects, repository)
        with pytest.raises(httpx.ReadTimeout):
            offline.get(url())
    else:
        assert capture.get(url()).content == body
        assert capture.records[0]["failure"] is None
        assert capture.records[0]["body_sha256"] == hashlib.sha256(body).hexdigest()


def test_replay_missing_request_or_evidence_cannot_be_provider_absence(storage, monkeypatch):
    objects, repository = storage
    transport, provider = replay(storage, [retained(storage)])
    monkeypatch.setattr(httpx, "Client", lambda **_: pytest.fail("offline replay opened live HTTP"))
    with pytest.raises(MarketCaptureError, match="uncaptured"):
        provider.history("ABT")
    with pytest.raises(MarketCaptureError, match="unavailable"):
        MarketReplay("a" * 64, objects, repository)
    digest = transport.records[url()]["body_sha256"]
    del repository.rows[digest]
    with pytest.raises(MarketCaptureError, match="unavailable"):
        provider.quote("ABT")


@pytest.mark.parametrize("corruption", ["hash", "size", "reference", "metadata"])
def test_tampered_capture_fails_closed(storage, corruption):
    objects, repository = storage
    record = retained(storage)
    transport, provider = replay(storage, [record])
    artifact = repository.get(record["body_sha256"])
    if corruption in {"hash", "size"}:
        original = objects.client.objects[artifact.object_key]
        objects.client.objects[artifact.object_key] = b"x" * (len(original) + (corruption == "size"))
        with pytest.raises(ObjectVerificationError):
            provider.quote("ABT")
    elif corruption == "reference":
        repository.rows[artifact.content_sha256] = replace(artifact, byte_size=MAX_RESPONSE_BYTES + 1)
        with pytest.raises(MarketCaptureError, match="oversized"):
            provider.quote("ABT")
    else:
        bad = {"recipe_revision": 1, "provider": "yahoo", "captures": [{**record, "price": 123}]}
        recipe = store_evidence(json.dumps(bad).encode(), RECIPE_MEDIA_TYPE, objects, repository)
        with pytest.raises(MarketCaptureError, match="metadata"):
            MarketReplay(recipe.content_sha256, objects, repository)


@pytest.mark.parametrize("changes", [{"started_at": NOW.replace(tzinfo=None).isoformat()}, {"status_code": True},
    {"response_url": url("OTHER")}, {"requested_url": "https://example.com/chart"}, {"failure": []},
    {"etag": "bad\nheader"}, {"body_sha256": "bad"}])
def test_recipe_metadata_is_validated_before_retention(storage, changes):
    objects, repository = storage
    record = retained(storage)
    with pytest.raises(MarketCaptureError):
        retain_recipe([{**record, **changes}], objects, repository)


def test_duplicate_empty_recipe_and_timeout_bounds(storage):
    objects, repository = storage
    record = retained(storage)
    for records in ([], [record, record]):
        with pytest.raises(MarketCaptureError):
            retain_recipe(records, objects, repository)
    for timeout in (True, 0, 61, float("nan")):
        with pytest.raises(MarketCaptureError):
            MarketCaptureHTTP(objects, repository, timeout=timeout)


def test_recipe_artifact_tamper_and_duplicate_fields_are_explicit(storage):
    objects, repository = storage
    artifact = retain_recipe([retained(storage)], objects, repository)
    objects.client.objects[artifact.object_key] = b"x" * artifact.byte_size
    with pytest.raises(ObjectVerificationError):
        MarketReplay(artifact.content_sha256, objects, repository)
    duplicate = store_evidence(b'{"recipe_revision":1,"recipe_revision":2,"provider":"yahoo","captures":[]}',
                               RECIPE_MEDIA_TYPE, objects, repository)
    with pytest.raises(MarketCaptureError, match="duplicate"):
        MarketReplay(duplicate.content_sha256, objects, repository)


def test_failed_body_upload_does_not_create_recipe_or_artifact(storage):
    objects, repository = storage
    objects.client.fail_put = True
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=chart()))) as http:
        capture = MarketCaptureHTTP(objects, repository, http=http, clock=lambda: NOW)
        with pytest.raises(RuntimeError, match="upload failed"):
            YahooPriceProvider(http=capture, clock=capture.now).quote("ABT")
        assert repository.rows == {} and capture.records == []


def test_genuine_twelve_retained_responses_match_prior_seven_branch_proof(storage):
    root = Path(__file__).parents[2]
    manifest_path = root / "tmp/verification-runtime/market-captures/manifest-20261004T081958687901Z.json"
    if not manifest_path.exists():
        pytest.skip("local genuine market evidence is not available")
    manifest_bytes = manifest_path.read_bytes()
    prior = json.loads((root / "local/market-replay-2026-10-04.json").read_bytes())
    assert hashlib.sha256(manifest_bytes).hexdigest() == prior["manifest_sha256"]
    objects, repository = storage
    records = []
    for item in json.loads(manifest_bytes)["captures"]:
        body = (manifest_path.parent / item["body_file"]).read_bytes()
        assert len(body) == item["byte_size"] and hashlib.sha256(body).hexdigest() == item["sha256"]
        records.append(retain_response(requested_url=item["requested_url"], response_url=item["response_url"],
            started_at=datetime.fromisoformat(item["started_at"]), captured_at=datetime.fromisoformat(item["captured_at"]),
            status_code=item["status_code"], media_type=item["media_type"], etag=item["etag"], last_modified=item["last_modified"],
            body=body, objects=objects, repository=repository))
    artifact = retain_recipe(records, objects, repository)
    outputs = []
    for _ in range(2):
        transport = MarketReplay(artifact.content_sha256, objects, repository)
        provider = YahooPriceProvider(http=transport, clock=transport.now)
        values = {ticker: provider.history(ticker, expected_currency=currency) for ticker, currency in
                  (("ABT", "USD"), ("NTES", "USD"), ("TRP", "USD"), ("MFG", "USD"), ("6752.T", "JPY"))}
        values.update({f"USD/{counter}": provider.exchange_rate_history("USD", counter) for counter in ("JPY", "CAD")})
        result = json.loads(json.dumps({key: asdict(value) for key, value in values.items()}, default=str))
        assert result == prior["values"]
        assert len(transport.requests) == 12
        outputs.append(result)
    assert outputs[0] == outputs[1]
