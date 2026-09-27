import hashlib
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import create_postgres_engine, evidence_artifact
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)


class FailingObjectStore:
    def put_verified(self, data, media_type):
        del data, media_type
        raise RuntimeError("forced upload failure")


def test_real_storage_migration_upload_read_and_idempotency():
    settings = StorageSettings.from_env()
    alembic = Config("alembic.ini")
    command.upgrade(alembic, "head")

    engine = create_postgres_engine(settings)
    client = create_s3_client(settings)
    objects = ImmutableObjectStore(client, settings.s3_bucket)
    repository = SqlArtifactRepository(engine)
    payload = b"market-screener-integration-evidence-v2"

    first = store_evidence(payload, "application/octet-stream", objects, repository)
    second = store_evidence(payload, "application/octet-stream", objects, repository)

    assert first.content_sha256 == second.content_sha256
    assert objects.read_verified(
        first.object_key, first.content_sha256, first.byte_size
    ) == payload
    with engine.connect() as connection:
        count = connection.scalar(
            select(func.count()).select_from(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == first.content_sha256
            )
        )
    assert count == 1

    failed_payload = f"forced-failure-{uuid4()}".encode()
    with pytest.raises(RuntimeError, match="forced upload failure"):
        store_evidence(
            failed_payload,
            "application/octet-stream",
            FailingObjectStore(),
            repository,
        )
    failed_hash = hashlib.sha256(failed_payload).hexdigest()
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == failed_hash
            )
        ) == 0

    engine.dispose()
