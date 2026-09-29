import hashlib
import json
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

from screener.artifacts import SqlArtifactRepository, store_evidence
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
from screener.sec_ingestion import (
    SecQuarterlyReconciliationHandler,
    SecRecentDiscoveryHandler,
    SecResourceFetchHandler,
    SecResourcePending,
)
from screener.source_observations import (
    SourceItemIdentity,
    SourceItemIdentityConflict,
    SourceObservationRepository,
)
from screener.sources.edgar import EdgarTransportResult, NoXbrlDataError
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
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


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


def _quarterly_url(year=2026, quarter=3):
    return (
        "https://www.sec.gov/Archives/edgar/full-index/"
        f"{year}/QTR{quarter}/form.idx"
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


def _quarterly_handler(engine, objects, jobs, edgar, clock=lambda: NOW):
    return SecQuarterlyReconciliationHandler(
        edgar=edgar,
        object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=clock),
        jobs=jobs,
        clock=clock,
    )


def _resource_handler(engine, objects, edgar, clock=lambda: NOW):
    return SecResourceFetchHandler(
        edgar=edgar,
        object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=clock),
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


def _filing_metadata(accession, company, *, quarter="2026Q3"):
    cik = accession[:10]
    return {
        "form": "10-K",
        "company_name": company,
        "cik": cik,
        "filing_date": "2026-09-28",
        "archive_filename": f"edgar/data/{int(cik)}/{accession}.txt",
        "accession": accession,
        "quarter": quarter,
    }


def test_quarterly_reconciliation_replays_exact_states_and_inventory(storage):
    engine, objects = storage
    jobs = _repository(engine)
    observations = SourceObservationRepository(engine, clock=lambda: NOW)
    seed = jobs.enqueue_scheduled(
        schedule_key="sec:quarter-seed",
        scheduled_for=NOW,
        kind="test-seed",
        parameters={},
        due_at=NOW,
    )
    claimed_seed = jobs.claim_next(
        owner="seed", lease_duration=LEASE, now=NOW, allowed_kinds={"test-seed"}
    )
    assert claimed_seed is not None and claimed_seed.job.job_id == seed.job_id
    seed_artifact = store_evidence(
        b"daily seed",
        "text/plain",
        objects,
        SqlArtifactRepository(engine),
    )
    unchanged = "0000000001-26-000001"
    changed = "0000000002-26-000002"
    removed = "0000000003-26-000003"
    other_quarter = "0000000004-26-000004"
    for offset, (accession, company, quarter) in enumerate(
        (
            (unchanged, "UNCHANGED COMPANY", "2026Q3"),
            (changed, "OLD COMPANY", "2026Q3"),
            (removed, "REMOVED COMPANY", "2026Q3"),
            (other_quarter, "OTHER QUARTER", "2026Q4"),
        ),
        start=1,
    ):
        observations.record(
            claimed_seed.lease,
            item=SourceItemIdentity(
                "SEC",
                "filing",
                accession,
                issuer_source_identifier=accession[:10],
            ),
            item_outcome_key=f"seed:{accession}",
            state="present",
            metadata=_filing_metadata(accession, company, quarter=quarter),
            source_url="https://www.sec.gov/Archives/daily-seed.idx",
            artifact_sha256=seed_artifact.content_sha256,
            detected_at=NOW - timedelta(minutes=1) + timedelta(seconds=offset),
        )
    jobs.complete(claimed_seed.lease, now=NOW)

    new = "0000000005-26-000005"
    payload = _index(
        _row("10-K", "UNCHANGED COMPANY", "1", "2026-09-28", unchanged),
        _row("10-K", "CHANGED COMPANY", "2", "2026-09-28", changed),
        _row("10-K", "NEW COMPANY", "5", "2026-09-28", new),
    )
    result = EdgarTransportResult(
        payload,
        _quarterly_url(),
        "text/plain",
        '"quarter-v1"',
        "Tue, 29 Sep 2026 12:00:00 GMT",
    )
    first = jobs.enqueue_scheduled(
        schedule_key="sec:quarter-first",
        scheduled_for=NOW,
        kind="sec-index-reconciliation",
        parameters={"year": 2026, "quarter": 3},
        due_at=NOW,
        priority=20,
    )
    claimed = jobs.claim_next(
        owner="reconciler",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"sec-index-reconciliation"},
    )
    assert claimed is not None and claimed.job.job_id == first.job_id
    handler = _quarterly_handler(
        engine,
        objects,
        jobs,
        FixtureEdgar({_quarterly_url(): result}),
    )
    checkpoint = handler(_context(claimed, jobs))
    jobs.complete(claimed.lease, final_checkpoint=checkpoint, now=NOW)

    digest = hashlib.sha256(payload).hexdigest()
    with engine.connect() as connection:
        artifact = connection.execute(
            select(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == digest
            )
        ).mappings().one()
        children = connection.execute(
            select(durable_job.c.parameters)
            .where(durable_job.c.kind == "sec-resource-fetch")
            .order_by(durable_job.c.job_id)
        ).scalars().all()
        first_outcomes = connection.execute(
            select(durable_job_item.c.item_key, durable_job_item.c.outcome)
            .where(durable_job_item.c.job_id == first.job_id)
            .order_by(durable_job_item.c.item_key)
        ).all()
    assert objects.read_verified(
        artifact["object_key"], digest, artifact["byte_size"]
    ) == payload
    assert [item["accession"] for item in children] == [changed, new]
    assert len(first_outcomes) == 5
    assert [
        outcome["action"]
        for _key, outcome in first_outcomes
        if outcome is not None and "action" in outcome
    ] == ["unchanged"]

    latest = {
        row.canonical_metadata["accession"]: row
        for row in observations.latest_sec_financial_filings("2026Q3")
    }
    assert latest[unchanged].canonical_metadata["company_name"] == (
        "UNCHANGED COMPANY"
    )
    assert latest[changed].canonical_metadata["company_name"] == "CHANGED COMPANY"
    assert latest[new].state == "present"
    assert latest[removed].state == "removed"
    assert latest[removed].canonical_metadata["reason"] == (
        "absent_from_rebuilt_quarter"
    )
    assert observations.latest_sec_financial_filings("2026Q4")[
        0
    ].canonical_metadata["accession"] == other_quarter

    with engine.connect() as connection:
        before_replay = {
            "observations": connection.scalar(
                select(func.count()).select_from(source_observation)
            ),
            "children": connection.scalar(
                select(func.count())
                .select_from(durable_job)
                .where(durable_job.c.kind == "sec-resource-fetch")
            ),
        }
    replay = jobs.enqueue_scheduled(
        schedule_key="sec:quarter-replay",
        scheduled_for=NOW + timedelta(seconds=1),
        kind="sec-index-reconciliation",
        parameters={"year": 2026, "quarter": 3},
        due_at=NOW,
        priority=20,
    )
    replay_claim = jobs.claim_next(
        owner="restarted-reconciler",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"sec-index-reconciliation"},
    )
    assert replay_claim is not None and replay_claim.job.job_id == replay.job_id
    replay_handler = _quarterly_handler(
        engine,
        objects,
        _repository(engine),
        FixtureEdgar({_quarterly_url(): result}),
    )
    replay_jobs = _repository(engine)
    replay_checkpoint = replay_handler(_context(replay_claim, replay_jobs))
    replay_jobs.complete(
        replay_claim.lease,
        final_checkpoint=replay_checkpoint,
        now=NOW,
    )
    with engine.connect() as connection:
        after_replay = {
            "observations": connection.scalar(
                select(func.count()).select_from(source_observation)
            ),
            "children": connection.scalar(
                select(func.count())
                .select_from(durable_job)
                .where(durable_job.c.kind == "sec-resource-fetch")
            ),
        }
        replay_actions = connection.execute(
            select(durable_job_item.c.outcome)
            .where(durable_job_item.c.job_id == replay.job_id)
        ).scalars().all()
    assert after_replay == before_replay
    assert sorted(
        outcome["action"]
        for outcome in replay_actions
        if outcome is not None and "action" in outcome
    ) == ["already_removed", "unchanged", "unchanged", "unchanged"]


RESOURCE_ACCESSION = "0000001234-26-000001"
RESOURCE_CIK = "0000001234"
RESOURCE_FORM = "10-K"
RESOURCE_FILENAME = "edgar/data/1234/0000001234-26-000001.txt"


def _resource_urls():
    return {
        "complete_submission": (
            "https://www.sec.gov/Archives/"
            "edgar/data/1234/0000001234-26-000001.txt"
        ),
        "accession_inventory": (
            "https://www.sec.gov/Archives/edgar/data/1234/"
            "000000123426000001/index.json"
        ),
        "submissions": (
            "https://data.sec.gov/submissions/CIK0000001234.json"
        ),
        "company_facts": (
            "https://data.sec.gov/api/xbrl/companyfacts/"
            "CIK0000001234.json"
        ),
    }


def _resource_payloads(*, revision=1):
    return {
        "complete_submission": b"<SEC-DOCUMENT>fixture filing</SEC-DOCUMENT>",
        "accession_inventory": json.dumps(
            {
                "directory": {
                    "name": (
                        "/Archives/edgar/data/1234/000000123426000001"
                    ),
                    "item": [{"name": "annual.htm"}],
                }
            }
        ).encode(),
        "submissions": json.dumps(
            {
                "cik": "1234",
                "filings": {
                    "recent": {
                        "accessionNumber": [RESOURCE_ACCESSION],
                        "form": [RESOURCE_FORM],
                    }
                },
            }
        ).encode(),
        "company_facts": json.dumps(
            {
                "cik": 1234,
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "accn": RESOURCE_ACCESSION,
                                        "form": RESOURCE_FORM,
                                        "val": revision,
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ).encode(),
    }


def _resource_responses(*, revision=1):
    payloads = _resource_payloads(revision=revision)
    return {
        url: EdgarTransportResult(
            payloads[role],
            url,
            "text/plain" if role == "complete_submission" else "application/json",
            f'"{role}-{revision if role == "company_facts" else 1}"',
            "Tue, 29 Sep 2026 12:00:00 GMT",
        )
        for role, url in _resource_urls().items()
    }


def _seed_resource_parent(engine, objects, jobs, key):
    jobs.enqueue_scheduled(
        schedule_key=f"seed:{key}",
        scheduled_for=NOW,
        kind="test-seed",
        parameters={},
        due_at=NOW,
    )
    claimed = jobs.claim_next(
        owner=f"seed:{key}",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"test-seed"},
    )
    assert claimed is not None
    artifact = store_evidence(
        f"seed:{key}".encode(),
        "text/plain",
        objects,
        SqlArtifactRepository(engine),
    )
    stored = SourceObservationRepository(engine, clock=lambda: NOW).record(
        claimed.lease,
        item=SourceItemIdentity(
            "SEC",
            "filing",
            RESOURCE_ACCESSION,
            issuer_source_identifier=RESOURCE_CIK,
        ),
        item_outcome_key="filing",
        state="present",
        metadata={
            "accession": RESOURCE_ACCESSION,
            "cik": RESOURCE_CIK,
            "form": RESOURCE_FORM,
            "archive_filename": RESOURCE_FILENAME,
            "filing_date": "2026-09-28",
            "company_name": "FIXTURE CORP",
            "quarter": "2026Q3",
        },
        source_url="https://www.sec.gov/Archives/fixture.idx",
        artifact_sha256=artifact.content_sha256,
        detected_at=NOW,
    )
    jobs.complete(claimed.lease, now=NOW)
    return stored


def _enqueue_resource_job(jobs, parent, key, *, max_attempts=3):
    stored = jobs.enqueue_scheduled(
        schedule_key=f"resource:{key}",
        scheduled_for=NOW,
        kind="sec-resource-fetch",
        parameters={
            "accession": RESOURCE_ACCESSION,
            "cik": RESOURCE_CIK,
            "form": RESOURCE_FORM,
            "filename": RESOURCE_FILENAME,
            "observation_id": parent.observation.source_observation_id,
        },
        due_at=NOW,
        max_attempts=max_attempts,
    )
    claimed = jobs.claim_next(
        owner=f"resource:{key}",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"sec-resource-fetch"},
    )
    assert claimed is not None and claimed.job.job_id == stored.job_id
    return claimed


def test_resource_fetch_exact_bytes_revisions_and_replay(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "flow")

    first_claim = _enqueue_resource_job(jobs, parent, "first")
    first_payloads = _resource_payloads()
    first_edgar = FixtureEdgar(_resource_responses())
    first_checkpoint = _resource_handler(
        engine, objects, first_edgar
    )(_context(first_claim, jobs))
    jobs.complete(
        first_claim.lease,
        final_checkpoint=first_checkpoint,
        now=NOW,
    )

    with engine.connect() as connection:
        resources = connection.execute(
            select(source_item, source_observation)
            .join(
                source_observation,
                source_observation.c.source_item_id
                == source_item.c.source_item_id,
            )
            .where(source_item.c.source_item_id != parent.item.source_item_id)
            .order_by(source_observation.c.source_observation_id)
        ).mappings().all()
        artifacts = {
            row["content_sha256"]: row
            for row in connection.execute(select(evidence_artifact)).mappings()
        }
    assert first_edgar.urls == list(_resource_urls().values())
    assert [row["canonical_metadata"]["role"] for row in resources] == list(
        _resource_urls()
    )
    assert [row["parent_source_item_id"] for row in resources] == [
        parent.item.source_item_id,
        parent.item.source_item_id,
        None,
        None,
    ]
    for role, payload in first_payloads.items():
        digest = hashlib.sha256(payload).hexdigest()
        artifact = artifacts[digest]
        assert objects.read_verified(
            artifact["object_key"], digest, artifact["byte_size"]
        ) == payload
        row = next(
            item
            for item in resources
            if item["canonical_metadata"]["role"] == role
        )
        assert row["artifact_sha256"] == digest
        if role in {"complete_submission", "accession_inventory"}:
            assert row["canonical_metadata"]["parent_observation_id"] == (
                parent.observation.source_observation_id
            )
        else:
            assert row["canonical_metadata"] == {
                "role": role,
                "cik": RESOURCE_CIK,
            }

    before_revision = _counts(engine)
    changed_claim = _enqueue_resource_job(jobs, parent, "changed")
    changed_checkpoint = _resource_handler(
        engine,
        objects,
        FixtureEdgar(_resource_responses(revision=2)),
    )(_context(changed_claim, jobs))
    jobs.complete(
        changed_claim.lease,
        final_checkpoint=changed_checkpoint,
        now=NOW,
    )
    after_revision = _counts(engine)
    assert after_revision == {
        **before_revision,
        "artifacts": before_revision["artifacts"] + 1,
        "observations": before_revision["observations"] + 1,
        "children": before_revision["children"] + 1,
    }
    company_item_id = next(
        row["source_item_id"]
        for row in resources
        if row["canonical_metadata"]["role"] == "company_facts"
    )
    history = SourceObservationRepository(engine).history(company_item_id)
    assert len(history) == 2
    assert [row.artifact_sha256 for row in history] == [
        hashlib.sha256(first_payloads["company_facts"]).hexdigest(),
        hashlib.sha256(_resource_payloads(revision=2)["company_facts"]).hexdigest(),
    ]

    before_replay = _counts(engine)
    replay_claim = _enqueue_resource_job(jobs, parent, "replay")
    replay_checkpoint = _resource_handler(
        engine,
        objects,
        FixtureEdgar(_resource_responses(revision=2)),
    )(_context(replay_claim, jobs))
    jobs.complete(
        replay_claim.lease,
        final_checkpoint=replay_checkpoint,
        now=NOW,
    )
    assert _counts(engine) == {
        **before_replay,
        "children": before_replay["children"] + 1,
    }
    assert replay_checkpoint == changed_checkpoint


def test_resource_fetch_restart_finishes_without_duplicate_observations(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "restart")
    claimed = _enqueue_resource_job(jobs, parent, "restart")

    class InterruptOnThird(FixtureEdgar):
        def fetch(self, url):
            if len(self.urls) == 2:
                raise RuntimeError("forced interruption")
            return super().fetch(url)

    interrupted = InterruptOnThird(_resource_responses())
    with pytest.raises(RuntimeError, match="forced interruption"):
        _resource_handler(engine, objects, interrupted)(_context(claimed, jobs))
    jobs.fail(claimed.lease, error_summary="forced interruption", now=NOW)
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(source_observation)
            .where(
                source_observation.c.source_item_id
                != parent.item.source_item_id
            )
        ) == 2

    database_url = engine.url
    engine.dispose()
    restarted = create_engine(database_url)
    restarted_objects = ImmutableObjectStore(
        create_s3_client(StorageSettings.from_env()),
        StorageSettings.from_env().s3_bucket,
    )
    try:
        restarted_jobs = _repository(restarted)
        replacement = restarted_jobs.claim_next(
            owner="resource:replacement",
            lease_duration=LEASE,
            now=NOW,
            allowed_kinds={"sec-resource-fetch"},
        )
        assert replacement is not None and replacement.job.job_id == claimed.job.job_id
        checkpoint = _resource_handler(
            restarted,
            restarted_objects,
            FixtureEdgar(_resource_responses()),
        )(_context(replacement, restarted_jobs))
        restarted_jobs.complete(
            replacement.lease,
            final_checkpoint=checkpoint,
            now=NOW,
        )
        with restarted.connect() as connection:
            assert connection.scalar(
                select(func.count())
                .select_from(source_observation)
                .where(
                    source_observation.c.source_item_id
                    != parent.item.source_item_id
                )
            ) == 4
            assert connection.scalar(
                select(func.count())
                .select_from(durable_job_item)
                .where(durable_job_item.c.job_id == claimed.job.job_id)
            ) == 4
    finally:
        restarted.dispose()


def test_source_pending_survives_attempt_limit_and_runtime_restart(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "pending-restart")
    claimed = _enqueue_resource_job(jobs, parent, "pending-restart")
    responses = _resource_responses()
    first_url = _resource_urls()["complete_submission"]
    responses[first_url] = NoXbrlDataError("not found")
    handler = _resource_handler(engine, objects, FixtureEdgar(responses))

    for index in range(4):
        with pytest.raises(SecResourcePending):
            handler(_context(claimed, jobs))
        deferred = jobs.defer(
            claimed.lease,
            reason="SEC root resource is pending",
            now=NOW,
        )
        assert deferred.status == "retry_wait"
        assert deferred.attempts == 0
        if index < 3:
            claimed = jobs.claim_next(
                owner=f"pending-retry-{index}",
                lease_duration=LEASE,
                now=NOW,
                allowed_kinds={"sec-resource-fetch"},
            )
            assert claimed is not None

    repeated = jobs.enqueue_scheduled(
        schedule_key="resource:pending-restart",
        scheduled_for=NOW,
        kind="sec-resource-fetch",
        parameters={
            "accession": RESOURCE_ACCESSION,
            "cik": RESOURCE_CIK,
            "form": RESOURCE_FORM,
            "filename": RESOURCE_FILENAME,
            "observation_id": parent.observation.source_observation_id,
        },
        due_at=NOW,
    )
    assert repeated.job_id == claimed.job.job_id
    assert repeated.status == "retry_wait"

    database_url = engine.url
    engine.dispose()
    restarted = create_engine(database_url)
    restarted_objects = ImmutableObjectStore(
        create_s3_client(StorageSettings.from_env()),
        StorageSettings.from_env().s3_bucket,
    )
    runtime = DurableJobRuntime(
        _repository(restarted),
        lambda _now: (),
        {
            "sec-resource-fetch": _resource_handler(
                restarted,
                restarted_objects,
                FixtureEdgar(_resource_responses()),
            )
        },
        poll_interval=timedelta(milliseconds=10),
        lease_duration=timedelta(seconds=2),
        heartbeat_interval=timedelta(milliseconds=200),
        clock=lambda: NOW,
    )
    try:
        runtime.start()
        _wait_for(
            lambda: _status(restarted, "resource:pending-restart")
            == "succeeded"
        )
    finally:
        assert runtime.stop(3)
    with restarted.connect() as connection:
        completed = connection.execute(
            select(durable_job).where(durable_job.c.job_id == repeated.job_id)
        ).mappings().one()
    assert completed["attempts"] == 1
    assert completed["ownership_generation"] == 5
    restarted.dispose()


def test_resource_fetch_expired_owner_leaves_only_verified_orphan(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "expired-root")
    claimed = _enqueue_resource_job(jobs, parent, "expired-root")
    late = NOW + LEASE + timedelta(seconds=1)

    with pytest.raises(LeaseLost):
        _resource_handler(
            engine,
            objects,
            FixtureEdgar(_resource_responses()),
            clock=lambda: late,
        )(_context(claimed, jobs, clock=lambda: late))

    payload = _resource_payloads()["complete_submission"]
    digest = hashlib.sha256(payload).hexdigest()
    with engine.connect() as connection:
        artifact = connection.execute(
            select(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == digest
            )
        ).mappings().one()
        assert connection.scalar(select(func.count()).select_from(source_item)) == 1
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 1
        assert connection.scalar(
            select(func.count())
            .select_from(durable_job_item)
            .where(durable_job_item.c.job_id == claimed.job.job_id)
        ) == 0
        fetch_job = connection.execute(
            select(durable_job).where(durable_job.c.job_id == claimed.job.job_id)
        ).mappings().one()
    assert fetch_job["checkpoint"] is None
    assert objects.read_verified(
        artifact["object_key"], digest, artifact["byte_size"]
    ) == payload


def test_resource_fetch_present_then_pending_retains_revision_and_outcome(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "pending-root")
    claimed = _enqueue_resource_job(jobs, parent, "pending-root")
    responses = _resource_responses()
    company_url = _resource_urls()["company_facts"]
    pending_payload = json.dumps({"cik": 1234, "facts": {}}).encode()
    responses[company_url] = EdgarTransportResult(
        pending_payload,
        company_url,
        "application/json",
        '"company-facts-pending"',
        "Tue, 29 Sep 2026 12:00:00 GMT",
    )

    with pytest.raises(SecResourcePending, match="does not yet identify"):
        _resource_handler(
            engine, objects, FixtureEdgar(responses)
        )(_context(claimed, jobs))

    digest = hashlib.sha256(pending_payload).hexdigest()
    with engine.connect() as connection:
        company_item = connection.execute(
            select(source_item).where(
                source_item.c.source_key == f"companyfacts/{RESOURCE_CIK}"
            )
        ).mappings().one()
        history = connection.execute(
            select(source_observation)
            .where(
                source_observation.c.source_item_id
                == company_item["source_item_id"]
            )
            .order_by(source_observation.c.source_observation_id)
        ).mappings().all()
        outcome = connection.execute(
            select(durable_job_item).where(
                durable_job_item.c.job_id == claimed.job.job_id,
                durable_job_item.c.item_key == "root:company_facts",
            )
        ).mappings().one()
        artifact = connection.execute(
            select(evidence_artifact).where(
                evidence_artifact.c.content_sha256 == digest
            )
        ).mappings().one()
        checkpoint = connection.scalar(
            select(durable_job.c.checkpoint).where(
                durable_job.c.job_id == claimed.job.job_id
            )
        )
    assert [row["state"] for row in history] == ["present"]
    assert history[0]["artifact_sha256"] == digest
    assert outcome["attempts"] == 2
    assert outcome["status"] == "failed"
    assert outcome["outcome"]["state"] == "pending"
    assert outcome["outcome"]["source_observation_id"] == history[0][
        "source_observation_id"
    ]
    assert checkpoint is None
    assert objects.read_verified(
        artifact["object_key"], digest, artifact["byte_size"]
    ) == pending_payload


def test_resource_fetch_rejects_stale_real_parent_before_request(storage):
    engine, objects = storage
    jobs = _repository(engine)
    parent = _seed_resource_parent(engine, objects, jobs, "stale-parent")
    jobs.enqueue_scheduled(
        schedule_key="remove:stale-parent",
        scheduled_for=NOW,
        kind="test-remove",
        parameters={},
        due_at=NOW,
    )
    removal_claim = jobs.claim_next(
        owner="remove",
        lease_duration=LEASE,
        now=NOW,
        allowed_kinds={"test-remove"},
    )
    assert removal_claim is not None
    SourceObservationRepository(
        engine, clock=lambda: NOW + timedelta(seconds=1)
    ).record(
        removal_claim.lease,
        item=SourceItemIdentity(
            "SEC",
            "filing",
            RESOURCE_ACCESSION,
            issuer_source_identifier=RESOURCE_CIK,
        ),
        item_outcome_key="removed",
        state="removed",
        metadata={"reason": "fixture removal"},
        source_url="https://www.sec.gov/Archives/rebuilt.idx",
        detected_at=NOW + timedelta(seconds=1),
    )
    jobs.complete(removal_claim.lease, now=NOW)
    claimed = _enqueue_resource_job(jobs, parent, "stale-parent")
    edgar = FixtureEdgar({})

    with pytest.raises(ValueError, match="not latest"):
        _resource_handler(engine, objects, edgar)(_context(claimed, jobs))

    assert edgar.urls == []
