import hashlib
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository, LeaseLost, RetryPolicy
from screener.job_runtime import DurableJobRuntime, JobContext, ScheduledOccurrence
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (
    create_postgres_engine,
    durable_job,
    durable_job_item,
    evidence_artifact,
    job_schedule_occurrence,
    source_item,
    source_observation,
)
from screener.sec_ingestion import SecRecentDiscoveryHandler
from screener.source_observations import SourceObservationRepository
from screener.sources.edgar import EdgarTransportResult
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)

NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)
HEADER = (
    "Form Type   Company Name                                                  "
    "CIK         Date Filed  File Name"
)


@pytest.fixture(scope="module")
def database_url():
    settings = StorageSettings.from_env()
    database_name = f"ms_sec_ingestion_{uuid4().hex}"
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
def storage(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE source_observation, source_item, durable_job_item, "
                "durable_job, job_schedule_occurrence, evidence_artifact "
                "RESTART IDENTITY CASCADE"
            )
        )
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    yield engine, objects
    engine.dispose()


class FixtureEdgar:
    def __init__(self, responses):
        self.responses = responses

    def fetch(self, url):
        return self.responses[url]


def _row(form, company, cik, filed, accession):
    return (
        f"{form:<12}{company:<62}{cik:<12}{filed:<12}"
        f"edgar/data/{int(cik)}/{accession}.txt"
    )


def _index(*rows):
    separator = (
        f"{'-' * 10}  {'-' * 60}  {'-' * 10}  {'-' * 10}  {'-' * 50}"
    )
    return (
        "Description: Daily Index of EDGAR Dissemination Feed\n\n"
        f"{HEADER}\n{separator}\n"
        + "\n".join(rows)
        + "\n"
    ).encode()


def _url(day):
    return (
        "https://www.sec.gov/Archives/edgar/daily-index/"
        f"{day.year}/QTR{(day.month - 1) // 3 + 1}/form.{day:%Y%m%d}.idx"
    )


def _responses():
    first = date(2026, 9, 28)
    second = date(2026, 9, 29)
    payloads = {
        first: _index(
            _row(
                "10-K",
                "DOMESTIC CORP",
                "1234",
                first.isoformat(),
                "0000001234-26-000001",
            ),
            _row(
                "20-F/A",
                "FOREIGN PLC",
                "987654",
                first.isoformat(),
                "0000987654-26-000002",
            ),
        ),
        second: _index(
            _row(
                "10-QT/A",
                "TRANSITION INC",
                "42",
                second.isoformat(),
                "0000000042-26-000003",
            )
        ),
    }
    return {
        _url(day): EdgarTransportResult(
            data=payload,
            url=_url(day),
            media_type="text/plain",
            etag=f'"{day.isoformat()}"',
            last_modified=None,
        )
        for day, payload in payloads.items()
    }, payloads


def _repository(engine):
    return DurableJobRepository(
        engine,
        retry_policy=RetryPolicy(jitter_seconds=lambda _cap: 0),
    )


def _handler(engine, objects, jobs, edgar, clock=lambda: NOW):
    return SecRecentDiscoveryHandler(
        edgar=edgar,
        object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=clock),
        jobs=jobs,
        clock=clock,
    )


def _wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def _status(engine, schedule_key):
    with engine.connect() as connection:
        return connection.scalar(
            select(durable_job.c.status)
            .join(
                job_schedule_occurrence,
                durable_job.c.occurrence_id
                == job_schedule_occurrence.c.occurrence_id,
            )
            .where(job_schedule_occurrence.c.schedule_key == schedule_key)
        )


def _run(engine, handler, occurrence):
    runtime = DurableJobRuntime(
        _repository(engine),
        lambda _now: (occurrence,),
        {"sec-recent-discovery": handler},
        poll_interval=timedelta(milliseconds=10),
        lease_duration=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=200),
        clock=lambda: NOW,
    )
    try:
        runtime.start()
        _wait_for(lambda: _status(engine, occurrence.schedule_key) == "succeeded")
    finally:
        assert runtime.stop(3)


def _occurrence(key, start, end):
    return ScheduledOccurrence(
        schedule_key=key,
        scheduled_for=NOW,
        kind="sec-recent-discovery",
        parameters={"start_date": start.isoformat(), "end_date": end.isoformat()},
        due_at=NOW,
        priority=30,
    )


def _counts(engine):
    with engine.connect() as connection:
        return {
            "artifacts": connection.scalar(
                select(func.count()).select_from(evidence_artifact)
            ),
            "items": connection.scalar(select(func.count()).select_from(source_item)),
            "observations": connection.scalar(
                select(func.count()).select_from(source_observation)
            ),
            "children": connection.scalar(
                select(func.count())
                .select_from(durable_job)
                .where(durable_job.c.kind == "sec-resource-fetch")
            ),
        }


def test_overlap_replay_and_repository_runtime_restart_are_idempotent(storage):
    engine, objects = storage
    responses, payloads = _responses()
    edgar = FixtureEdgar(responses)
    first = date(2026, 9, 28)
    second = date(2026, 9, 29)

    _run(
        engine,
        _handler(engine, objects, _repository(engine), edgar),
        _occurrence("sec:first", first, second),
    )
    _run(
        engine,
        _handler(engine, objects, _repository(engine), edgar),
        _occurrence("sec:overlap", second, second),
    )
    expected = {"artifacts": 2, "items": 5, "observations": 5, "children": 3}
    assert _counts(engine) == expected

    for payload in payloads.values():
        digest = hashlib.sha256(payload).hexdigest()
        with engine.connect() as connection:
            artifact = connection.execute(
                select(evidence_artifact).where(
                    evidence_artifact.c.content_sha256 == digest
                )
            ).mappings().one()
        assert objects.read_verified(
            artifact["object_key"], digest, artifact["byte_size"]
        ) == payload

    database_url = engine.url
    engine.dispose()
    restarted = create_engine(database_url)
    restarted_objects = ImmutableObjectStore(
        create_s3_client(StorageSettings.from_env()),
        StorageSettings.from_env().s3_bucket,
    )
    try:
        _run(
            restarted,
            _handler(
                restarted,
                restarted_objects,
                _repository(restarted),
                FixtureEdgar(responses),
            ),
            _occurrence("sec:after-restart", first, second),
        )
        assert _counts(restarted) == expected
    finally:
        restarted.dispose()


class InterruptOnceJobs:
    def __init__(self, jobs):
        self.jobs = jobs
        self.interrupted = False

    def enqueue_child(self, *args, **kwargs):
        if not self.interrupted:
            self.interrupted = True
            raise RuntimeError("forced interruption")
        return self.jobs.enqueue_child(*args, **kwargs)


def _context(claimed, repository, clock=lambda: NOW):
    return JobContext(
        job=claimed.job,
        stopping=threading.Event(),
        _repository=repository,
        _lease=claimed.lease,
        _clock=clock,
    )


def test_interruption_after_observation_recovers_without_loss(storage):
    engine, objects = storage
    responses, _payloads = _responses()
    day = date(2026, 9, 29)
    jobs = _repository(engine)
    stored = jobs.enqueue_scheduled(
        schedule_key="sec:interrupted",
        scheduled_for=NOW,
        kind="sec-recent-discovery",
        parameters={"start_date": day.isoformat(), "end_date": day.isoformat()},
        due_at=NOW,
        priority=30,
    )
    claimed = jobs.claim_next(owner="worker-1", lease_duration=LEASE, now=NOW)
    assert claimed is not None and claimed.job.job_id == stored.job_id
    interrupting = InterruptOnceJobs(jobs)
    handler = _handler(
        engine,
        objects,
        interrupting,
        FixtureEdgar(responses),
    )

    with pytest.raises(RuntimeError, match="forced interruption"):
        handler(_context(claimed, jobs))
    assert _counts(engine) == {
        "artifacts": 1,
        "items": 2,
        "observations": 2,
        "children": 0,
    }
    assert claimed.job.checkpoint is None

    jobs.fail(claimed.lease, error_summary="forced interruption", now=NOW)
    replacement = jobs.claim_next(
        owner="worker-2", lease_duration=LEASE, now=NOW
    )
    assert replacement is not None
    checkpoint = handler(_context(replacement, jobs))
    jobs.complete(replacement.lease, final_checkpoint=checkpoint, now=NOW)

    assert _counts(engine) == {
        "artifacts": 1,
        "items": 2,
        "observations": 2,
        "children": 1,
    }
    with engine.connect() as connection:
        current = connection.execute(
            select(durable_job).where(durable_job.c.job_id == stored.job_id)
        ).mappings().one()
    assert current["checkpoint"]["through_date"] == day.isoformat()


def test_expired_owner_leaves_only_verified_orphan_artifact(storage):
    engine, objects = storage
    responses, payloads = _responses()
    day = date(2026, 9, 29)
    jobs = _repository(engine)
    stored = jobs.enqueue_scheduled(
        schedule_key="sec:expired",
        scheduled_for=NOW,
        kind="sec-recent-discovery",
        parameters={"start_date": day.isoformat(), "end_date": day.isoformat()},
        due_at=NOW,
    )
    claimed = jobs.claim_next(
        owner="expired-worker",
        lease_duration=timedelta(seconds=1),
        now=NOW,
    )
    assert claimed is not None and claimed.job.job_id == stored.job_id
    late = NOW + timedelta(seconds=2)
    handler = _handler(
        engine,
        objects,
        jobs,
        FixtureEdgar(responses),
        clock=lambda: late,
    )

    with pytest.raises(LeaseLost):
        handler(_context(claimed, jobs, clock=lambda: late))

    assert _counts(engine) == {
        "artifacts": 1,
        "items": 0,
        "observations": 0,
        "children": 0,
    }
    assert jobs.claim_next(
        owner="not-yet-recovered", lease_duration=LEASE, now=late
    ) is None
    payload = payloads[day]
    digest = hashlib.sha256(payload).hexdigest()
    with engine.connect() as connection:
        artifact = connection.execute(
            select(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == digest
            )
        ).mappings().one()
        assert connection.scalar(
            select(func.count()).select_from(durable_job_item)
        ) == 0
    assert objects.read_verified(
        artifact["object_key"], digest, artifact["byte_size"]
    ) == payload
