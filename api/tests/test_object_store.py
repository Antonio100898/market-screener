from io import BytesIO

import pytest
from botocore.exceptions import ClientError
from botocore.exceptions import IncompleteReadError
from botocore.response import StreamingBody

from screener.object_store import (
    ImmutableObjectStore,
    ObjectVerificationError,
    content_address,
)


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.put_count = 0
        self.fail_put = False

    def get_object(self, *, Bucket, Key):
        del Bucket
        try:
            value = self.objects[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            ) from None
        return {"Body": BytesIO(value)}

    def put_object(self, *, Bucket, Key, Body, ContentType, Metadata, IfNoneMatch):
        del Bucket, ContentType, Metadata
        assert IfNoneMatch == "*"
        if self.fail_put:
            raise RuntimeError("upload failed")
        self.put_count += 1
        self.objects[Key] = bytes(Body)


def test_content_address_is_sha256_key():
    digest, key = content_address(b"filing")

    assert len(digest) == 64
    assert key == f"raw/sha256/{digest}"


def test_repeated_bytes_upload_once_and_verify_each_read():
    client = MemoryS3()
    store = ImmutableObjectStore(client, "evidence")

    first = store.put_verified(b"same filing", "application/pdf")
    second = store.put_verified(b"same filing", "application/pdf")

    assert first.object_key == second.object_key
    assert client.put_count == 1
    assert store.read_verified(first.object_key, first.content_sha256, 11) == b"same filing"


def test_different_bytes_use_different_keys():
    client = MemoryS3()
    store = ImmutableObjectStore(client, "evidence")

    first = store.put_verified(b"first", "text/plain")
    second = store.put_verified(b"second", "text/plain")

    assert first.object_key != second.object_key
    assert client.put_count == 2


def test_existing_corrupt_object_is_not_overwritten():
    client = MemoryS3()
    digest, key = content_address(b"expected")
    client.objects[key] = b"corrupt"
    store = ImmutableObjectStore(client, "evidence")

    with pytest.raises(ObjectVerificationError, match="hash|bytes"):
        store.put_verified(b"expected", "text/plain")

    assert client.objects[key] == b"corrupt"
    assert client.put_count == 0


def test_upload_failure_is_reported_before_verification():
    client = MemoryS3()
    client.fail_put = True
    store = ImmutableObjectStore(client, "evidence")

    with pytest.raises(RuntimeError, match="upload failed"):
        store.put_verified(b"filing", "application/pdf")

    assert client.objects == {}


class CountingStream(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []
        self.bytes_read = 0

    def read(self, amount=None):
        self.read_sizes.append(amount)
        result = super().read(amount)
        self.bytes_read += len(result)
        return result


def streaming_store(raw, declared_size):
    class Client:
        def get_object(self, **_kwargs):
            return {"Body": StreamingBody(raw, declared_size)}
    return ImmutableObjectStore(Client(), "evidence")


def test_oversized_actual_stream_is_bounded_before_rejecting():
    class LazyOversizedStream(CountingStream):
        def __init__(self):
            super().__init__(b"")
            self.remaining = 1024 * 1024

        def read(self, amount=None):
            self.read_sizes.append(amount)
            length = self.remaining if amount is None else min(amount, self.remaining)
            self.remaining -= length
            self.bytes_read += length
            return b"x" * length

    raw = LazyOversizedStream()
    store = streaming_store(raw, raw.remaining)
    digest, key = content_address(b"same")
    with pytest.raises(ObjectVerificationError, match="bytes"):
        store.read_verified(key, digest, 4)
    assert raw.read_sizes == [5]
    assert raw.bytes_read == 5
    assert raw.closed


@pytest.mark.parametrize("expected,actual,error", [
    (b"same", b"same", None), (b"", b"", None),
    (b"same", b"sam", "bytes"), (b"same", b"fail", "hash"),
])
def test_streaming_exact_size_hash_and_close(expected, actual, error):
    raw = CountingStream(actual)
    store = streaming_store(raw, len(actual))
    digest, key = content_address(expected)
    if error:
        with pytest.raises(ObjectVerificationError, match=error):
            store.read_verified(key, digest, len(expected))
    else:
        assert store.read_verified(key, digest, len(expected)) == expected
    assert raw.closed
    assert all(type(size) is int and size > 0 for size in raw.read_sizes)
    assert raw.bytes_read <= len(expected) + 1


def test_exact_body_with_truncated_declared_length_still_fails_and_closes():
    raw = CountingStream(b"same")
    store = streaming_store(raw, 99)
    digest, key = content_address(b"same")
    with pytest.raises(IncompleteReadError):
        store.read_verified(key, digest, 4)
    assert raw.read_sizes == [5, 1]
    assert raw.bytes_read == 4
    assert raw.closed


def test_extra_byte_after_exact_first_read_is_rejected():
    class ShortFirstRead(CountingStream):
        def read(self, amount=None):
            if not self.read_sizes and amount is not None:
                return super().read(4)
            return super().read(amount)

    raw = ShortFirstRead(b"same+")
    digest, key = content_address(b"same")
    with pytest.raises(ObjectVerificationError, match="bytes"):
        streaming_store(raw, 5).read_verified(key, digest, 4)
    assert raw.bytes_read == 5
    assert raw.closed


def test_stream_read_error_closes_without_verification():
    class BrokenStream(CountingStream):
        def read(self, amount=None):
            raise RuntimeError("stream unavailable")

    raw = BrokenStream(b"same")
    digest, key = content_address(b"same")
    with pytest.raises(RuntimeError, match="stream unavailable"):
        streaming_store(raw, 4).read_verified(key, digest, 4)
    assert raw.closed


@pytest.mark.parametrize("invalid", [-1, True, 4.0, "4", None])
def test_invalid_expected_size_does_not_open_storage(invalid):
    class ForbiddenClient:
        def get_object(self, **_kwargs):
            raise AssertionError("invalid expected size opened storage")

    digest, key = content_address(b"same")
    with pytest.raises(ObjectVerificationError, match="nonnegative integer"):
        ImmutableObjectStore(ForbiddenClient(), "evidence").read_verified(key, digest, invalid)


def test_existing_verified_body_returns_once_and_next_call_verifies_fresh():
    class Client(MemoryS3):
        def __init__(self):
            super().__init__()
            self.reads, self.streams = 0, []
        def get_object(self, **kwargs):
            self.reads += 1
            if self.reads > 1:
                raise AssertionError("existing verified body was fetched twice")
            response = super().get_object(**kwargs)
            self.streams.append(response["Body"])
            return response
    client = Client()
    digest, key = content_address(b"same filing")
    client.objects[key] = b"same filing"
    store = ImmutableObjectStore(client, "evidence")
    verified = store.put_verified(b"same filing", "application/pdf")
    assert (verified.content_sha256, verified.object_key, verified.byte_size) == (digest, key, 11)
    assert client.put_count == 0 and client.reads == 1 and client.streams[0].closed
    client.reads = 0
    client.objects[key] = b"corrupt now"
    with pytest.raises(ObjectVerificationError, match="hash|bytes"):
        store.put_verified(b"same filing", "text/plain")
    assert client.reads == 1 and client.put_count == 0 and client.streams[-1].closed


@pytest.mark.parametrize("race", [False, True])
def test_new_or_racing_upload_verifies_readback_once(race):
    class Client(MemoryS3):
        def __init__(self):
            super().__init__()
            self.reads, self.streams = 0, []
        def get_object(self, **kwargs):
            self.reads += 1
            response = super().get_object(**kwargs)
            self.streams.append(response["Body"])
            return response
        def put_object(self, **kwargs):
            super().put_object(**kwargs)
            if race:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    client = Client()
    verified = ImmutableObjectStore(client, "evidence").put_verified(b"filing", "text/plain")
    assert verified.content_sha256 == content_address(b"filing")[0]
    assert client.reads == 2 and client.put_count == 1  # Missing GET then one verified body readback.
    assert len(client.streams) == 1 and client.streams[0].closed


def test_corrupt_new_upload_readback_never_returns_verified_object():
    class Client(MemoryS3):
        def put_object(self, **kwargs):
            super().put_object(**kwargs)
            self.objects[kwargs["Key"]] = b"broken"
    client = Client()
    with pytest.raises(ObjectVerificationError, match="hash|bytes"):
        ImmutableObjectStore(client, "evidence").put_verified(b"filing", "text/plain")
    assert client.put_count == 1
