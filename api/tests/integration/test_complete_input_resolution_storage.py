"""Real association owners over synthetic bytes/rows; no financial builds."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from threading import Event
from time import sleep
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from screener import complete_input_plan as owner, store, sync
from screener.artifacts import SqlArtifactRepository, store_evidence
from screener.captured_assembly import CapturedCanonicalPreparer, engine_identity
from screener.durable_jobs import DurableJobRepository, LeaseLost, RetryPolicy
from screener.figure_history import FigureHistoryRepository
from screener.job_runtime import JobContext
from screener.models import CapturedSource
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import durable_job
from screener.primary_input_plan import PrimaryInputPlan, PlanDependency, encode_primary_plan
from screener.primary_input_transport import PrimaryInputTransport, PLAN_ROLE, DEPENDENCY_ROLE
from screener.publication import PublicationRepository
from screener.shared_companies import SharedCompanyRepository, SnapshotArtifact
from screener.source_observations import SourceItemIdentity, SourceObservationRepository
from screener.storage_config import StorageSettings
from test_publication_storage import storage, pytestmark


@pytest.fixture
def resolved(storage):
    engine=storage[0];now=datetime.now(timezone.utc)
    jobs=DurableJobRepository(engine,retry_policy=RetryPolicy(base_delay=timedelta(seconds=1),max_delay=timedelta(seconds=1),jitter_seconds=lambda _:0));key=uuid4().hex
    jobs.enqueue_scheduled(schedule_key=key,scheduled_for=now,kind='fixture',parameters={},due_at=now)
    claim=jobs.claim_next(owner=key,lease_duration=timedelta(minutes=5),now=now,allowed_kinds={'fixture'})
    source=SourceObservationRepository(engine,clock=lambda:now);companies=SharedCompanyRepository(engine,clock=lambda:now)
    client=create_s3_client(StorageSettings.from_env());objects=ImmutableObjectStore(client,StorageSettings.from_env().s3_bucket)
    artifacts=SqlArtifactRepository(engine);publications=PublicationRepository(engine,clock=lambda:now,object_store=objects)
    canonical=CapturedCanonicalPreparer(companies,artifacts,objects,source)
    service=owner.CompleteInputResolver(canonical,artifacts,objects,source,companies,FigureHistoryRepository(engine),publications)
    def put(value):
        return store_evidence(value if isinstance(value,bytes) else owner.encode(value),'application/json',objects,artifacts).content_sha256
    def capture(role,body,occurrence=1,*,item_kind='aggregate',source_key=None,metadata=None):
        flags=dict(capture_revision=2,capture_fidelity='RAW_HTTP_BYTES') if item_kind=='aggregate' else {}
        sha=put(body);stored=source.record(claim.lease,item=SourceItemIdentity('SEC',item_kind,source_key or f'{role}/0000001800','0000001800'),
            item_outcome_key=f'root:{role}:{occurrence}',state='present',metadata=dict(role=role,cik='0000001800',**flags,**(metadata or {})),
            source_url=f'https://data.sec.gov/{role}/0000001800',artifact_sha256=sha,detected_at=now+timedelta(microseconds=occurrence))
        captured=CapturedSource(role,sha,stored.observation.source_url,stored.item.source_item_id,stored.observation.source_observation_id,stored.delivery.occurrence_id,'EXACT_LEXICAL')
        ref=dict(observation_sha256=stored.observation.observation_sha256,item_kind=stored.item.item_kind,source_key=stored.item.source_key,
            parent_source_item_id=stored.item.parent_source_item_id,role=role,artifact_sha256=sha,artifact_byte_size=len(body),
            captured_source=asdict(captured),source_url=stored.observation.source_url,state='present',delivery={**asdict(stored.delivery),'checked_at':stored.delivery.checked_at.isoformat()})
        return stored,captured,ref
    cf_body=owner.encode(dict(cik=1800,facts={}))
    cf,cf_capture,cf_ref=capture('company_facts',cf_body)
    sub_body=owner.encode(dict(cik=1800,name='Synthetic association fixture',sic='2834',filings=dict(files=[],recent=dict(
        form=['10-K','10-Q'],filingDate=['2026-03-01','2026-05-01'],reportDate=['2025-12-31','2026-03-31'],
        accessionNumber=['0000001800-26-000001','0000001800-26-000002'],primaryDocument=['annual.htm','quarter.htm'],items=['','']))))
    sub,sub_capture,sub_ref=capture('submissions',sub_body)
    filing_body=b'EXPLICITLY SYNTHETIC filing witness: SEC 0000001800 10-K 0000001800-26-000001 filed 2026-03-01; no financial/source authority'
    filing,filing_capture,filing_ref=capture('filing',filing_body,item_kind='filing',source_key='0000001800-26-000001',
        metadata=dict(accession='0000001800-26-000001',form='10-K',filing_date='2026-03-01'))
    def prepare_refs(revision=1,parent=False):
        if revision==2:
            return prepare_staged_plan_refs(f,parent=parent)
        raw_payload=dict(fixture='Synthetic financial row; no financial/source acceptance')
        dependencies=[SnapshotArtifact(cf_capture.artifact_sha256,'sec_root_company_facts'),SnapshotArtifact(sub_capture.artifact_sha256,'sec_root_submissions'),SnapshotArtifact(filing_capture.artifact_sha256,'raw_filing')]
        row=companies.store_candidate(claim.lease,issuer_source='SEC',issuer_identifier='0000001800',security_identifier='sec:0000001800:primary',ticker='FIX',
            security_title=None,source_accession=None,security_basis='PRIMARY_COMMON_SHARE',engine_revision=store.ENGINE_VERSION,canonical_payload=raw_payload,artifacts=dependencies)
        parent_id=None
        if parent:
            # Fixture-only bootstrap setup through the existing owner, no resolver activation.
            with engine.begin() as connection:
                from screener.postgres import current_company_snapshot
                connection.execute(current_company_snapshot.insert().values(security_id=row.security_id,snapshot_id=row.snapshot_id))
            parent_id=publications.bootstrap(claim.lease,request_key='fixture-parent').release_id
        candidate=publications.register_candidate(claim.lease,request_key=f'fixture-candidate-{revision}-{parent}',snapshot_id=row.snapshot_id,
            expected_parent_release_id=parent_id,source_observation_ids=(cf.observation.source_observation_id,sub.observation.source_observation_id,filing.observation.source_observation_id),cause_type='source_change')
        company=companies.snapshot(row.snapshot_id)
        member_request=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,market_recipe_sha256=None,jpx_listing_sha256=None,
            source_observation_sha256=[cf.observation.observation_sha256,sub.observation.observation_sha256,filing.observation.observation_sha256])
        recipe=put(dict(recipe_revision=revision,engine_revision=store.ENGINE_VERSION,engine_identity_sha256=engine_identity(),members=[member_request]))
        member=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,snapshot_sha256=row.snapshot_sha256,canonical_payload_sha256=row.payload_sha256,
            canonical_artifact_sha256=put(raw_payload),assumed_engine_artifact_sha256=None,detail_projection_status='NOT_BUILT',market_artifact_sha256=put({'status':'UNAVAILABLE'}),
            market_recipe_sha256=None,jpx_listing_sha256=None,input_artifacts=[asdict(a) for a in company.artifacts],
            source_observations=canonical._sources(NS(source_observation_sha256=member_request['source_observation_sha256']),company,{a.content_sha256:canonical._read(a.content_sha256) for a in company.artifacts}),upstream_gaps=['SYNTHETIC_NO_FINANCIAL_PROOF'])
        prep=put(dict(preparation_revision=revision,recipe_sha256=recipe,engine_revision=store.ENGINE_VERSION,engine_identity_sha256=engine_identity(),
            official_financial_readiness='NOT_ASSESSED',display_projection_status='NOT_BUILT',members=[member]))
        binding=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,exact_bundle_id=None)
        accounting=put(dict(accounting_revision=1,expected_parent_release_id=parent_id,engine_identity_sha256=engine_identity(),engine_identity_status='DECLARED_NOT_VERIFIED',
            universe=[dict(key='input',artifact_sha256s=sorted({a.content_sha256 for a in company.artifacts}))],
            outcomes=[dict(input_key='input',status='SELECTED',selection=binding)],parent_members=[binding] if parent else [],
            selected_members=[dict(**binding,disposition='REPLACED' if parent else 'NEW',candidate_id=candidate.candidate_id)],retirements=[]))
        record=owner.load(owner.InputRecord,owner.encode(dict(ordinal=0,input_key='input',issuer=dict(source='SEC',identifier='0000001800'),sources=[cf_ref,sub_ref,filing_ref],
            canonical=dict(recipe_sha256=recipe,preparation_sha256=prep,member_ordinal=0),exact_bundle=None,
            submissions=dict(source=sub_ref,accession='0000001800-26-000001',form='10-K',filing_date='2026-03-01'),
            parent_state=dict(status='UNAVAILABLE',artifact_sha256=None,reason='Accepted market state unavailable',parent_release_id=parent_id,security_id=row.security_id,snapshot_id=row.snapshot_id) if parent else None,
            gaps=['SYNTHETIC_NOT_FINANCIAL_PROOF'])))
        complete=service.record_plan(accounting_sha256=accounting,records=[record],preparations=[dict(recipe_sha256=recipe,preparation_sha256=prep)],operation_at=now.isoformat(),
            export_mode='FULL',global_listing=dict(status='UNAVAILABLE',artifact_sha256=None,reason='Listing not registered'),
            global_indexes=[dict(name=n,status='UNAVAILABLE',artifact_sha256=None,reason='Index not registered') for n in ('S&P 500','DJIA','Nasdaq 100')],gaps=['SYNTHETIC'])
        return complete,row,accounting
    f=NS(**locals())
    try:yield f
    finally:client.close()



def prepare_staged_plan_refs(f, *, parent=False):
    """Reuse the actual planned-stage fixture; its finances remain explicitly synthetic."""
    assert not parent, 'planned resolver fixture currently covers initial association'
    from test_primary_input_transport_storage import planned_flow
    from test_sec_ingestion_storage import _context, NOW as STAGE_NOW
    from screener.postgres import source_observation, source_item_current
    flow=planned_flow((f.engine,f.objects),'resolver-pair3')
    checkpoint=flow.stage(_context(flow.planned,flow.jobs))
    flow.jobs.complete(flow.planned.lease,final_checkpoint=checkpoint,now=STAGE_NOW)
    company=f.companies.snapshot(checkpoint['snapshot_id']);row=company.snapshot
    candidate=f.publications.register_candidate(f.claim.lease,request_key='fixture-pair3-candidate',snapshot_id=row.snapshot_id,
        expected_parent_release_id=None,source_observation_ids=(s.source_observation_id for s in flow.stage._history.load(checkpoint['exact_bundle_id']).sources),
        cause_type='source_change',exact_bundle_id=checkpoint['exact_bundle_id'],expected_exact_run_key=checkpoint['pipeline_run_key'])
    refs=[];hashes={a.content_sha256 for a in company.artifacts}
    with f.engine.connect() as connection:
        bindings=connection.execute(select(source_observation.c.observation_sha256,source_item_current.c.occurrence_id).join(
            source_item_current,source_item_current.c.source_item_id==source_observation.c.source_item_id).where(
            source_observation.c.source_observation_id.in_(candidate.source_observation_ids)).order_by(source_observation.c.source_observation_id)).all()
    for digest,occurrence in bindings:
        stored=f.source.by_sha256(digest);item,obs=stored.item,stored.observation
        delivery=f.source.delivery(occurrence,source_item_id=item.source_item_id,source_observation_id=obs.source_observation_id)
        role=obs.canonical_metadata.get('role') or ('filing' if item.item_kind=='filing' else None)
        captured=CapturedSource(role,obs.artifact_sha256,obs.source_url,item.source_item_id,obs.source_observation_id,occurrence,'EXACT_LEXICAL')
        refs.append(dict(observation_sha256=digest,item_kind=item.item_kind,source_key=item.source_key,parent_source_item_id=item.parent_source_item_id,
            role=role,artifact_sha256=obs.artifact_sha256,artifact_byte_size=f.artifacts.get(obs.artifact_sha256).byte_size,captured_source=asdict(captured),
            source_url=obs.source_url,state=obs.state,delivery={**asdict(delivery),'checked_at':delivery.checked_at.isoformat()}))
    source_hashes=[ref['observation_sha256'] for ref in refs if ref['artifact_sha256'] in hashes]
    requested=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,market_recipe_sha256=None,jpx_listing_sha256=None,
        source_observation_sha256=source_hashes,primary_input_plan_sha256=flow.plan_artifact.content_sha256,
        company_facts_occurrence_id=flow.plan.dependencies[0].delivery.occurrence_id)
    recipe=f.put(dict(recipe_revision=2,engine_revision=store.ENGINE_VERSION,engine_identity_sha256=engine_identity(),members=[requested]))
    transported=PrimaryInputTransport(f.artifacts,f.objects,f.source).resolve(flow.plan_artifact.content_sha256,cik=company.issuer_identifier,
        security_id=row.security_id,company_facts_source=None,company_facts_payload=flow.raw,company_facts_occurrence_id=requested['company_facts_occurrence_id'])
    member=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,snapshot_sha256=row.snapshot_sha256,canonical_payload_sha256=row.payload_sha256,
        canonical_artifact_sha256=f.put(row.canonical_payload),assumed_engine_artifact_sha256=None,detail_projection_status='NOT_BUILT',
        market_artifact_sha256=f.put({'status':'UNAVAILABLE'}),market_recipe_sha256=None,jpx_listing_sha256=None,input_artifacts=[asdict(a) for a in company.artifacts],
        source_observations=f.canonical._sources(NS(source_observation_sha256=source_hashes),company,{a.content_sha256:f.canonical._read(a.content_sha256) for a in company.artifacts}),
        primary_input_transport=transported.manifest,company_facts_occurrence_id=requested['company_facts_occurrence_id'],upstream_gaps=['SYNTHETIC_NO_FINANCIAL_PROOF'])
    prep=f.put(dict(preparation_revision=2,recipe_sha256=recipe,engine_revision=store.ENGINE_VERSION,engine_identity_sha256=engine_identity(),
        official_financial_readiness='NOT_ASSESSED',display_projection_status='NOT_BUILT',members=[member]))
    binding=dict(security_id=row.security_id,snapshot_id=row.snapshot_id,exact_bundle_id=checkpoint['exact_bundle_id'])
    accounting=f.put(dict(accounting_revision=1,expected_parent_release_id=None,engine_identity_sha256=engine_identity(),engine_identity_status='DECLARED_NOT_VERIFIED',
        universe=[dict(key='input',artifact_sha256s=sorted(hashes|{ref['artifact_sha256'] for ref in refs}))],outcomes=[dict(input_key='input',status='SELECTED',selection=binding)],
        parent_members=[],selected_members=[dict(**binding,disposition='NEW',candidate_id=candidate.candidate_id)],retirements=[]))
    submissions=next(ref for ref in refs if ref['role']=='submissions')
    parameters=flow.planned.job.parameters
    record=owner.load(owner.InputRecord,owner.encode(dict(ordinal=0,input_key='input',issuer=dict(source='SEC',identifier=company.issuer_identifier),sources=refs,
        canonical=dict(recipe_sha256=recipe,preparation_sha256=prep,member_ordinal=0),
        exact_bundle=dict(exact_bundle_id=checkpoint['exact_bundle_id'],exact_bundle_sha256=checkpoint['exact_bundle_sha256'],expected_exact_run_key=checkpoint['pipeline_run_key']),
        submissions=dict(source=submissions,accession=parameters['accession'],form=parameters['form'],filing_date='2026-09-28'),parent_state=None,gaps=['SYNTHETIC_NOT_FINANCIAL_PROOF'])))
    complete=f.service.record_plan(accounting_sha256=accounting,records=[record],preparations=[dict(recipe_sha256=recipe,preparation_sha256=prep)],operation_at=f.now.isoformat(),
        export_mode='FULL',global_listing=dict(status='UNAVAILABLE',artifact_sha256=None,reason='Listing not registered'),
        global_indexes=[dict(name=n,status='UNAVAILABLE',artifact_sha256=None,reason='Index not registered') for n in ('S&P 500','DJIA','Nasdaq 100')],gaps=['SYNTHETIC'])
    f.planned_flow,f.planned_record=flow,record
    return complete,row,accounting


def checkpoint(f, job_id):
    with f.engine.connect() as connection:
        return connection.scalar(select(durable_job.c.checkpoint).where(durable_job.c.job_id==job_id))


def context(f,plan,duration=timedelta(minutes=5)):
    now=datetime.now(timezone.utc);key=uuid4().hex
    f.jobs.enqueue_scheduled(schedule_key=key,scheduled_for=now,kind='local-complete-input-resolution',parameters={'complete_plan_sha256':plan},due_at=now)
    claim=f.jobs.claim_next(owner=key,lease_duration=duration,allowed_kinds={'local-complete-input-resolution'})
    assert claim
    return JobContext(claim.job,Event(),f.jobs,claim.lease,lambda:datetime.now(timezone.utc))


@pytest.mark.parametrize('revision',[1,2])
def test_real_registered_canonical_verify_and_metadata_recovery_without_builder(resolved,monkeypatch,revision):
    f=resolved;p,_,_=f.prepare_refs(revision)
    monkeypatch.setattr(sync,'_derive_result',lambda *a,**k:pytest.fail('financial builder'))
    monkeypatch.setattr(f.canonical,'prepare',lambda *a:pytest.fail('canonical prepare'))
    result=f.service.resolve(p)
    monkeypatch.setattr(owner,'resolve_submissions',lambda *a,**k:pytest.fail('metadata reselection'))
    output=f.service.verify(result,p)
    assert output.scope=='RESOLVER_ONLY' and output.financial_status=='NOT_ASSESSED'
    if revision==1:
        expected=owner.load(owner.InputRecord,owner.encode(dict(output.inputs[0].input.model_dump(mode='json'),sources=[f.cf_ref,f.sub_ref,f.filing_ref]))).sources
        assert output.inputs[0].input.sources==expected
        assert [(ref.role,ref.item_kind) for ref in expected]==[('company_facts','aggregate'),('submissions','aggregate'),('filing','filing')]
    else:
        assert output.inputs[0].input.sources==f.planned_record.sources
        assert output.inputs[0].input.exact_bundle==f.planned_record.exact_bundle
        assert all(ref.artifact_sha256!=f.planned_flow.plan_artifact.content_sha256 for ref in output.inputs[0].input.sources)


def test_completed_handler_new_repositories_and_changed_source_pin(resolved,monkeypatch):
    f=resolved;p,_,_=f.prepare_refs();ctx=context(f,p);first=owner.CompleteInputResolutionHandler(f.service)(ctx)
    f.capture('submissions',b'{"later":"unavailable for this pinned plan"}',2)
    fresh=owner.CompleteInputResolver(CapturedCanonicalPreparer(f.companies,f.artifacts,f.objects,f.source),f.artifacts,f.objects,f.source,f.companies,f.service.history,f.publications)
    monkeypatch.setattr(fresh,'resolve',lambda *a:pytest.fail('re-resolved completed job'))
    monkeypatch.setattr(owner,'resolve_submissions',lambda *a,**k:pytest.fail('metadata reselection'))
    saved=replace(ctx.job,checkpoint=checkpoint(f,ctx.job.job_id))
    assert owner.CompleteInputResolutionHandler(fresh)(replace(ctx,job=saved))==first


def test_actual_retained_parent_and_registered_candidate_bindings(resolved):
    f=resolved;p,row,a=f.prepare_refs(parent=True)
    ledger=owner.load_accounting(f.canonical._read(a))
    assert f.publications.retained_members(f.publications.candidate(ledger.selected_members[0].candidate_id).expected_parent_release_id)==((row.security_id,row.snapshot_id,None),)
    assert f.service.verify(f.service.resolve(p),p).inputs[0].input.parent_state.status=='UNAVAILABLE'


@pytest.mark.parametrize('transition',['expire','reclaim'])
def test_expired_or_reclaimed_worker_cannot_store_resolution_checkpoint(resolved,monkeypatch,transition):
    f=resolved;p,_,_=f.prepare_refs();ctx=context(f,p)
    with f.engine.begin() as connection:
        connection.execute(update(durable_job).where(durable_job.c.job_id==ctx.job.job_id).values(lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    if transition=='reclaim':
        f.jobs.recover_expired();sleep(.01)
        replacement=f.jobs.claim_next(owner='replacement',lease_duration=timedelta(minutes=5),allowed_kinds={'local-complete-input-resolution'})
        assert replacement.lease.generation>ctx.lease.generation
    monkeypatch.setattr(f.service,'resolve',lambda *a:pytest.fail('expired worker resolved'))
    with pytest.raises(LeaseLost):owner.CompleteInputResolutionHandler(f.service)(ctx)
    assert checkpoint(f,ctx.job.job_id) is None


@pytest.mark.parametrize('expired',[True,False])
def test_actual_checkpoint_lock_wait_expiry_and_live_neighbor(resolved,monkeypatch,expired):
    f=resolved;p,_,_=f.prepare_refs();ctx=context(f,p,duration=timedelta(seconds=2 if expired else 30));entered=Event()
    original=f.service.resolve
    blocker=f.engine.connect();transaction=blocker.begin()
    def resolve(plan):
        result=original(plan)
        blocker.execute(select(durable_job.c.job_id).where(durable_job.c.job_id==ctx.job.job_id).with_for_update())
        entered.set();return result
    monkeypatch.setattr(f.service,'resolve',resolve)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(owner.CompleteInputResolutionHandler(f.service),ctx)
            assert entered.wait(5)
            sleep(2.1 if expired else .05);transaction.commit()
            if expired:
                with pytest.raises(LeaseLost):future.result(5)
                assert checkpoint(f,ctx.job.job_id) is None
            else:assert future.result(5)==checkpoint(f,ctx.job.job_id)
    finally:
        if transaction.is_active:transaction.rollback()
        blocker.close()
