"""Prepared real UUID SQL/S3 controls over synthetic source/financial fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
import subprocess
import sys
from time import monotonic, sleep

import pytest
from sqlalchemy import func, select, text, update

from screener import figure_history as history, store
from screener.captured_assembly import engine_identity
from screener.durable_jobs import LeaseLost
from screener.postgres import exact_figure_bundle, priced_security, source_item
from screener.shared_companies import SnapshotArtifact
from screener.source_observations import SourceItemIdentity
from test_primary_input_transport_storage import database_url, storage, planned_flow
from test_sec_ingestion_storage import NOW


pytestmark = pytest.mark.skipif(os.getenv("RUN_STORAGE_INTEGRATION") != "1", reason="requires root-exclusive UUID SQL/S3")


def source_case(storage):
    flow = planned_flow(storage, "native-source-only")
    repository = history.FigureHistoryRepository(storage[0], clock=lambda: NOW)
    proof = repository.prepare_registered_native(flow.plan_artifact.content_sha256, cik=flow.plan.cik,
        company_facts_occurrence_id=flow.plan.dependencies[0].captured_source.occurrence_id,
        security_identifier=f"sec:{flow.plan.cik}:primary-listed-security",
        expected_engine_identity_sha256=engine_identity(), object_store=flow.objects)
    snapshot = flow.stage._companies.store_candidate(flow.planned.lease, issuer_source="SEC",
        issuer_identifier=flow.plan.cik, security_identifier=proof.security_identifier, ticker="FIX",
        security_title=None, source_accession=None, security_basis="SEC_TICKER_MAPPING_ONLY", engine_revision=store.ENGINE_VERSION,
        canonical_payload={"label": "synthetic unselected native source support"}, artifacts=proof.artifacts)
    bundle = history.ExactFigureBundle(security_id=snapshot.security_id, snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256, engine_revision=store.ENGINE_VERSION,
        pipeline_run_key="native-source-only", contract_revision=3, observations=proof.observations,
        selections=(), coverage=(), sources=proof.sources, document_evidence=proof.document_evidence)
    return flow, repository, proof, bundle


def rows(engine):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(exact_figure_bundle))


def test_scalar_authentic_native_store_load_replay_keeps_publication_closed(storage):
    flow, repository, proof, bundle = source_case(storage)
    identifier = repository.store(flow.planned.lease, request_key="native-source", bundle=bundle,
        prepared_native=proof, prepared_documents=proof.prepared_documents)
    assert repository.load(identifier) == bundle
    assert repository.store(flow.planned.lease, request_key="native-source", bundle=bundle,
        prepared_native=proof, prepared_documents=proof.prepared_documents) == identifier
    assert bundle.observations[0].value_decimal_text == "2.06" and not bundle.selections
    with storage[0].connect() as connection:
        with pytest.raises(ValueError, match="not publication qualified"):
            repository.publication_identity_on_connection(connection, identifier, security_id=bundle.security_id,
                snapshot_id=bundle.snapshot_id, expected_run_key=bundle.pipeline_run_key)


@pytest.mark.parametrize("fault", ["wrong-role", "wrong-security", "source-delivery"])
def test_sql_reread_role_security_and_original_delivery_refuses(storage, fault):
    flow, repository, proof, bundle = source_case(storage)
    engine = storage[0]
    if fault == "wrong-role":
        artifacts = tuple(SnapshotArtifact(a.content_sha256, "wrong-plan-role" if a.role == "primary_input_plan" else a.role)
                          for a in proof.artifacts)
        snapshot = flow.stage._companies.store_candidate(flow.planned.lease, issuer_source="SEC",
            issuer_identifier=flow.plan.cik, security_identifier=proof.security_identifier, ticker="FIX",
            security_title=None, source_accession=None, security_basis="SEC_TICKER_MAPPING_ONLY",
            engine_revision=store.ENGINE_VERSION, canonical_payload={"label": "synthetic wrong role"}, artifacts=artifacts)
        bundle = bundle.model_copy(update=dict(snapshot_id=snapshot.snapshot_id, snapshot_sha256=snapshot.snapshot_sha256))
    elif fault == "wrong-security":
        with engine.begin() as connection:
            connection.execute(update(priced_security).where(priced_security.c.security_id == bundle.security_id).values(security_identifier="wrong-native-security"))
    else:
        source = proof.sources[0]
        bundle = bundle.model_copy(update=dict(sources=(source.model_copy(update=dict(occurrence_id=999999)), *proof.sources[1:])))
    before = rows(engine)
    with pytest.raises(ValueError):
        repository.store(flow.planned.lease, request_key="wrong-native", bundle=bundle,
            prepared_native=proof, prepared_documents=proof.prepared_documents)
    assert rows(engine) == before


@pytest.mark.parametrize("fault", ["expiry", "security-after-wait"])
def test_complete_prepared_source_union_rechecks_after_real_lock_wait(storage, fault):
    flow, repository, proof, bundle = source_case(storage)
    if fault == "expiry":
        flow.jobs.complete(flow.candidate.lease, final_checkpoint={"fixture_state": "seed-consumed"}, now=NOW)
    engine = storage[0]
    filing = next(dep.captured_source for dep in proof.plan.dependencies if dep.captured_source.role == "filing")
    clock = [NOW]
    repository._clock = lambda: clock[0]
    before = rows(engine)
    with engine.connect() as blocker:
        transaction = blocker.begin()
        blocker.execute(select(source_item.c.source_item_id).where(source_item.c.source_item_id == filing.source_item_id).with_for_update()).all()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(repository.store, flow.planned.lease, request_key="expired-native", bundle=bundle,
                    prepared_native=proof, prepared_documents=proof.prepared_documents)
                try:
                    deadline = monotonic() + 3
                    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                        while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                            assert not future.done() and monotonic() < deadline
                            sleep(.01)
                    if fault == "expiry":
                        clock[0] = NOW + timedelta(minutes=10)
                    else:
                        with engine.begin() as connection:
                            connection.execute(update(priced_security).where(priced_security.c.security_id == bundle.security_id)
                                .values(security_identifier="changed-while-native-waits"))
                finally:
                    transaction.rollback()
                with pytest.raises(LeaseLost if fault == "expiry" else ValueError): future.result(timeout=5)
        finally:
            if transaction.is_active: transaction.rollback()
    assert rows(engine) == before
    if fault == "expiry":
        recovered = flow.jobs.recover_expired(now=clock[0])
        retry = next(job for job in recovered if job.job_id == flow.planned.lease.job_id)
        assert retry.status == "retry_wait" and retry.next_retry_at is not None
        replacement = flow.jobs.claim_next(owner="native-replacement", lease_duration=timedelta(minutes=5),
            now=retry.next_retry_at, allowed_kinds={"sec-candidate-stage"})
        assert replacement and replacement.lease.job_id == flow.planned.lease.job_id
        assert replacement.lease.generation > flow.planned.lease.generation
        with pytest.raises(LeaseLost):
            repository.store(flow.planned.lease, request_key="stale-native", bundle=bundle,
                prepared_native=proof, prepared_documents=proof.prepared_documents)
        assert rows(engine) == before


@pytest.mark.parametrize("final_state", ["pending", "restored"])
def test_original_captured_occurrences_survive_source_current_change_without_io_under_sql(storage, monkeypatch, final_state):
    flow, repository, proof, bundle = source_case(storage)
    native = flow.native
    flow.source.record(flow.planned.lease, item=SourceItemIdentity("SEC", "resource", native.item.source_key,
        flow.plan.cik, native.item.parent_source_item_id), item_outcome_key="native-later-pending",
        state="pending", metadata={"role": "primary_document", "reason": "synthetic later state"},
        source_url=native.observation.source_url, detected_at=NOW + timedelta(seconds=1))
    if final_state == "restored":
        restored = flow.source.record(flow.planned.lease, item=SourceItemIdentity("SEC", "resource", native.item.source_key,
            flow.plan.cik, native.item.parent_source_item_id), item_outcome_key="native-restored",
            state="present", metadata=native.observation.canonical_metadata, artifact_sha256=native.observation.artifact_sha256,
            source_url=native.observation.source_url, detected_at=NOW + timedelta(seconds=2))
        assert restored.observation.source_observation_id == native.observation.source_observation_id
        assert restored.delivery.occurrence_id != native.delivery.occurrence_id
    monkeypatch.setattr(flow.objects, "read_verified", lambda *args: pytest.fail("writer read object bytes"))
    from screener import primary_input_transport
    monkeypatch.setattr(primary_input_transport.PrimaryInputTransport, "resolve", lambda *args, **kwargs: pytest.fail("writer resolved source bodies"))
    monkeypatch.setattr(primary_input_transport.evidence.sec_json, "decode_company_facts", lambda *args, **kwargs: pytest.fail("writer decoded CF body"))
    monkeypatch.setattr(primary_input_transport.evidence.inline_xbrl, "read_native_numeric", lambda *args, **kwargs: pytest.fail("writer parsed native body"))
    identifier = repository.store(flow.planned.lease, request_key="historical-native", bundle=bundle,
        prepared_native=proof, prepared_documents=proof.prepared_documents)
    assert repository.load(identifier).sources == proof.sources


def test_fresh_process_retained_native_load_and_new_repository_proof_recovery(storage):
    flow, repository, proof, bundle = source_case(storage)
    identifier = repository.store(flow.planned.lease, request_key="native-restart", bundle=bundle,
        prepared_native=proof, prepared_documents=proof.prepared_documents)
    process = subprocess.run([sys.executable, "-c", """
import sys
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.figure_history import FigureHistoryRepository
engine = create_engine(StorageSettings.from_env().database_url)
try:
    print(FigureHistoryRepository(engine).load(int(sys.argv[1])).model_dump_json())
finally:
    engine.dispose()
""", str(identifier)], capture_output=True, text=True, check=True, timeout=15)
    assert history.ExactFigureBundle.model_validate_json(process.stdout) == bundle
    recovered = history.FigureHistoryRepository(storage[0], clock=lambda: NOW)
    with pytest.raises(ValueError, match="native persistence is closed"):
        recovered.store(flow.planned.lease, request_key="native-restart", bundle=bundle,
            prepared_documents=proof.prepared_documents)
    fresh = recovered.prepare_registered_native(proof.plan_sha256, cik=proof.cik,
        company_facts_occurrence_id=proof.company_facts_occurrence_id, security_identifier=proof.security_identifier,
        expected_engine_identity_sha256=engine_identity(), object_store=flow.objects)
    assert recovered.store(flow.planned.lease, request_key="native-restart", bundle=bundle,
        prepared_native=fresh, prepared_documents=fresh.prepared_documents) == identifier
