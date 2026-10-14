import gzip
import tracemalloc

import httpx
import pytest

from screener.sources import edgar as owner


URL = "https://www.sec.gov/requested"


class ObservedStream(httpx.SyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.read_bytes = 0
        self.read_chunks = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read_bytes += len(chunk)
            self.read_chunks += 1
            yield chunk
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


@pytest.fixture
def client_factory(tmp_path, monkeypatch):
    clients, sleeps = [], []
    clock = iter(10 + index / 5 for index in range(10000))
    monkeypatch.setattr(owner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(owner.time, "sleep", sleeps.append)

    def create(handler, **options):
        client = owner.EdgarClient(cache_dir=tmp_path)
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(handler),
                                   follow_redirects=True, **options)
        clients.append(client)
        return client, sleeps

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize("payload,maximum", [(b"", 1), (b"abc", 3), (b"\x00exact\xff", 7)])
def test_exact_boundary_retains_actual_bytes_metadata_and_closes(client_factory, payload, maximum):
    stream = ObservedStream([payload])
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream, headers={
            "Content-Type": "text/plain; charset=iso-8859-1", "ETag": '"actual-v1"',
            "Last-Modified": "Mon, 28 Sep 2026 20:00:00 GMT"})

    client, _ = client_factory(handler)
    result = client.fetch(URL, maximum_bytes=maximum)
    assert result == owner.EdgarTransportResult(payload, URL, "text/plain; charset=iso-8859-1",
                                                '"actual-v1"', "Mon, 28 Sep 2026 20:00:00 GMT")
    assert stream.closed and stream.read_bytes == len(payload)
    assert requests[0].headers["Accept-Encoding"] == "identity"


@pytest.mark.parametrize("length", [None, "1", "99999999999999999999999999999999999999999"])
def test_actual_stream_enforces_overflow_despite_missing_or_lying_length(client_factory, length):
    stream = ObservedStream([b"abc", b"d", b"unread-tail"])
    headers = {} if length is None else {"Content-Length": length}
    client, sleeps = client_factory(lambda request: httpx.Response(200, headers=headers, stream=stream))
    with pytest.raises(owner.EdgarPayloadTooLarge, match="exceeds 3 bytes"):
        client.fetch(URL, maximum_bytes=3)
    assert stream.closed and stream.read_bytes == 4 and stream.read_chunks == 2
    assert sleeps == []


def test_hostile_long_stream_stops_before_unread_tail(client_factory):
    stream = ObservedStream((b"a" * 65536 for _ in range(1000)))
    client, sleeps = client_factory(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(owner.EdgarPayloadTooLarge):
        client.fetch(URL, maximum_bytes=65536)
    assert stream.closed and stream.read_bytes == 131072 and stream.read_chunks == 2
    assert sleeps == []


def test_one_giant_raw_chunk_refuses_without_rechunking_or_reading_tail(client_factory):
    giant = b"x" * (8 * 1024 * 1024)
    stream = ObservedStream([giant, b"unread-tail"])
    client, sleeps = client_factory(lambda request: httpx.Response(200, stream=stream))
    tracemalloc.start()
    try:
        with pytest.raises(owner.EdgarPayloadTooLarge):
            client.fetch(URL, maximum_bytes=3)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert stream.closed and stream.read_chunks == 1 and stream.read_bytes == len(giant)
    assert peak < 1024 * 1024 and sleeps == []


@pytest.mark.parametrize("length", ["", "invalid", "-1", "3, 3", "1.0"])
def test_malformed_content_length_closes_without_body_or_retry(client_factory, length):
    stream = ObservedStream([b"abc"])
    client, sleeps = client_factory(lambda request: httpx.Response(200, headers={"Content-Length": length}, stream=stream))
    with pytest.raises(owner.EdgarError, match="invalid Content-Length"):
        client.fetch(URL, maximum_bytes=3)
    assert stream.closed and stream.read_bytes == 0 and sleeps == []


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "identity, gzip", ""])
def test_compressed_or_invalid_encoding_refuses_before_decoded_allocation(client_factory, encoding):
    stream = ObservedStream([gzip.compress(b"a" * 1000000)])
    client, sleeps = client_factory(lambda request: httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream))
    with pytest.raises(owner.EdgarError, match="requires identity Content-Encoding"):
        client.fetch(URL, maximum_bytes=3)
    assert stream.closed and stream.read_bytes == 0 and sleeps == []


def test_legacy_uncapped_compression_and_identity_encoding_neighbor(client_factory):
    compressed = ObservedStream([gzip.compress(b"actual decoded data")])
    identity = ObservedStream([b"actual decoded data"])

    def handler(request):
        bounded = request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "identity" if bounded else "gzip"},
                              stream=identity if bounded else compressed)

    client, _ = client_factory(handler)
    assert client.fetch(URL).data == b"actual decoded data"
    assert client.fetch(URL, maximum_bytes=19).data == b"actual decoded data"
    assert compressed.closed and compressed.read_bytes > 0 and identity.closed


def test_redirect_body_never_accumulates_and_final_metadata_is_actual(client_factory):
    redirect = ObservedStream([b"unused" * 1000000])
    final = ObservedStream([b"abc"])
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/requested":
            return httpx.Response(302, headers={"Location": "/final", "Set-Cookie": "proof=actual", "Content-Encoding": "gzip"}, stream=redirect)
        assert request.headers["Cookie"] == "proof=actual"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"ETag": '"final"', "Content-Type": "application/json"}, stream=final)

    client, _ = client_factory(handler)
    assert client.fetch(URL, maximum_bytes=3) == owner.EdgarTransportResult(
        b"abc", "https://www.sec.gov/final", "application/json", '"final"', None)
    assert redirect.closed and redirect.read_bytes == 0 and final.closed
    assert [request.url.path for request in requests] == ["/requested", "/final"]


def test_redirect_limit_uses_existing_attempt_bound_and_closes_every_response(client_factory):
    streams = []

    def handler(request):
        stream = ObservedStream([b"unread"])
        streams.append(stream)
        return httpx.Response(302, headers={"Location": "/again"}, stream=stream)

    client, sleeps = client_factory(handler, max_redirects=2)
    with pytest.raises(owner.EdgarError, match="failed after 4 attempts"):
        client.fetch(URL, maximum_bytes=3)
    assert len(streams) == 12 and all(stream.closed and stream.read_bytes == 0 for stream in streams)
    assert sleeps == [1, 2, 4]


def test_every_bounded_redirect_hop_shares_the_existing_rate_gate(client_factory, monkeypatch):
    monotonic = [0.0]
    events = []

    def sleep(seconds):
        monotonic[0] += seconds

    def handler(request):
        events.append((request.url.path, monotonic[0]))
        return httpx.Response(302 if request.url.path == "/requested" else 200,
                              headers={"Location": "/final"} if request.url.path == "/requested" else {},
                              stream=ObservedStream([b"unused"] if request.url.path == "/requested" else [b"ok"]))

    client, _ = client_factory(handler)
    monkeypatch.setattr(owner.time, "monotonic", lambda: monotonic[0])
    monkeypatch.setattr(owner.time, "sleep", sleep)
    assert client.fetch(URL, maximum_bytes=2).data == b"ok"
    assert events == [("/requested", owner._MIN_INTERVAL), ("/final", 2 * owner._MIN_INTERVAL)]


@pytest.mark.parametrize("maximum", [True, False, 0, -1, 1.0, "3"])
def test_invalid_limit_rejects_before_request(client_factory, maximum):
    def handler(request):
        raise AssertionError("invalid limit reached transport")

    client, sleeps = client_factory(handler)
    with pytest.raises(ValueError, match="positive integer"):
        client.fetch(URL, maximum_bytes=maximum)
    assert sleeps == []


@pytest.mark.parametrize("status,error", [(404, owner.NoXbrlDataError), (400, owner.EdgarError), (403, owner.EdgarError)])
def test_nonretryable_status_closes_without_reading_error_body(client_factory, status, error):
    stream = ObservedStream([b"unread error body"])
    client, sleeps = client_factory(lambda request: httpx.Response(status, stream=stream))
    with pytest.raises(error):
        client.fetch(URL, maximum_bytes=3)
    assert stream.closed and stream.read_bytes == 0 and sleeps == []


@pytest.mark.parametrize("failure", [429, 500, "connect", "read"])
def test_retry_owner_recovers_status_network_and_read_failures(client_factory, failure):
    streams, requests = [], []

    def handler(request):
        requests.append(request)
        failing = len(requests) < 3
        if failing and failure == "connect":
            raise httpx.ConnectError("fixture connection failure", request=request)
        stream = ObservedStream([b"x"] if failing else [b"ok"],
                                httpx.ReadError("fixture read failure", request=request) if failing and failure == "read" else None)
        streams.append(stream)
        return httpx.Response(failure if failing and isinstance(failure, int) else 200, stream=stream)

    client, sleeps = client_factory(handler)
    assert client.fetch(URL, maximum_bytes=2).data == b"ok"
    assert sleeps == [1, 2] and len(requests) == 3 and all(stream.closed for stream in streams)
    if isinstance(failure, int):
        assert all(stream.read_bytes == 0 for stream in streams[:-1])


def test_read_failure_exhaustion_closes_all_attempts(client_factory):
    streams = []

    def handler(request):
        stream = ObservedStream([b"x"], httpx.ReadError("fixture read interruption", request=request))
        streams.append(stream)
        return httpx.Response(200, stream=stream)

    client, sleeps = client_factory(handler)
    with pytest.raises(owner.EdgarError, match="failed after 4 attempts"):
        client.fetch(URL, maximum_bytes=2)
    assert sleeps == [1, 2, 4] and len(streams) == 4 and all(stream.closed for stream in streams)


def test_nontransport_interruption_closes_without_retry(client_factory):
    stream = ObservedStream([b"x"], RuntimeError("fixture interruption"))
    client, sleeps = client_factory(lambda request: httpx.Response(200, stream=stream))
    with pytest.raises(RuntimeError, match="fixture interruption"):
        client.fetch(URL, maximum_bytes=2)
    assert stream.closed and stream.read_bytes == 1 and sleeps == []
