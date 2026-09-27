from io import BytesIO

import pytest
from botocore.exceptions import ClientError

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
