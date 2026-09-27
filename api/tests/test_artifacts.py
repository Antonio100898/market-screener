from datetime import datetime, timezone

import pytest

from screener.artifacts import EvidenceArtifact, store_evidence
from screener.object_store import VerifiedObject, content_address


class FakeObjectStore:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def put_verified(self, data, media_type):
        del media_type
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        digest, key = content_address(data)
        return VerifiedObject(digest, key, len(data), datetime.now(timezone.utc))


class MemoryRepository:
    def __init__(self, failure=None):
        self.failure = failure
        self.rows = {}

    def add_verified(self, verified, media_type):
        if self.failure is not None:
            raise self.failure
        row = EvidenceArtifact(
            verified.content_sha256,
            verified.object_key,
            verified.byte_size,
            media_type,
            datetime.now(timezone.utc),
            verified.verified_at,
        )
        return self.rows.setdefault(verified.content_sha256, row)


def test_verified_object_is_recorded_idempotently():
    objects = FakeObjectStore()
    repository = MemoryRepository()

    first = store_evidence(b"filing", "application/pdf", objects, repository)
    second = store_evidence(b"filing", "application/pdf", objects, repository)

    assert first == second
    assert len(repository.rows) == 1


def test_upload_failure_creates_no_database_row():
    objects = FakeObjectStore(RuntimeError("upload failed"))
    repository = MemoryRepository()

    with pytest.raises(RuntimeError, match="upload failed"):
        store_evidence(b"filing", "application/pdf", objects, repository)

    assert repository.rows == {}


def test_database_failure_does_not_modify_verified_object():
    objects = FakeObjectStore()
    repository = MemoryRepository(RuntimeError("database failed"))

    with pytest.raises(RuntimeError, match="database failed"):
        store_evidence(b"filing", "application/pdf", objects, repository)

    assert objects.calls == 1
