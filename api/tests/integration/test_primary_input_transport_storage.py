"""Real registered plan/pair mechanics over labelled synthetic financial bodies."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import subprocess
import sys
import threading
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text

from screener import sync, store
from screener.artifacts import store_evidence
from screener.captured_assembly import CapturedCanonicalPreparer, engine_identity
from screener.company_import import _canonical_json
from screener.durable_jobs import LeaseLost
from screener.models import CapturedSource
from screener.object_store import ImmutableObjectStore, ObjectVerificationError, create_s3_client
from screener.postgres import durable_job
from screener.primary_input_plan import PlanDependency, PrimaryInputPlan, encode_primary_plan
from screener.shared_companies import SnapshotArtifact
from screener.source_observations import SourceItemIdentity
from screener.sources.sec_json import decode_company_facts, ORIGIN_KEY
from screener.storage_config import StorageSettings
from test_native_primary_inputs import fixture, mapping
from test_sec_ingestion_storage import database_url, NOW, LEASE, _context
from test_sec_exact_annotation_storage import setup_flow, counts, saved_checkpoint


pytestmark = pytest.mark.skipif(os.getenv('RUN_STORAGE_INTEGRATION') != '1', reason='isolated PostgreSQL/S3 required')


@pytest.fixture
def storage(database_url):
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text('TRUNCATE issuer, source_item, durable_job, evidence_artifact RESTART IDENTITY CASCADE'))
    settings = StorageSettings.from_env()
    client = create_s3_client(settings)
    bucket = 'ms-private-plan-' + uuid4().hex
    client.create_bucket(Bucket=bucket)
    try:
        yield engine, ImmutableObjectStore(client, bucket)
    finally:
        assert bucket.startswith('ms-private-plan-') and len(bucket) == len('ms-private-plan-') + 32
        for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
            keys = [{'Key': row['Key']} for row in page.get('Contents', [])]
            if keys:
                client.delete_objects(Bucket=bucket, Delete={'Objects': keys})
        client.delete_bucket(Bucket=bucket)
        client.close()
        engine.dispose()


def planned_flow(storage, key='planned', *, missing_native=False):
    flow = setup_flow(storage, key, complete_root=False)
    cik = flow.parent.item.issuer_source_identifier
    seed = flow.stage._companies.store_candidate(flow.candidate.lease, issuer_source='SEC', issuer_identifier=cik,
        security_identifier=f'sec:{cik}:primary-listed-security', ticker='FIX', security_title=None,
        source_accession=None, security_basis='SEC_TICKER_MAPPING_ONLY', engine_revision=store.ENGINE_VERSION,
        canonical_payload={'ticker': 'FIX', 'label': 'synthetic security identity fixture'})
    flow.seed = seed
    _, _, roots = flow.stage._load_roots(flow.root_checkpoint['roots'], parent=flow.parent,
        accession=flow.parent.observation.canonical_metadata['accession'], cik=cik,
        deliveries=flow.root_checkpoint['root_deliveries'])
    cf_source = next(source for source in roots if source.role == 'company_facts')
    cf_record = flow.source.by_sha256(flow.root_checkpoint['roots']['company_facts'])
    facts = decode_company_facts(flow.raw, source=cf_source)
    target = facts['facts']['us-gaap']['EarningsPerShareDiluted']['units']['USD/shares'][0][ORIGIN_KEY]
    body, request = fixture('2.06')
    body = body.replace(b'0001066923', cik.encode())
    url = request.source.source_document.replace('/1066923/', '/1234/')
    artifact = store_evidence(body, 'text/html', flow.objects, flow.artifacts)
    source = replace(request.source, artifact_sha256=artifact.content_sha256, byte_size=len(body), cik=cik,
                     source_document=url)
    request = replace(request, source=source, context=replace(request.context, entity_identifier=cik))
    filing_body = b'Labelled synthetic historical filing witness: 10-K/A 2023-03-24'
    filing_artifact = store_evidence(filing_body, 'text/plain', flow.objects, flow.artifacts)
    filing = flow.source.record(flow.root_claim.lease, item=SourceItemIdentity('SEC', 'filing', source.accession, cik),
        item_outcome_key='plan:filing', state='present', artifact_sha256=filing_artifact.content_sha256,
        source_url='https://www.sec.gov/Archives/synthetic-historical.idx', detected_at=NOW,
        metadata=dict(cik=cik, accession=source.accession, form=source.form, filing_date=source.filed.isoformat()))
    from screener.sec_ingestion import _SecResource, _resource_metadata
    native_resource = _SecResource('primary_document', 'resource', source.accession + '/native-primary', url, True)
    native = flow.source.record(flow.root_claim.lease,
        item=SourceItemIdentity('SEC', 'resource', source.accession + '/native-primary', cik, filing.item.source_item_id),
        item_outcome_key='plan:native', state='present', artifact_sha256=artifact.content_sha256,
        source_url=url, detected_at=NOW, metadata=_resource_metadata(resource=native_resource,
            role=native_resource.role, accession=source.accession, cik=cik, form=source.form,
            filename=filing.observation.canonical_metadata.get('archive_filename'), parent=filing))
    native_capture = CapturedSource('primary_document', artifact.content_sha256, url,
        native.item.source_item_id, native.observation.source_observation_id, native.delivery.occurrence_id, 'EXACT_LEXICAL')
    request = replace(request, source=replace(source, captured_source=native_capture))
    declaration = mapping(body, request, target_origin=target, unary_factor_text='-1')
    doc = declaration.documents[0]
    doc = doc.model_copy(update={'reference': doc.reference.model_copy(update={'issuer_source_identifier': cik,
                                                                            'source_document': url})})
    declaration = replace(declaration, documents=(doc,))
    filing_capture = CapturedSource('filing', filing_artifact.content_sha256, filing.observation.source_url,
        filing.item.source_item_id, filing.observation.source_observation_id, filing.delivery.occurrence_id, 'EXACT_LEXICAL')
    dependencies = (
        PlanDependency(cf_source.artifact_sha256, len(flow.raw), ('company_facts',), cf_source,
            cf_record.observation.observation_sha256, cf_record.delivery if cf_record.delivery else flow.source.delivery(
                cf_source.occurrence_id, source_item_id=cf_source.source_item_id, source_observation_id=cf_source.source_observation_id)),
        PlanDependency(artifact.content_sha256, len(body), ('native_original', 'explanation'), native_capture,
                       native.observation.observation_sha256, None if missing_native else native.delivery),
        PlanDependency(filing_artifact.content_sha256, len(filing_body), ('filing_witness',), filing_capture,
                       filing.observation.observation_sha256, filing.delivery))
    flow.plan = PrimaryInputPlan(cik, seed.security_id, (declaration,), dependencies)
    flow.native, flow.body = native, body
    flow.plan_artifact = store_evidence(encode_primary_plan(flow.plan), 'application/json', flow.objects, flow.artifacts)
    flow.resource.enqueue_planned_candidate(_context(flow.root_claim, flow.jobs), flow.root_checkpoint,
        primary_input_plan_sha256=flow.plan_artifact.content_sha256)
    flow.jobs.complete(flow.root_claim.lease, final_checkpoint=flow.root_checkpoint, now=NOW)
    flow.planned = flow.jobs.claim_next(owner='planned-worker', lease_duration=LEASE, now=NOW,
                                       allowed_kinds={'sec-candidate-stage'})
    assert flow.planned and flow.planned.job.parameters['primary_input_revision'] == 1
    return flow


def recovered_context(flow, checkpoint):
    return replace(_context(flow.planned, flow.jobs), job=replace(flow.planned.job, checkpoint=checkpoint))


def test_real_plan_stage_exact_pair_and_captured_preparation_recovery(storage, monkeypatch):
    flow = planned_flow(storage)
    bundles, original = [], sync._derive_evidence_result
    def observe(bundle, *args, **kwargs):
        bundles.append(bundle)
        return original(bundle, *args, **kwargs)
    monkeypatch.setattr(sync, '_derive_evidence_result', observe)
    checkpoint = flow.stage(_context(flow.planned, flow.jobs))
    assert checkpoint['checkpoint_revision'] == 3 and len(bundles) == 1
    assert type(bundles[0].primary_inputs[0]).__name__ == 'AdmittedPrimaryInput'
    assert bundles[0].primary_inputs[0].mapping == flow.plan.declarations[0]
    assert counts(flow.engine) == [2, 1, 0]
    graph = flow.stage._history.load(checkpoint['exact_bundle_id'])
    assert len(graph.sources) == 6 and all(row.assessment != 'FULL' for row in graph.coverage)
    selected = flow.stage._companies.snapshot(checkpoint['snapshot_id'])
    assert SnapshotArtifact(flow.plan_artifact.content_sha256, 'primary_input_plan') in selected.artifacts
    recipe = dict(recipe_revision=2, engine_revision=store.ENGINE_VERSION, engine_identity_sha256=engine_identity(),
        members=[dict(security_id=checkpoint['security_id'], snapshot_id=checkpoint['snapshot_id'],
            market_recipe_sha256=None, primary_input_plan_sha256=flow.plan_artifact.content_sha256,
            company_facts_occurrence_id=flow.plan.dependencies[0].delivery.occurrence_id)])
    recipe_sha = store_evidence(_canonical_json(recipe), 'application/json', flow.objects, flow.artifacts).content_sha256
    preparer = CapturedCanonicalPreparer(flow.stage._companies, flow.artifacts, flow.objects, flow.source)
    prepared = preparer.prepare(recipe_sha)
    assert len(bundles) == 2
    monkeypatch.setattr(sync, '_derive_evidence_result', lambda *args, **kwargs: pytest.fail('recovery built'))
    monkeypatch.setattr(flow.source, 'latest_sec_filing', lambda *args: pytest.fail('recovery looked up current'))
    assert flow.stage(recovered_context(flow, checkpoint)) == checkpoint
    for field in ('revision', 'dependency_id'):
        changed = json.loads(json.dumps(checkpoint))
        manifest = changed['primary_input_transport']
        if field == 'revision':
            manifest['primary_input_revision'] = True
        else:
            manifest['dependencies'][0]['source_item_id'] = float(manifest['dependencies'][0]['source_item_id'])
        with pytest.raises(ValueError, match='dependency or admission differs'):
            flow.stage(recovered_context(flow, changed))
        assert saved_checkpoint(flow.engine, flow.planned.job.job_id) == checkpoint
    assert preparer.verify(prepared, recipe_sha)['preparation_revision'] == 2
    assert counts(flow.engine) == [2, 1, 0]


def test_real_declared_unregistered_native_retains_keyed_unavailability(storage, monkeypatch):
    flow = planned_flow(storage, 'unavailable', missing_native=True)
    bundles, original = [], sync._derive_evidence_result
    def observe(bundle, *args, **kwargs):
        bundles.append(bundle)
        return original(bundle, *args, **kwargs)
    monkeypatch.setattr(sync, '_derive_evidence_result', observe)
    checkpoint = flow.stage(_context(flow.planned, flow.jobs))
    assert checkpoint['primary_input_transport']['admission'] == [dict(key='diluted', status='UNAVAILABLE',
        reason='NATIVE_REGISTERED_SOURCE_DELIVERY_UNAVAILABLE')]
    assert bundles[0].primary_inputs[0].target_origin == flow.plan.declarations[0].target_origin
    assert len(flow.stage._history.load(checkpoint['exact_bundle_id']).sources) == 5


@pytest.mark.parametrize('failure', ['history', 'checkpoint', 'late_lease'])
def test_real_planned_pair_late_failure_rolls_back_and_exact_retry(storage, monkeypatch, failure):
    flow = planned_flow(storage, failure)
    before = counts(flow.engine)
    context = _context(flow.planned, flow.jobs)
    target, name = (flow.stage._history, 'store_on_connection') if failure == 'history' else (flow.jobs, 'save_checkpoint_on_connection')
    write = getattr(target, name)
    def fail(*args, **kwargs):
        result = write(*args, **kwargs)
        if failure == 'late_lease':
            flow.jobs._lock_live_lease(args[0], flow.planned.lease, NOW + LEASE + timedelta(seconds=1))
        raise ValueError('after actual pair write')
    monkeypatch.setattr(target, name, fail)
    with pytest.raises((ValueError, LeaseLost)):
        flow.stage(context)
    assert counts(flow.engine) == before and saved_checkpoint(flow.engine, flow.planned.job.job_id) is None
    monkeypatch.setattr(target, name, write)
    checkpoint = flow.stage(context)
    assert counts(flow.engine) == [2, 1, 0]
    assert saved_checkpoint(flow.engine, flow.planned.job.job_id) == checkpoint


@pytest.mark.parametrize('field', ['checked_at', 'job_id', 'ownership_generation', 'item_key'])
def test_real_false_dependency_delivery_refuses_before_pair(storage, field):
    flow = planned_flow(storage, field)
    dependency = flow.plan.dependencies[1]
    changed = {'checked_at': NOW + timedelta(seconds=1), 'job_id': flow.candidate.job.job_id,
               'ownership_generation': dependency.delivery.ownership_generation + 1, 'item_key': 'wrong:leaf'}[field]
    plan = replace(flow.plan, dependencies=(flow.plan.dependencies[0],
        replace(dependency, delivery=replace(dependency.delivery, **{field: changed})), flow.plan.dependencies[2]))
    artifact = store_evidence(encode_primary_plan(plan), 'application/json', flow.objects, flow.artifacts)
    context = _context(flow.planned, flow.jobs)
    context = replace(context, job=replace(context.job, parameters={**context.job.parameters,
        'primary_input_plan_sha256': artifact.content_sha256}))
    with pytest.raises(ValueError, match='delivery differs'):
        flow.stage(context)
    assert counts(flow.engine) == [1, 0, 0]


@pytest.mark.parametrize('missing', [True, False])
def test_real_missing_or_corrupt_plan_object_refuses_before_pair(storage, missing):
    flow = planned_flow(storage, 'object')
    client, bucket, key = flow.objects.client, flow.objects.bucket, flow.plan_artifact.object_key
    if missing:
        client.delete_object(Bucket=bucket, Key=key)
    else:
        client.put_object(Bucket=bucket, Key=key, Body=b'changed-plan')
    with pytest.raises(Exception):
        flow.stage(_context(flow.planned, flow.jobs))
    assert counts(flow.engine) == [1, 0, 0]


def test_real_changed_restored_source_recovery_keeps_original_check(storage, monkeypatch):
    flow = planned_flow(storage, 'recurrence')
    checkpoint = flow.stage(_context(flow.planned, flow.jobs))
    item, original = flow.native.item, flow.native.observation
    for revision in ('B', 'A'):
        metadata = dict(original.canonical_metadata)
        if revision == 'B':
            metadata['synthetic_revision'] = revision
        stored = flow.source.record(flow.planned.lease,
            item=SourceItemIdentity(item.source_system, item.item_kind, item.source_key,
                item.issuer_source_identifier, item.parent_source_item_id), item_outcome_key='probe:' + revision,
            state='present', metadata=metadata, artifact_sha256=original.artifact_sha256,
            source_url=original.source_url, detected_at=NOW + timedelta(seconds=1 if revision == 'B' else 2))
    assert stored.observation.source_observation_id == original.source_observation_id
    assert stored.delivery.occurrence_id != flow.native.delivery.occurrence_id
    monkeypatch.setattr(sync, '_derive_evidence_result', lambda *args, **kwargs: pytest.fail('recovery built'))
    assert flow.stage(recovered_context(flow, checkpoint)) == checkpoint
    native_dependency = checkpoint['primary_input_transport']['dependencies'][1]
    assert native_dependency['occurrence_id'] == flow.native.delivery.occurrence_id


def test_real_fresh_process_pair3_recovery_without_network_build_or_current(storage):
    flow = planned_flow(storage, 'restart')
    checkpoint = flow.stage(_context(flow.planned, flow.jobs))
    script = '''
import json,sys,threading
from datetime import datetime
from sqlalchemy import create_engine,select
from screener import sync
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository,LeaseToken,_job_record
from screener.figure_history import FigureHistoryRepository
from screener.job_runtime import JobContext
from screener.object_store import ImmutableObjectStore,create_s3_client
from screener.postgres import durable_job
from screener.sec_ingestion import SecCandidateStageHandler
from screener.shared_companies import SharedCompanyRepository
from screener.source_observations import SourceObservationRepository
from screener.storage_config import StorageSettings
data=json.load(sys.stdin); engine=create_engine(data['url']); clock=lambda:datetime.fromisoformat(data['now'])
def forbidden(*args,**kwargs):raise AssertionError('offline recovery invoked forbidden owner')
sync.build_snapshot=forbidden;sync._derive_evidence_result=forbidden
SourceObservationRepository.latest_sec_filing=forbidden;SharedCompanyRepository.current=forbidden
class OfflineEdgar:
    fetch=forbidden
jobs=DurableJobRepository(engine)
with engine.connect() as connection:
    job=_job_record(connection.execute(select(durable_job).where(durable_job.c.job_id==data['job'])).mappings().one())
context=JobContext(job,threading.Event(),jobs,LeaseToken(job.job_id,job.owner,job.ownership_generation),clock)
objects=ImmutableObjectStore(create_s3_client(StorageSettings.from_env()),data['bucket'])
stage=SecCandidateStageHandler(edgar=OfflineEdgar(),object_store=objects,artifacts=SqlArtifactRepository(engine),
    observations=SourceObservationRepository(engine,clock=clock),companies=SharedCompanyRepository(engine,clock=clock),
    history=FigureHistoryRepository(engine,clock=clock),clock=clock)
print(json.dumps(stage(context)));engine.dispose()
'''
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(dict(
        url=flow.engine.url.render_as_string(hide_password=False), bucket=flow.objects.bucket,
        now=NOW.isoformat(), job=flow.planned.job.job_id)), text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == checkpoint


@pytest.mark.parametrize('expired', [True, False])
def test_real_planned_checkpoint_lock_wait_rolls_back_or_commits_whole_pair(storage, expired):
    flow = planned_flow(storage, 'live-wait')
    live = lambda: datetime.now(timezone.utc)
    now = live()
    flow.jobs.enqueue_scheduled(schedule_key='live-planned', scheduled_for=now, kind='live-planned',
        parameters=flow.planned.job.parameters, due_at=now)
    claim = flow.jobs.claim_next(owner='live-planned', lease_duration=timedelta(seconds=2 if expired else 20),
                                allowed_kinds={'live-planned'})
    guard = flow.jobs.enqueue_scheduled(schedule_key='planned-guard', scheduled_for=now,
        kind='guard', parameters={}, due_at=now).job_id
    blocker = flow.engine.connect(); transaction = blocker.begin()
    blocker.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
    entered, pids = threading.Event(), []
    for owner in (flow.stage, flow.stage._observations, flow.stage._companies, flow.stage._history):
        owner._clock = live
    def wait_after_write(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith('UPDATE durable_job SET checkpoint'):
            pids.append(connection.scalar(text('SELECT pg_backend_pid()')))
            entered.set()
            connection.execute(select(durable_job).where(durable_job.c.job_id == guard).with_for_update()).all()
    event.listen(flow.engine, 'after_cursor_execute', wait_after_write)
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(flow.stage, _context(claim, flow.jobs, clock=live))
    try:
        assert entered.wait(5)
        waiting = False
        deadline = monotonic() + 3
        while monotonic() < deadline:
            with flow.engine.connect() as observer:
                waiting = observer.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {'pid': pids[0]})
            if waiting:
                break
            sleep(.01)
        assert waiting
        with flow.engine.connect() as observer:
            expires = observer.scalar(select(durable_job.c.lease_expires_at).where(durable_job.c.job_id == claim.job.job_id))
        sleep(max(0, (expires - live()).total_seconds()) + .05 if expired else .05)
        transaction.rollback()
        if expired:
            with pytest.raises(LeaseLost):
                future.result(timeout=5)
            assert counts(flow.engine) == [1, 0, 0] and saved_checkpoint(flow.engine, claim.job.job_id) is None
        else:
            checkpoint = future.result(timeout=5)
            assert counts(flow.engine) == [2, 1, 0] and saved_checkpoint(flow.engine, claim.job.job_id) == checkpoint
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        event.remove(flow.engine, 'after_cursor_execute', wait_after_write)
        pool.shutdown(wait=True)
