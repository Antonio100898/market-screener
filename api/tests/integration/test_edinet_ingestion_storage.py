import json
import io
import os
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

import screener.edinet_ingestion as edinet_ingestion
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.edinet_ingestion import EdinetListDiscoveryHandler, EdinetResourceFetchHandler
from screener.job_runtime import JobContext
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (
    company_snapshot,
    current_company_snapshot,
    create_postgres_engine,
    durable_job,
    evidence_artifact,
    source_item,
    source_observation,
)
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceObservationRepository
from screener.source_observations import StaleSourceRevision
from screener.sources.edinet import EdinetTransportResult
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_STORAGE_INTEGRATION") != "1",
    reason="set RUN_STORAGE_INTEGRATION=1 with local PostgreSQL and S3 running",
)
NOW = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


@pytest.fixture
def storage():
    settings = StorageSettings.from_env()
    database_name = f"ms_edinet_ingestion_{uuid4().hex}"
    admin = create_postgres_engine(settings).execution_options(isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=database_name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    previous = os.environ.get("SCREENER_DATABASE_URL")
    engine = None
    try:
        os.environ["SCREENER_DATABASE_URL"] = url.render_as_string(hide_password=False)
        command.upgrade(Config("alembic.ini"), "head")
        engine = create_engine(url)
        yield engine, ImmutableObjectStore(
            create_s3_client(settings), settings.s3_bucket
        )
    finally:
        if engine is not None:
            engine.dispose()
        if previous is None:
            os.environ.pop("SCREENER_DATABASE_URL", None)
        else:
            os.environ["SCREENER_DATABASE_URL"] = previous
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin.dispose()


def _payload(name="Company A", timestamp="2026-09-29 21:00"):
    return json.dumps({
        "metadata": {
            "status": "200",
            "parameter": {"date": "2026-09-29", "type": "2"},
            "processDateTime": timestamp,
            "resultset": {"count": 1},
        },
        "results": [{
            "seqNumber": 1,
            "docID": "S100TEST", "edinetCode": "E01772", "secCode": "67520",
            "filerName": name, "docTypeCode": "120",
            "submitDateTime": "2026-09-29 10:30", "opeDateTime": None,
            "periodStart": "2025-04-01", "periodEnd": "2026-03-31",
            "parentDocID": None, "withdrawalStatus": "0",
            "docInfoEditStatus": "0", "disclosureStatus": "0",
            "legalStatus": "1", "xbrlFlag": "1",
        }],
    }).encode()


class Client:
    def __init__(self, payload):
        self.payload = payload

    def fetch_documents_on(self, day):
        return EdinetTransportResult(
            self.payload,
            f"https://api.edinet-fsa.go.jp/api/v2/documents.json?date={day}&type=2",
            "application/json", None, None,
        )


def _run(engine, objects, payload, key):
    jobs = DurableJobRepository(engine)
    job = jobs.enqueue_scheduled(
        schedule_key=key, scheduled_for=NOW, kind="edinet-list-discovery",
        parameters={"start_date": "2026-09-29", "end_date": "2026-09-29"},
        due_at=NOW,
    )
    claimed = jobs.claim_next(
        owner=key, lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-list-discovery"},
    )
    assert claimed is not None and claimed.job.job_id == job.job_id
    handler = EdinetListDiscoveryHandler(
        client=Client(payload), object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=lambda: NOW),
        jobs=jobs, clock=lambda: NOW,
    )
    checkpoint = handler(JobContext(
        claimed.job, threading.Event(), jobs, claimed.lease, lambda: NOW
    ))
    jobs.complete(claimed.lease, final_checkpoint=checkpoint, now=NOW)


def test_exact_list_revision_replay_and_future_fetch(storage):
    engine, objects = storage
    first = _payload()
    _run(engine, objects, first, "edinet:first")
    _run(engine, objects, first, "edinet:replay")
    second = _payload(timestamp="2026-09-29 21:05")
    _run(engine, objects, second, "edinet:changed")

    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(evidence_artifact)) == 2
        assert connection.scalar(select(func.count()).select_from(source_item)) == 2
        assert connection.scalar(select(func.count()).select_from(source_observation)) == 3
        assert connection.scalar(
            select(func.count()).select_from(durable_job).where(
                durable_job.c.kind == "edinet-resource-fetch"
            )
        ) == 1
        artifacts = connection.execute(select(evidence_artifact)).mappings().all()
    for artifact in artifacts:
        assert objects.read_verified(
            artifact["object_key"], artifact["content_sha256"], artifact["byte_size"]
        ) in {first, second}


def test_older_concurrent_list_cannot_replace_newer_state(storage):
    engine, objects = storage
    newest = _payload(timestamp="2026-09-29 21:05")
    _run(engine, objects, newest, "edinet:newest")

    with pytest.raises(StaleSourceRevision):
        _run(
            engine,
            objects,
            _payload(timestamp="2026-09-29 21:00"),
            "edinet:older",
        )

    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(source_observation)
        ) == 2


def _mapped_facts(record, _archive, *, ticker):
    return {
        "cik": record["edinetCode"],
        "entityName": "Panasonic Holdings Corporation",
        "facts": {"canonical": {}},
        "_adapter": {
            "kind": "edinet_xbrl",
            "statement_basis": "canonical",
            "reporting_currency": "JPY",
            "quote_currency": "JPY",
            "ticker": ticker,
            "security_code": record["secCode"],
            "security_basis": "PRIMARY_ORDINARY_SHARE",
            "reports": [{
                "document": record["docID"],
                "published": record["submitDateTime"][:10],
            }],
        },
    }


class ArchiveClient:
    def __init__(self, archive):
        self.archive = archive
        self.calls = []

    def fetch_xbrl_archive(self, document_id):
        self.calls.append(document_id)
        return EdinetTransportResult(
            self.archive,
            f"https://api.edinet-fsa.go.jp/api/v2/documents/{document_id}?type=1",
            "application/zip", '"zip-v1"', None,
        )


def _archive_bytes(payload=b"placeholder"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("XBRL/PublicDoc/annual.xbrl", payload)
    return buffer.getvalue()


def _real_xbrl_archive():
    return _archive_bytes(b"""<xbrli:xbrl
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:jpigp="http://disclosure.edinet-fsa.go.jp/taxonomy/jpigp/2025-11-01/jpigp_cor">
      <xbrli:context id="CurrentInstant"><xbrli:entity>
        <xbrli:identifier scheme="edinet">E01772</xbrli:identifier>
      </xbrli:entity><xbrli:period><xbrli:instant>2026-03-31</xbrli:instant>
      </xbrli:period></xbrli:context>
      <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
      <jpigp:AssetsIFRS contextRef="CurrentInstant" unitRef="JPY">1000</jpigp:AssetsIFRS>
    </xbrli:xbrl>""")


def test_archive_fetch_stages_replayable_candidate_without_selecting_it(
    storage, monkeypatch
):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    artifacts = SqlArtifactRepository(engine)
    companies = SharedCompanyRepository(engine, clock=lambda: NOW)
    old_record = {
        "docID": "S100OLD1", "edinetCode": "E01772", "secCode": "67520",
        "docTypeCode": "120", "xbrlFlag": "1",
        "submitDateTime": "2025-06-20 10:30",
    }
    previous = _mapped_facts(old_record, b"old", ticker="6752.T")
    previous_artifact = store_evidence(
        json.dumps(previous, sort_keys=True, separators=(",", ":")).encode(),
        "application/json", objects, artifacts,
    )
    old_raw = store_evidence(b"old archive", "application/zip", objects, artifacts)
    selected = companies.store_and_select(
        issuer_source="EDINET",
        issuer_identifier="E01772",
        security_identifier="edinet:E01772:primary-ordinary",
        ticker="6752.T",
        exchange_code="TSE",
        quote_currency="JPY",
        security_title="PRIMARY_ORDINARY_SHARE",
        source_accession="S100OLD1",
        security_basis="PRIMARY_ORDINARY_SHARE",
        engine_revision=1,
        canonical_payload={"ticker": "6752.T", "assets": 90},
        artifacts=(
            SnapshotArtifact(previous_artifact.content_sha256, "canonical_edinet_facts"),
            SnapshotArtifact(old_raw.content_sha256, "raw_filing"),
        ),
    )
    _run(engine, objects, _payload(), "edinet:candidate")
    claimed = jobs.claim_next(
        owner="archive-worker", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert claimed is not None
    exact_archive = _archive_bytes()
    archive_client = ArchiveClient(exact_archive)
    monkeypatch.setattr(edinet_ingestion, "build_edinet_companyfacts", _mapped_facts)
    handler = EdinetResourceFetchHandler(
        client=archive_client,
        object_store=objects,
        artifacts=artifacts,
        observations=SourceObservationRepository(engine, clock=lambda: NOW),
        companies=companies,
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=lambda bundle: (
            "ok",
            {
                "ticker": bundle.ticker,
                "reports": [
                    report["document"] for report in bundle.facts["_adapter"]["reports"]
                ],
            },
        ),
        clock=lambda: NOW,
    )
    checkpoint = handler(JobContext(
        claimed.job, threading.Event(), jobs, claimed.lease, lambda: NOW
    ))
    jobs.complete(claimed.lease, final_checkpoint=checkpoint, now=NOW)

    replay = jobs.enqueue_scheduled(
        schedule_key="edinet:candidate-replay", scheduled_for=NOW,
        kind="edinet-resource-fetch", parameters=claimed.job.parameters, due_at=NOW,
    )
    replay_claim = jobs.claim_next(
        owner="archive-replay", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert replay_claim is not None and replay_claim.job.job_id == replay.job_id
    replay_checkpoint = handler(JobContext(
        replay_claim.job, threading.Event(), jobs, replay_claim.lease, lambda: NOW
    ))
    jobs.complete(
        replay_claim.lease, final_checkpoint=replay_checkpoint, now=NOW
    )

    with engine.connect() as connection:
        snapshots = connection.execute(
            select(company_snapshot).order_by(company_snapshot.c.snapshot_id)
        ).mappings().all()
        current_id = connection.scalar(
            select(current_company_snapshot.c.snapshot_id)
        )
    assert len(snapshots) == 2
    assert current_id == selected.snapshot_id
    assert snapshots[-1]["canonical_payload"] == {
        "ticker": "6752.T", "reports": ["S100OLD1", "S100TEST"]
    }
    assert replay_checkpoint["snapshot_id"] == checkpoint["snapshot_id"]
    assert replay_checkpoint["snapshot_created"] is False
    assert archive_client.calls == ["S100TEST", "S100TEST"]
    archive = artifacts.get(
        next(
            artifact.content_sha256
            for artifact in companies.artifacts(checkpoint["snapshot_id"])
            if artifact.role == "raw_filing"
            and artifact.content_sha256 != old_raw.content_sha256
        )
    )
    assert objects.read_verified(
        archive.object_key, archive.content_sha256, archive.byte_size
    ) == exact_archive


def test_real_archive_maps_derives_and_stages_with_provenance(storage):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    _run(engine, objects, _payload(), "edinet:real-mapper")
    claimed = jobs.claim_next(
        owner="real-mapper", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert claimed is not None
    companies = SharedCompanyRepository(engine, clock=lambda: NOW)
    handler = EdinetResourceFetchHandler(
        client=ArchiveClient(_real_xbrl_archive()),
        object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=lambda: NOW),
        companies=companies,
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        clock=lambda: NOW,
    )

    checkpoint = handler(JobContext(
        claimed.job, threading.Event(), jobs, claimed.lease, lambda: NOW
    ))
    jobs.complete(claimed.lease, final_checkpoint=checkpoint, now=NOW)

    with engine.connect() as connection:
        snapshot = connection.execute(select(company_snapshot)).mappings().one()
        assert connection.scalar(
            select(func.count()).select_from(current_company_snapshot)
        ) == 0
    assert snapshot["ticker"] == "6752.T"
    assert snapshot["source_accession"] == "S100TEST"
    assert snapshot["canonical_payload"]["total_assets"] == 1000.0
    assert snapshot["canonical_payload"]["data_source"] == "edinet_xbrl"
    assert snapshot["canonical_payload"]["source_reports"][0]["document"] == "S100TEST"
    assert {artifact.role for artifact in companies.artifacts(snapshot["snapshot_id"])} == {
        "canonical_edinet_facts", "raw_filing"
    }


def test_partial_checkpoint_survives_process_restart_without_refetch(storage):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    _run(engine, objects, _payload(), "edinet:restart")
    claimed = jobs.claim_next(
        owner="before-restart", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert claimed is not None
    archive = _real_xbrl_archive()

    def interrupt_after_mapping(_bundle):
        raise RuntimeError("forced interruption")

    handler = EdinetResourceFetchHandler(
        client=ArchiveClient(archive), object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=lambda: NOW),
        companies=SharedCompanyRepository(engine, clock=lambda: NOW),
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=interrupt_after_mapping, clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="forced interruption"):
        handler(JobContext(
            claimed.job, threading.Event(), jobs, claimed.lease, lambda: NOW
        ))
    jobs.fail(claimed.lease, error_summary="forced interruption", now=NOW)
    database_url = engine.url
    engine.dispose()
    restarted = create_engine(database_url)
    later = NOW + timedelta(hours=1)
    try:
        restarted_jobs = DurableJobRepository(restarted)
        replacement = restarted_jobs.claim_next(
            owner="after-restart", lease_duration=LEASE, now=later,
            allowed_kinds={"edinet-resource-fetch"},
        )
        assert replacement is not None and replacement.job.job_id == claimed.job.job_id

        class NoNetwork:
            def fetch_xbrl_archive(self, _document_id):
                raise AssertionError("archive checkpoint must prevent refetch")

        replacement_handler = EdinetResourceFetchHandler(
            client=NoNetwork(), object_store=objects,
            artifacts=SqlArtifactRepository(restarted),
            observations=SourceObservationRepository(restarted, clock=lambda: later),
            companies=SharedCompanyRepository(restarted, clock=lambda: later),
            listings={"6752": {"name": "Panasonic Holdings Corporation"}},
            clock=lambda: later,
        )
        checkpoint = replacement_handler(JobContext(
            replacement.job, threading.Event(), restarted_jobs,
            replacement.lease, lambda: later,
        ))
        restarted_jobs.complete(
            replacement.lease, final_checkpoint=checkpoint, now=later
        )
        with restarted.connect() as connection:
            resources = connection.scalar(
                select(func.count()).select_from(source_item).where(
                    source_item.c.source_system == "EDINET",
                    source_item.c.item_kind == "resource",
                )
            )
            snapshots = connection.scalar(
                select(func.count()).select_from(company_snapshot)
            )
        assert resources == 1
        assert snapshots == 1
    finally:
        restarted.dispose()


def test_expired_owner_after_archive_upload_cannot_commit_observation(storage):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    _run(engine, objects, _payload(), "edinet:expired-upload")
    claimed = jobs.claim_next(
        owner="expired-upload", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert claimed is not None
    expired = [False]

    class ExpiringObjects:
        def put_verified(self, data, media_type):
            stored = objects.put_verified(data, media_type)
            expired[0] = True
            return stored

        def read_verified(self, *args):
            return objects.read_verified(*args)

    def clock():
        return NOW + LEASE + timedelta(seconds=1) if expired[0] else NOW

    handler = EdinetResourceFetchHandler(
        client=ArchiveClient(_real_xbrl_archive()),
        object_store=ExpiringObjects(),
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=clock),
        companies=SharedCompanyRepository(engine, clock=clock),
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        clock=clock,
    )
    with pytest.raises(LeaseLost):
        handler(JobContext(
            claimed.job, threading.Event(), jobs, claimed.lease, clock
        ))
    with engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(source_item).where(
                source_item.c.source_system == "EDINET",
                source_item.c.item_kind == "resource",
            )
        ) == 0
        assert connection.scalar(
            select(func.count()).select_from(company_snapshot)
        ) == 0


def test_expired_owner_after_partial_checkpoint_cannot_stage_candidate(storage):
    engine, objects = storage
    jobs = DurableJobRepository(engine)
    _run(engine, objects, _payload(), "edinet:expired-candidate")
    claimed = jobs.claim_next(
        owner="expired-candidate", lease_duration=LEASE, now=NOW,
        allowed_kinds={"edinet-resource-fetch"},
    )
    assert claimed is not None
    expired = [False]

    def clock():
        return NOW + LEASE + timedelta(seconds=1) if expired[0] else NOW

    def expire_before_candidate(bundle):
        expired[0] = True
        return "ok", {"ticker": bundle.ticker}

    handler = EdinetResourceFetchHandler(
        client=ArchiveClient(_real_xbrl_archive()), object_store=objects,
        artifacts=SqlArtifactRepository(engine),
        observations=SourceObservationRepository(engine, clock=clock),
        companies=SharedCompanyRepository(engine, clock=clock),
        listings={"6752": {"name": "Panasonic Holdings Corporation"}},
        derive=expire_before_candidate, clock=clock,
    )
    with pytest.raises(LeaseLost):
        handler(JobContext(
            claimed.job, threading.Event(), jobs, claimed.lease, clock
        ))
    with engine.connect() as connection:
        job = connection.execute(
            select(durable_job).where(durable_job.c.job_id == claimed.job.job_id)
        ).mappings().one()
        resources = connection.scalar(
            select(func.count()).select_from(source_item).where(
                source_item.c.source_system == "EDINET",
                source_item.c.item_kind == "resource",
            )
        )
        snapshots = connection.scalar(
            select(func.count()).select_from(company_snapshot)
        )
    assert "archive_observation_sha256" in job["checkpoint"]
    assert resources == 1
    assert snapshots == 0
