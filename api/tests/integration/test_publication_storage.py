import os
from time import monotonic, sleep
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import (
    active_data_release, company_snapshot, current_company_snapshot, data_release,
    durable_job_item, publication_candidate, publication_candidate_source,
    release_company, source_item, source_observation,
)
from screener.publication import (
    PublicationIdentityConflict, PublicationRepository, PublicationRequired, PublicationSourceMismatch,
    StalePublicationParent, StalePublicationSource, UnknownDataRelease,
)
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires real local PostgreSQL and S3")
NOW = datetime(2026, 10, 4, 9, tzinfo=timezone.utc)
LEASE = timedelta(minutes=5)


@pytest.fixture
def storage(monkeypatch, request):
    settings = StorageSettings.from_env()
    name = f"ms_publication_{uuid4().hex}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    url = make_url(settings.database_url).set(database=name)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(url)
    try:
        monkeypatch.setenv("SCREENER_DATABASE_URL", url.render_as_string(hide_password=False))
        config = Config("alembic.ini")
        starting_revision = getattr(request, "param", "base")
        command.upgrade(config, starting_revision)
        if starting_revision == "20260929_0006":
            with engine.begin() as connection:
                issuer_id = connection.scalar(text("INSERT INTO issuer (source_system, source_identifier) VALUES ('SEC', '0000001800') RETURNING issuer_id"))
                security_id = connection.scalar(text("INSERT INTO priced_security (issuer_id, security_identifier) VALUES (:issuer_id, 'sec:0000001800:primary') RETURNING security_id"), {"issuer_id": issuer_id})
                snapshot_id = connection.scalar(text("""INSERT INTO company_snapshot (security_id, engine_revision, payload_sha256, snapshot_sha256, canonical_payload, ticker, security_basis)
                    VALUES (:security_id, 187, :digest, :digest, CAST(:payload AS jsonb), 'ABT', 'PRIMARY_COMMON_SHARE') RETURNING snapshot_id"""), {"security_id": security_id, "digest": "a" * 64, "payload": '{"value":100}'})
                connection.execute(text("INSERT INTO current_company_snapshot (security_id, snapshot_id) VALUES (:security_id, :snapshot_id)"), {"security_id": security_id, "snapshot_id": snapshot_id})
        command.upgrade(config, "head")
        objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
        artifact = store_evidence(b'publication source bytes: 100', "application/json", objects, SqlArtifactRepository(engine))
        yield engine, artifact
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _claim(engine, key):
    jobs = DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key=key, scheduled_for=NOW, kind=key, parameters={}, due_at=NOW)
    claimed = jobs.claim_next(owner=key, lease_duration=LEASE, now=NOW, allowed_kinds={key})
    assert claimed is not None
    return claimed.lease


def _company(engine, artifact, *, value=100, ticker="ABT", identifier="1800", lease=None):
    repository = SharedCompanyRepository(engine, clock=lambda: NOW)
    values = dict(issuer_source="SEC", issuer_identifier=identifier,
                  security_identifier=f"sec:{int(identifier):010}:primary", ticker=ticker,
                  security_title="Common Stock", source_accession="filing-1",
                  security_basis="PRIMARY_COMMON_SHARE", engine_revision=187,
                  canonical_payload={"value": value}, artifacts=(SnapshotArtifact(artifact.content_sha256, "raw_filing"),))
    return repository.store_candidate(lease, **values) if lease else repository.store_and_select(**values)


def _source(engine, artifact, lease, *, identifier="1800", state="present", revision=1, key="filing-1", detected_at=NOW):
    repository = SourceObservationRepository(engine, clock=lambda: NOW)
    stored = repository.record(lease, item=SourceItemIdentity("SEC", "filing", key, f"{int(identifier):010}"),
                              item_outcome_key=f"source:{key}:{revision}", state=state,
                              metadata={"revision": revision}, source_url="https://www.sec.gov/filing-1",
                              artifact_sha256=artifact.content_sha256 if state == "present" else None,
                              detected_at=detected_at)
    return stored.observation.source_observation_id


def _setup(storage):
    engine, artifact = storage
    lease = _claim(engine, "publisher")
    original = _company(engine, artifact)
    repository = PublicationRepository(engine, clock=lambda: NOW)
    first = repository.bootstrap(lease, request_key="initial")
    source_id = _source(engine, artifact, lease)
    snapshot = _company(engine, artifact, value=90, lease=lease)
    candidate = repository.register_candidate(lease, request_key="candidate", snapshot_id=snapshot.snapshot_id,
                                             expected_parent_release_id=first.release_id,
                                             source_observation_ids=(source_id,), cause_type="source_change")
    return engine, artifact, lease, repository, first, original, snapshot, candidate, source_id


@pytest.mark.parametrize("storage", ["base", "20260929_0006"], indirect=True)
def test_empty_and_populated_previous_head_migration_and_restart(storage):
    engine, artifact = storage
    command.current(Config("alembic.ini"))
    command.heads(Config("alembic.ini"))
    command.check(Config("alembic.ini"))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    if SharedCompanyRepository(engine).current_by_ticker("ABT") is None:
        _company(engine, artifact)
    lease = _claim(engine, "initial")
    initial = PublicationRepository(engine, clock=lambda: NOW).bootstrap(lease, request_key="initial")
    engine.dispose()
    reopened = create_engine(engine.url)
    try:
        selected = SharedCompanyRepository(reopened).current_by_ticker("ABT")
        assert selected.release_id == initial.release_id
        assert selected.snapshot.canonical_payload == {"value": 100}
        assert PublicationRepository(reopened).release(initial.release_id).member_count == 1
    finally:
        reopened.dispose()


def test_commit_before_retry_pins_history_and_blocks_bootstrap_writer(storage):
    engine, artifact, lease, repository, first, original, snapshot, candidate, _ = _setup(storage)
    second = repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    retry = repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    assert retry == second
    assert repository.bootstrap(lease, request_key="initial") == first
    companies = SharedCompanyRepository(engine)
    assert companies.current(original.security_id).snapshot_id == snapshot.snapshot_id
    assert companies.current(original.security_id, release_id=first.release_id).snapshot_id == original.snapshot_id
    assert companies.current_by_ticker("ABT").release_id == second.release_id
    with pytest.raises(PublicationRequired):
        _company(engine, artifact, value=95)
    with pytest.raises(UnknownDataRelease):
        companies.current(original.security_id, release_id=999999)
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(data_release)) == 2
        assert connection.scalar(select(func.count()).select_from(durable_job_item).where(durable_job_item.c.item_key == "publication:revision-2")) == 1


def test_candidate_and_release_idempotency_keys_cannot_change_meaning(storage):
    engine, _, lease, repository, first, _, snapshot, candidate, source_id = _setup(storage)
    repeated = repository.register_candidate(lease, request_key="candidate", snapshot_id=snapshot.snapshot_id,
                                             expected_parent_release_id=first.release_id, source_observation_ids=(source_id,), cause_type="source_change")
    assert repeated == candidate
    with pytest.raises(PublicationIdentityConflict):
        repository.register_candidate(lease, request_key="candidate", snapshot_id=snapshot.snapshot_id,
                                      expected_parent_release_id=first.release_id, source_observation_ids=(source_id,), cause_type="extraction_fix")
    repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    with pytest.raises(PublicationIdentityConflict):
        repository.bootstrap(lease, request_key="revision-2")


def test_new_financial_source_cannot_certify_an_old_snapshot(storage):
    engine, artifact, lease, repository, first, _, snapshot, _, _ = _setup(storage)
    settings = StorageSettings.from_env()
    newer = store_evidence(b'new publication source bytes: 95', "application/json",
                           ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket), SqlArtifactRepository(engine))
    source_id = _source(engine, newer, lease, revision=2, detected_at=NOW + timedelta(seconds=1))
    with pytest.raises(PublicationSourceMismatch, match="no recorded observation lineage"):
        repository.register_candidate(lease, request_key="false-certification", snapshot_id=snapshot.snapshot_id,
                                      expected_parent_release_id=first.release_id, source_observation_ids=(source_id,), cause_type="source_change")
    # Even an independently staged candidate must pass the same lineage check
    # during activation, rather than trusting registration's previous result.
    with engine.begin() as connection:
        candidate_id = connection.scalar(publication_candidate.insert().values(
            request_key="unvalidated", security_id=snapshot.security_id, snapshot_id=snapshot.snapshot_id,
            expected_parent_release_id=first.release_id, cause_type="source_change", registering_job_id=lease.job_id,
        ).returning(publication_candidate.c.candidate_id))
        connection.execute(publication_candidate_source.insert().values(candidate_id=candidate_id, source_observation_id=source_id))
        connection.execute(update(publication_candidate).where(publication_candidate.c.candidate_id == candidate_id).values(registered_at=NOW))
    with pytest.raises(PublicationSourceMismatch):
        repository.publish(lease, request_key="false-release", candidate_ids=(candidate_id,), expected_parent_release_id=first.release_id)
    assert repository.active().release_id == first.release_id
    assert SharedCompanyRepository(engine).current_by_ticker("ABT").snapshot.canonical_payload == {"value": 100}


def _metadata_and_document(engine, artifact, lease, *, metadata_revision=1, resource_revision=1):
    settings = StorageSettings.from_env()
    inventory = store_evidence(f'filing metadata revision {metadata_revision}'.encode(), "application/json",
                               ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket), SqlArtifactRepository(engine))
    source_id = _source(engine, inventory, lease, key="inventory-filing", revision=metadata_revision,
                        detected_at=NOW + timedelta(seconds=metadata_revision))
    with engine.connect() as connection:
        parent = connection.execute(select(source_observation).where(source_observation.c.source_observation_id == source_id)).mappings().one()
    document = SourceObservationRepository(engine, clock=lambda: NOW).record(
        lease, item=SourceItemIdentity("SEC", "resource", "inventory-filing/document", "0000001800", parent["source_item_id"]),
        item_outcome_key=f"resource:{resource_revision}", state="present", metadata={
            "parent_source_item_id": parent["source_item_id"], "parent_observation_id": source_id,
            "parent_observation_sha256": parent["observation_sha256"], "role": "primary_document", "revision": resource_revision,
        }, source_url="https://www.sec.gov/inventory-filing/document", artifact_sha256=artifact.content_sha256,
        detected_at=NOW + timedelta(seconds=resource_revision),
    )
    return source_id, document.observation.source_observation_id


def test_filing_inventory_metadata_is_bound_through_retained_document_and_retry_identity(storage):
    engine, artifact, lease, repository, first, _, snapshot, _, _ = _setup(storage)
    filing_id, document_id = _metadata_and_document(engine, artifact, lease)
    registered = repository.register_candidate(lease, request_key="inventory-candidate", snapshot_id=snapshot.snapshot_id,
                                              expected_parent_release_id=first.release_id, source_observation_ids=(filing_id,), cause_type="source_change")
    assert registered.source_observation_ids == tuple(sorted((filing_id, document_id)))
    retry = repository.register_candidate(lease, request_key="inventory-candidate", snapshot_id=snapshot.snapshot_id,
                                         expected_parent_release_id=first.release_id, source_observation_ids=(document_id, filing_id), cause_type="source_change")
    assert retry == registered
    second = repository.publish(lease, request_key="inventory-release", candidate_ids=(registered.candidate_id,), expected_parent_release_id=first.release_id)
    assert second.release_id == repository.active().release_id


def test_new_filing_metadata_cannot_reuse_a_document_bound_to_its_old_revision(storage):
    engine, artifact, lease, repository, first, _, snapshot, _, _ = _setup(storage)
    filing_id, document_id = _metadata_and_document(engine, artifact, lease)
    candidate = repository.register_candidate(lease, request_key="inventory-candidate", snapshot_id=snapshot.snapshot_id,
                                             expected_parent_release_id=first.release_id, source_observation_ids=(filing_id,), cause_type="source_change")
    settings = StorageSettings.from_env()
    inventory = store_evidence(b'new metadata', "application/json", ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket), SqlArtifactRepository(engine))
    new_filing = _source(engine, inventory, lease, key="inventory-filing", revision=2, detected_at=NOW + timedelta(seconds=2))
    with pytest.raises(StalePublicationSource):
        repository.publish(lease, request_key="old-inventory-release", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    with pytest.raises((StalePublicationSource, PublicationSourceMismatch)):
        repository.register_candidate(lease, request_key="wrong-parent-candidate", snapshot_id=snapshot.snapshot_id,
                                      expected_parent_release_id=first.release_id, source_observation_ids=(new_filing, document_id), cause_type="source_change")
    assert repository.active().release_id == first.release_id


def test_resource_revision_after_registration_fences_auto_discovered_dependency(storage):
    engine, artifact, lease, repository, first, _, snapshot, _, _ = _setup(storage)
    filing_id, document_id = _metadata_and_document(engine, artifact, lease)
    candidate = repository.register_candidate(lease, request_key="inventory-candidate", snapshot_id=snapshot.snapshot_id,
                                             expected_parent_release_id=first.release_id, source_observation_ids=(filing_id,), cause_type="source_change")
    _, new_document_id = _metadata_and_document(engine, artifact, lease, resource_revision=2)
    assert new_document_id != document_id
    with pytest.raises(StalePublicationSource):
        repository.publish(lease, request_key="stale-input-release", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    with pytest.raises(PublicationIdentityConflict):
        repository.register_candidate(lease, request_key="inventory-candidate", snapshot_id=snapshot.snapshot_id,
                                      expected_parent_release_id=first.release_id, source_observation_ids=(filing_id,), cause_type="source_change")


def test_unrecorded_old_financial_input_cannot_hide_behind_current_cover_metadata(storage):
    engine, artifact, lease, repository, first, _, _, _, _ = _setup(storage)
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    old_facts = store_evidence(b'legacy facts amount 90', "application/json", objects, SqlArtifactRepository(engine))
    new_facts = store_evidence(b'new aggregate facts amount 95', "application/json", objects, SqlArtifactRepository(engine))
    SourceObservationRepository(engine, clock=lambda: NOW).record(
        lease, item=SourceItemIdentity("SEC", "aggregate", "companyfacts/0000001800", "0000001800"),
        item_outcome_key="new-facts", state="present", metadata={"role": "company_facts"},
        source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000001800.json", artifact_sha256=new_facts.content_sha256,
    )
    filing_id = _source(engine, artifact, lease, key="current-cover")
    snapshot = SharedCompanyRepository(engine, clock=lambda: NOW).store_candidate(
        lease, issuer_source="SEC", issuer_identifier="1800", security_identifier="sec:0000001800:primary",
        ticker="ABT", security_title="Common Stock", source_accession="current-cover", security_basis="PRIMARY_COMMON_SHARE",
        engine_revision=187, canonical_payload={"value": 90}, artifacts=(
            SnapshotArtifact(old_facts.content_sha256, "official_api_facts"),
            SnapshotArtifact(artifact.content_sha256, "raw_cover_identity"),
        ),
    )
    with pytest.raises(PublicationSourceMismatch, match="no recorded observation lineage"):
        repository.register_candidate(lease, request_key="missing-facts-lineage", snapshot_id=snapshot.snapshot_id,
                                      expected_parent_release_id=first.release_id, source_observation_ids=(filing_id,), cause_type="source_change")
    assert repository.active().release_id == first.release_id


@pytest.mark.parametrize("state", ["present", "removed", "unavailable", "pending"])
def test_newer_source_revision_blocks_old_candidate_without_inventing_withdrawal(storage, state):
    engine, artifact, lease, repository, first, _, _, candidate, _ = _setup(storage)
    source_id = _source(engine, artifact, lease, state=state, revision=2, detected_at=NOW + timedelta(seconds=1))
    with pytest.raises(StalePublicationSource):
        repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    assert repository.active().release_id == first.release_id
    with engine.connect() as connection:
        assert connection.scalar(select(source_observation.c.state).where(source_observation.c.source_observation_id == source_id)) == state
        assert connection.scalar(select(func.count()).select_from(data_release)) == 1


def test_expired_and_reclaimed_owner_cannot_publish(storage):
    engine, _, lease, repository, first, _, _, candidate, _ = _setup(storage)
    expired = PublicationRepository(engine, clock=lambda: NOW + LEASE)
    with pytest.raises(LeaseLost):
        expired.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    jobs = DurableJobRepository(engine)
    jobs.recover_expired(now=NOW + LEASE)
    reclaimed = jobs.claim_next(owner="replacement", lease_duration=LEASE, now=NOW + LEASE + timedelta(hours=2), allowed_kinds={"publisher"})
    assert reclaimed is not None
    with pytest.raises(LeaseLost):
        repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    assert repository.active().release_id == first.release_id


def test_lease_expiring_during_lock_wait_rolls_back_complete_release(storage):
    engine, _, lease, _, first, _, _, candidate, _ = _setup(storage)
    times = iter([NOW, NOW + LEASE])
    repository = PublicationRepository(engine, clock=lambda: next(times))
    with pytest.raises(LeaseLost):
        repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    assert PublicationRepository(engine).active().release_id == first.release_id
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(data_release)) == 1
        assert connection.scalar(select(func.count()).select_from(release_company)) == 1


def test_first_publishers_race_without_losing_initial_members(storage):
    engine, artifact = storage
    original = _company(engine, artifact)
    leases = [_claim(engine, f"initial-{index}") for index in range(2)]
    barrier = Barrier(2)
    def publish(index):
        barrier.wait(timeout=10)
        try:
            return PublicationRepository(engine, clock=lambda: NOW).bootstrap(leases[index], request_key=f"initial-{index}")
        except StalePublicationParent:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, range(2)))
    assert sum(result is not None for result in results) == 1
    assert PublicationRepository(engine).active().member_count == 1
    assert SharedCompanyRepository(engine).current(original.security_id).snapshot_id == original.snapshot_id


def test_competing_children_require_revalidated_rebase_and_keep_unchanged_members(storage):
    engine, artifact, lease, repository, first, _, _, candidate, _ = _setup(storage)
    other_lease = _claim(engine, "publisher-2")
    other_snapshot = _company(engine, artifact, value=300, ticker="OTHER", identifier="2", lease=other_lease)
    other_source = _source(engine, artifact, other_lease, identifier="2", key="filing-2")
    other_candidate = repository.register_candidate(other_lease, request_key="candidate-2", snapshot_id=other_snapshot.snapshot_id,
                                                   expected_parent_release_id=first.release_id, source_observation_ids=(other_source,), cause_type="source_change")
    barrier = Barrier(2)
    def publish(args):
        item, owner = args
        barrier.wait(timeout=10)
        try:
            return repository.publish(owner, request_key=f"publish-{item.candidate_id}", candidate_ids=(item.candidate_id,), expected_parent_release_id=first.release_id)
        except StalePublicationParent:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [(candidate, lease), (other_candidate, other_lease)]))
    assert sum(result is not None for result in results) == 1
    winner = repository.active()
    loser_index = next(index for index, result in enumerate(results) if result is None)
    loser, owner, source = [(candidate, lease, candidate.source_observation_ids[0]), (other_candidate, other_lease, other_source)][loser_index]
    rebased = repository.register_candidate(owner, request_key="rebased", snapshot_id=loser.snapshot_id,
                                            expected_parent_release_id=winner.release_id, source_observation_ids=(source,), cause_type="source_change")
    final = repository.publish(owner, request_key="rebased-release", candidate_ids=(rebased.candidate_id,), expected_parent_release_id=winner.release_id)
    assert final.member_count == 2
    assert SharedCompanyRepository(engine).current_by_ticker("ABT").snapshot.canonical_payload == {"value": 90}
    assert SharedCompanyRepository(engine).current_by_ticker("OTHER").snapshot.canonical_payload == {"value": 300}


def test_database_protects_sealed_membership_candidates_and_active_parent(storage):
    engine, _, lease, repository, first, original, snapshot, candidate, source_id = _setup(storage)
    forbidden = [
        update(release_company).where(release_company.c.release_id == first.release_id).values(snapshot_id=snapshot.snapshot_id),
        release_company.delete().where(release_company.c.release_id == first.release_id),
        release_company.insert().values(release_id=first.release_id, security_id=999, snapshot_id=snapshot.snapshot_id),
        update(data_release).where(data_release.c.release_id == first.release_id).values(request_key="rewritten"),
        update(publication_candidate).where(publication_candidate.c.candidate_id == candidate.candidate_id).values(cause_type="extraction_fix"),
        publication_candidate_source.delete().where(publication_candidate_source.c.candidate_id == candidate.candidate_id),
        publication_candidate_source.insert().values(candidate_id=candidate.candidate_id, source_observation_id=source_id),
        update(current_company_snapshot).where(current_company_snapshot.c.security_id == original.security_id).values(snapshot_id=snapshot.snapshot_id),
        active_data_release.delete(),
    ]
    for statement in forbidden:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(statement)
    second = repository.publish(lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(update(active_data_release).values(release_id=first.release_id))
    assert repository.active().release_id == second.release_id


def test_compound_fk_rejects_another_security_snapshot(storage):
    engine, artifact, lease, _, first, original, _, _, _ = _setup(storage)
    other = _company(engine, artifact, ticker="OTHER", identifier="2", lease=lease)
    with pytest.raises(IntegrityError, match="fk_release_company_snapshot"):
        with engine.begin() as connection:
            release_id = connection.scalar(data_release.insert().values(request_key="wrong-security", publishing_job_id=lease.job_id, parent_release_id=first.release_id).returning(data_release.c.release_id))
            connection.execute(release_company.insert().values(release_id=release_id, security_id=original.security_id, snapshot_id=other.snapshot_id))


def test_source_writer_cannot_commit_between_publication_validation_and_activation(storage, monkeypatch):
    engine, artifact, lease, repository, first, _, _, candidate, _ = _setup(storage)
    writer_lease = _claim(engine, "source-writer")
    entered, writer_started, allow_publish = Event(), Event(), Event()
    original_activate = repository._activate
    def held_activation(*args):
        entered.set()
        assert allow_publish.wait(timeout=10)
        return original_activate(*args)
    monkeypatch.setattr(repository, "_activate", held_activation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(repository.publish, lease, request_key="revision-2", candidate_ids=(candidate.candidate_id,), expected_parent_release_id=first.release_id)
        assert entered.wait(timeout=10)
        def write_source():
            writer_started.set()
            return _source(engine, artifact, writer_lease, state="unavailable", revision=2, detected_at=NOW + timedelta(seconds=1))
        writer = pool.submit(write_source)
        assert writer_started.wait(timeout=10)
        try:
            deadline = monotonic() + 5
            while monotonic() < deadline:
                with engine.connect() as connection:
                    blocked = connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%source_item%' AND query LIKE '%FOR UPDATE%')"))
                if blocked:
                    break
                sleep(0.01)
            assert blocked, "source write must wait on the publisher's source-item lock"
            assert not writer.done()
        finally:
            allow_publish.set()
        second = publication.result(timeout=10)
        source_id = writer.result(timeout=10)
    assert second.parent_release_id == first.release_id
    with engine.connect() as connection:
        assert connection.scalar(select(source_observation.c.state).where(source_observation.c.source_observation_id == source_id)) == "unavailable"
