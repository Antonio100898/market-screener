import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.object_store import (
    ImmutableObjectStore,
    ObjectVerificationError,
    create_s3_client,
)
from screener.postgres import (
    create_postgres_engine,
    durable_job,
    durable_job_item,
    evidence_artifact,
    source_item,
    source_observation,
)
from screener.source_observations import (
    SourceItemIdentity,
    SourceItemIdentityConflict,
    SourceObservationIdentityConflict,
    SourceObservationRepository,
)
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


@pytest.fixture(scope="module")
def database_url():
    settings = StorageSettings.from_env()
    database_name = f"ms_observations_{uuid4().hex}"
    admin_engine = create_postgres_engine(settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    url = make_url(settings.database_url).set(database=database_name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    previous_url = os.environ.get("SCREENER_DATABASE_URL")
    try:
        os.environ["SCREENER_DATABASE_URL"] = url.render_as_string(
            hide_password=False
        )
        command.upgrade(Config("alembic.ini"), "head")
        yield url
    finally:
        if previous_url is None:
            os.environ.pop("SCREENER_DATABASE_URL", None)
        else:
            os.environ["SCREENER_DATABASE_URL"] = previous_url
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture
def repositories(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE source_observation, source_item, durable_job_item, "
                "durable_job, job_schedule_occurrence, evidence_artifact "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield (
        engine,
        DurableJobRepository(engine),
        SourceObservationRepository(engine, clock=lambda: NOW),
    )
    engine.dispose()


def _claim(jobs, key, owner="worker"):
    jobs.enqueue_scheduled(
        schedule_key=key,
        scheduled_for=NOW,
        kind="source-discovery",
        parameters={"source": key},
        due_at=NOW,
        deadline_at=NOW + timedelta(days=1),
    )
    claimed = jobs.claim_next(owner=owner, lease_duration=LEASE, now=NOW)
    assert claimed is not None
    return claimed.lease


def _pending(observations, lease, item, outcome_key, **overrides):
    values = {
        "item": item,
        "item_outcome_key": outcome_key,
        "state": "pending",
        "metadata": {"form": "20-F"},
        "source_url": "https://www.sec.gov/Archives/0001.txt",
        "detected_at": NOW,
    }
    values.update(overrides)
    return observations.record(lease, **values)


def test_concurrent_duplicate_delivery_reuses_one_observation(repositories):
    engine, jobs, observations = repositories
    first_lease = _claim(jobs, "duplicate-a", "worker-a")
    second_lease = _claim(jobs, "duplicate-b", "worker-b")
    item = SourceItemIdentity("SEC", "filing", "0001-26-000001")
    barrier = Barrier(2)

    def record(values):
        lease, key, detected_at = values
        barrier.wait()
        return _pending(
            observations,
            lease,
            item,
            key,
            detected_at=detected_at,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(
            executor.map(
                record,
                (
                    (first_lease, "a", NOW),
                    (second_lease, "b", NOW + timedelta(seconds=1)),
                ),
            )
        )

    assert first.item.source_item_id == second.item.source_item_id
    assert (
        first.observation.source_observation_id
        == second.observation.source_observation_id
    )
    assert {first.created, second.created} == {True, False}
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_item)) == 1
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(durable_job_item)
        ) == 2


def test_revision_history_parent_and_issuer_identity(repositories):
    _engine, jobs, observations = repositories
    lease = _claim(jobs, "history")
    parent = _pending(
        observations,
        lease,
        SourceItemIdentity("EDINET", "aggregate", "documents/2026-09-29"),
        "aggregate",
        source_url=(
            "https://disclosure2.edinet-fsa.go.jp/api/v2/documents.json"
            "?date=2026-09-29&type=2"
        ),
    )
    child_identity = SourceItemIdentity(
        "EDINET",
        "resource",
        "S100TEST/xbrl.zip",
        issuer_source_identifier="E12345",
        parent_source_item_id=parent.item.source_item_id,
    )
    pending = _pending(observations, lease, child_identity, "pending")
    digest = "a" * 64
    with observations.engine.begin() as connection:
        connection.execute(
            evidence_artifact.insert().values(
                content_sha256=digest,
                object_key=f"raw/sha256/{digest}",
                byte_size=1,
                media_type="application/zip",
                verified_at=NOW,
            )
        )
    present = observations.record(
        lease,
        item=child_identity,
        item_outcome_key="present",
        state="present",
        metadata={"form": "annual", "revision": 1},
        artifact_sha256=digest,
        source_url=(
            "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/"
            "S100TEST?type=1"
        ),
        etag='"v1"',
        source_published_at=NOW - timedelta(hours=1),
        detected_at=NOW + timedelta(seconds=1),
    )
    changed_metadata = observations.record(
        lease,
        item=child_identity,
        item_outcome_key="metadata-change",
        state="present",
        metadata={"form": "annual", "revision": 2},
        artifact_sha256=digest,
        source_url=(
            "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/"
            "S100TEST?type=1"
        ),
        etag='"v1"',
        source_published_at=NOW - timedelta(hours=1),
        detected_at=NOW + timedelta(seconds=2),
    )
    removed = observations.record(
        lease,
        item=child_identity,
        item_outcome_key="removed",
        state="removed",
        metadata={"reason": "withdrawn"},
        source_url=(
            "https://disclosure2.edinet-fsa.go.jp/api/v2/documents/"
            "S100TEST?type=1"
        ),
        detected_at=NOW + timedelta(seconds=3),
    )

    history = observations.history(pending.item.source_item_id)
    assert [entry.state for entry in history] == [
        "pending",
        "present",
        "present",
        "removed",
    ]
    assert len({entry.observation_sha256 for entry in history}) == 4
    assert (
        present.observation.artifact_sha256
        == changed_metadata.observation.artifact_sha256
    )
    assert pending.item.issuer_source_identifier == "E12345"
    assert pending.item.parent_source_item_id == parent.item.source_item_id
    assert removed.observation.artifact_sha256 is None


def test_expired_and_stale_leases_write_nothing(repositories):
    engine, jobs, observations = repositories
    expired = _claim(jobs, "expired")
    expired_observations = SourceObservationRepository(
        engine,
        clock=lambda: NOW + LEASE,
    )
    with pytest.raises(LeaseLost):
        _pending(
            expired_observations,
            expired,
            SourceItemIdentity("SEC", "inventory", "expired"),
            "expired",
            detected_at=NOW - timedelta(hours=1),
        )
    jobs.recover_expired(now=NOW + LEASE)
    with pytest.raises(LeaseLost):
        _pending(
            observations,
            expired,
            SourceItemIdentity("SEC", "inventory", "stale"),
            "stale",
            detected_at=NOW + LEASE + timedelta(seconds=1),
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_item)) == 0
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 0
        assert connection.scalar(
            select(func.count()).select_from(durable_job_item)
        ) == 0


def test_database_constraints_and_unknown_artifact(repositories):
    engine, jobs, observations = repositories
    lease = _claim(jobs, "constraints")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                source_item.insert().values(
                    source_system="OTHER",
                    item_kind="filing",
                    source_key="x",
                )
            )
    with pytest.raises(IntegrityError):
        observations.record(
            lease,
            item=SourceItemIdentity("SEC", "resource", "unknown-artifact"),
            item_outcome_key="unknown-artifact",
            state="present",
            metadata={},
            artifact_sha256="f" * 64,
            source_url="https://example.test/resource",
            detected_at=NOW,
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_item)) == 0
        assert connection.scalar(
            select(func.count()).select_from(durable_job_item)
        ) == 0


def test_database_enforces_observation_constraints(repositories):
    engine, jobs, observations = repositories
    lease = _claim(jobs, "observation-constraints")
    stored = _pending(
        observations,
        lease,
        SourceItemIdentity("SEC", "filing", "0001-26-000099"),
        "valid",
    )
    artifact = "a" * 64
    with engine.begin() as connection:
        connection.execute(
            evidence_artifact.insert().values(
                content_sha256=artifact,
                object_key=f"raw/sha256/{artifact}",
                byte_size=1,
                media_type="application/octet-stream",
                verified_at=NOW,
            )
        )
    base = {
        "source_item_id": stored.item.source_item_id,
        "observation_sha256": "b" * 64,
        "state": "pending",
        "canonical_metadata": {},
        "artifact_sha256": None,
        "source_url": "https://example.test/resource",
        "detected_at": NOW,
        "detecting_job_id": lease.job_id,
    }
    invalid = (
        {**base, "state": "present"},
        {**base, "observation_sha256": "c" * 64, "artifact_sha256": artifact},
        {**base, "observation_sha256": "d" * 64, "canonical_metadata": []},
        {**base, "observation_sha256": "E" * 64},
        {**base, "observation_sha256": "f" * 64, "source_url": " "},
    )
    for values in invalid:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(source_observation.insert().values(**values))
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 1


def test_reuse_detects_stored_identity_conflicts(repositories):
    engine, jobs, observations = repositories
    lease = _claim(jobs, "identity-conflicts")
    identity = SourceItemIdentity(
        "SEC",
        "filing",
        "0001-26-000100",
        issuer_source_identifier="0000000001",
    )
    stored = _pending(observations, lease, identity, "first")
    with pytest.raises(SourceItemIdentityConflict, match="different immutable content"):
        _pending(
            observations,
            lease,
            SourceItemIdentity(
                "SEC",
                "filing",
                "0001-26-000100",
                issuer_source_identifier="0000000002",
            ),
            "changed-issuer",
        )
    with engine.begin() as connection:
        connection.execute(
            source_observation.update()
            .where(
                source_observation.c.source_observation_id
                == stored.observation.source_observation_id
            )
            .values(canonical_metadata={"tampered": True})
        )
    with pytest.raises(SourceObservationIdentityConflict, match="canonical identity"):
        _pending(observations, lease, identity, "tampered")


def test_parent_must_belong_to_same_source_system(repositories):
    engine, jobs, observations = repositories
    lease = _claim(jobs, "parent-system")
    parent = _pending(
        observations,
        lease,
        SourceItemIdentity("SEC", "aggregate", "submissions/0000000001"),
        "parent",
    )
    with pytest.raises(
        SourceItemIdentityConflict,
        match="parent source item must belong to the same source system",
    ):
        _pending(
            observations,
            lease,
            SourceItemIdentity(
                "EDINET",
                "resource",
                "S100TEST/xbrl.zip",
                issuer_source_identifier="E12345",
                parent_source_item_id=parent.item.source_item_id,
            ),
            "cross-source-child",
        )
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_item)) == 1
        assert connection.scalar(
            select(func.count()).select_from(durable_job_item).where(
                durable_job_item.c.item_key == "cross-source-child"
            )
        ) == 0


class FailingObjectStore:
    def put_verified(self, data, media_type):
        del data, media_type
        raise RuntimeError("forced upload failure")


class MismatchedObjectStore:
    def put_verified(self, data, media_type):
        del data, media_type
        raise ObjectVerificationError("forced readback mismatch")


@pytest.mark.parametrize(
    ("store", "error"),
    [
        (FailingObjectStore(), RuntimeError),
        (MismatchedObjectStore(), ObjectVerificationError),
    ],
)
def test_failed_or_mismatched_upload_cannot_create_observation(
    repositories, store, error
):
    engine, _jobs, _observations = repositories
    with pytest.raises(error):
        store_evidence(
            b"unverified",
            "application/octet-stream",
            store,
            SqlArtifactRepository(engine),
        )
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(evidence_artifact)
        ) == 0
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 0


def test_verified_s3_object_links_to_present_observation(repositories):
    engine, jobs, observations = repositories
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    payload = f"source-observation-{uuid4()}".encode()
    artifact = store_evidence(
        payload,
        "application/octet-stream",
        objects,
        SqlArtifactRepository(engine),
    )
    stored = observations.record(
        _claim(jobs, "s3"),
        item=SourceItemIdentity("SEC", "resource", artifact.object_key),
        item_outcome_key=artifact.object_key,
        state="present",
        metadata={"byte_size": len(payload)},
        artifact_sha256=artifact.content_sha256,
        source_url="https://www.sec.gov/Archives/source.bin",
        detected_at=NOW,
    )

    assert stored.observation.artifact_sha256 == hashlib.sha256(payload).hexdigest()
    assert objects.read_verified(
        artifact.object_key, artifact.content_sha256, artifact.byte_size
    ) == payload


def test_upgrade_populated_0005_database_to_0006(monkeypatch):
    settings = StorageSettings.from_env()
    database_name = f"ms_observation_migration_{uuid4().hex}"
    admin_engine = create_postgres_engine(settings).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    url = make_url(settings.database_url).set(database=database_name)
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    engine = None
    try:
        monkeypatch.setenv(
            "SCREENER_DATABASE_URL", url.render_as_string(hide_password=False)
        )
        alembic = Config("alembic.ini")
        command.upgrade(alembic, "20260929_0005")
        engine = create_engine(url)
        jobs = DurableJobRepository(engine)
        stored = jobs.enqueue_scheduled(
            schedule_key="preserved",
            scheduled_for=NOW,
            kind="source-discovery",
            parameters={},
            due_at=NOW,
        )
        engine.dispose()
        engine = None
        command.upgrade(alembic, "head")
        engine = create_engine(url)
        with engine.connect() as connection:
            assert MigrationContext.configure(connection).get_current_revision() == (
                "20260929_0006"
            )
            assert connection.scalar(
                select(func.count()).select_from(durable_job).where(
                    durable_job.c.job_id == stored.job_id
                )
            ) == 1
            assert connection.scalar(select(func.count()).select_from(source_item)) == 0
    finally:
        if engine is not None:
            engine.dispose()
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()
