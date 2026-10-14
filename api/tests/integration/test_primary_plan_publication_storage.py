"""Actual pair3/publication storage owners; all financial bodies are synthetic."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import event, func, select

from screener.artifacts import store_evidence
from screener.display_publication import (CAPTURE_FIELDS, DisplayCompanyInput, DisplayReleasePreparer,
    canonical_bytes, capture_fields)
from screener.postgres import publication_candidate, data_release, source_observation
from screener.primary_input_plan import MAXIMUM_BYTES, encode_primary_plan
from screener.shared_companies import SnapshotArtifact
from screener.publication import PublicationRepository
from screener.source_observations import SourceItemIdentity
from test_primary_input_transport_storage import planned_flow, storage, database_url, pytestmark
from test_sec_ingestion_storage import NOW, LEASE, _context


def staged(storage, key='plan-publication'):
    flow = planned_flow(storage, key)
    checkpoint = flow.stage(_context(flow.planned, flow.jobs))
    flow.jobs.complete(flow.planned.lease, final_checkpoint=checkpoint, now=NOW)
    flow.jobs.enqueue_scheduled(schedule_key=key+':publisher', scheduled_for=NOW, kind='publication-fixture', parameters={}, due_at=NOW)
    claim = flow.jobs.claim_next(owner=key+':publisher', lease_duration=LEASE, now=NOW, allowed_kinds={'publication-fixture'})
    repo = PublicationRepository(flow.engine, clock=lambda: NOW, object_store=flow.objects)
    graph = flow.stage._history.load(checkpoint['exact_bundle_id'])
    return NS(flow=flow, checkpoint=checkpoint, claim=claim, repo=repo,
        sources=tuple(s.source_observation_id for s in graph.sources))


def register(case, key='candidate', **changes):
    cp=case.checkpoint
    args=dict(request_key=key,snapshot_id=cp['snapshot_id'],expected_parent_release_id=None,
        source_observation_ids=case.sources,cause_type='source_change',exact_bundle_id=cp['exact_bundle_id'],
        expected_exact_run_key=cp['pipeline_run_key'])
    args.update(changes)
    return case.repo.register_candidate(case.claim.lease, **args)


def display(case, candidate):
    flow,cp=case.flow,case.checkpoint
    row=flow.stage._companies.snapshot(cp['snapshot_id']).snapshot.canonical_payload
    captures={}
    for role in CAPTURE_FIELDS:
        fields=capture_fields(role,row)
        if any(value not in (None,[],{}) for value in fields.values()):
            raw=store_evidence(canonical_bytes(fields),'application/json',flow.objects,flow.artifacts)
            body=dict(role=role,row_fields=fields,provider='synthetic-retained-fixture',captured_at=NOW.isoformat(),input_artifacts=[raw.content_sha256])
            artifact=store_evidence(canonical_bytes(body),'application/json',flow.objects,flow.artifacts)
            captures[role]=dict(status='CAPTURED',artifact_sha256=artifact.content_sha256)
        else:captures[role]=dict(status='ABSENT',reason='No synthetic input')
    member=DisplayCompanyInput(cp['security_id'],cp['snapshot_id'],row,captures,candidate.source_observation_ids,
        exact_bundle_id=cp['exact_bundle_id'],expected_exact_run_key=cp['pipeline_run_key'])
    return DisplayReleasePreparer(flow.engine,flow.objects).prepare([member],capture_mode='caller_captured_enrichment',
        limitations=['Synthetic mechanics only; no financial/source acceptance'])


def tally(case):
    with case.flow.engine.connect() as connection:
        return tuple(connection.scalar(select(func.count()).select_from(table)) for table in (publication_candidate,data_release,source_observation))


@pytest.mark.parametrize('operation',['direct','display'])
def test_completed_pair3_producer_register_activate_and_replay_without_plan_source(storage,monkeypatch,operation):
    case=staged(storage);flow=case.flow
    monkeypatch.setattr(flow.source,'latest_sec_filing',lambda *a:pytest.fail('current source selection'))
    locks=set();read=flow.objects.read_verified
    def observe_read(*args,**kwargs):
        assert not locks, 'object read after publication lock'
        return read(*args,**kwargs)
    def observe_sql(connection,cursor,statement,parameters,context,executemany):
        if 'FOR UPDATE' in statement:
            assert connection.in_transaction()
            locks.add(connection)
    def release_locks(connection):
        locks.discard(connection)
    monkeypatch.setattr(flow.objects,'read_verified',observe_read)
    event.listen(flow.engine,'after_cursor_execute',observe_sql)
    event.listen(flow.engine,'commit',release_locks)
    event.listen(flow.engine,'rollback',release_locks)
    try:
        candidate=register(case);assert not locks;assert register(case)==candidate;assert not locks
        if operation=='display':
            prepared=display(case,candidate);assert not locks
            invoke=lambda:case.repo.publish_display(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],
                expected_parent_release_id=None,prepared=prepared,object_store=flow.objects)
        else:invoke=lambda:case.repo.publish(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],expected_parent_release_id=None)
        release=invoke();assert not locks;assert invoke()==release;assert not locks
    finally:
        event.remove(flow.engine,'after_cursor_execute',observe_sql)
        event.remove(flow.engine,'commit',release_locks)
        event.remove(flow.engine,'rollback',release_locks)
    with flow.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(source_observation).where(
            source_observation.c.artifact_sha256==flow.plan_artifact.content_sha256))==0
    assert flow.stage._history.load(case.checkpoint['exact_bundle_id']).sources


@pytest.mark.parametrize('operation',['register','register_replay','publish','publish_replay','display','display_replay'])
def test_every_publication_entry_refuses_absent_repository_preflight(storage,monkeypatch,operation):
    case=staged(storage);candidate=None;prepared=None
    if operation!='register':candidate=register(case)
    if operation.startswith('display'):prepared=display(case,candidate)
    def invoke():
        if operation.startswith('register'):return register(case)
        if operation.startswith('display'):return case.repo.publish_display(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],
            expected_parent_release_id=None,prepared=prepared,object_store=case.flow.objects)
        return case.repo.publish(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],expected_parent_release_id=None)
    if operation in {'publish_replay','display_replay'}:invoke()
    before=tally(case)
    original=case.repo._prepare_primary_plans
    def unprepared(*args,**kwargs):
        return {key:(value[0],value[1],value[2],None) for key,value in original(*args,**kwargs).items()}
    monkeypatch.setattr(case.repo,'_prepare_primary_plans',unprepared)
    with pytest.raises(ValueError,match='verified preparation outside SQL'):invoke()
    assert tally(case)==before


@pytest.mark.parametrize('operation',['register','publish','display'])
@pytest.mark.parametrize('failure',['missing','corrupt'])
def test_registered_plan_object_failure_refuses_before_publication_mutation(storage,operation,failure):
    case=staged(storage);candidate=None;prepared=None
    if operation!='register':candidate=register(case)
    if operation=='display':prepared=display(case,candidate)
    flow=case.flow;before=tally(case)
    if failure=='missing':flow.objects.client.delete_object(Bucket=flow.objects.bucket,Key=flow.plan_artifact.object_key)
    else:flow.objects.client.put_object(Bucket=flow.objects.bucket,Key=flow.plan_artifact.object_key,Body=b'{"malformed":"unrelated plan"}')
    with pytest.raises(Exception):
        if operation=='register':register(case)
        elif operation=='publish':case.repo.publish(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],expected_parent_release_id=None)
        else:case.repo.publish_display(case.claim.lease,request_key='release',candidate_ids=[candidate.candidate_id],expected_parent_release_id=None,
            prepared=prepared,object_store=flow.objects)
    assert tally(case)==before


@pytest.mark.parametrize('changes',[dict(exact_bundle_id=None,expected_exact_run_key=None),
    dict(expected_exact_run_key='another-run'),dict(exact_bundle_id=999999),dict(exact_bundle_id=True)])
def test_registered_plan_cannot_use_missing_unrelated_or_false_pair(storage,changes):
    case=staged(storage);before=tally(case)
    with pytest.raises(ValueError):register(case,**changes)
    assert tally(case)==before


def test_accepted_a_b_a_between_preflight_and_sql_refuses_with_no_candidate_write(storage,monkeypatch):
    case=staged(storage);flow=case.flow;original=case.repo._prepare_primary_plans
    def change_after_preflight(*args,**kwargs):
        prepared=original(*args,**kwargs)
        item,obs=flow.native.item,flow.native.observation
        for index in (1,2):
            metadata=dict(obs.canonical_metadata)
            if index==1:metadata['synthetic_revision']='B'
            flow.source.record(case.claim.lease,item=SourceItemIdentity(item.source_system,item.item_kind,item.source_key,
                item.issuer_source_identifier,item.parent_source_item_id),item_outcome_key=f'publication:recurrence:{index}',
                state='present',metadata=metadata,artifact_sha256=obs.artifact_sha256,source_url=obs.source_url,
                detected_at=NOW+timedelta(seconds=index))
        return prepared
    monkeypatch.setattr(case.repo,'_prepare_primary_plans',change_after_preflight)
    with pytest.raises(ValueError,match='verified preparation outside SQL'):register(case)
    with flow.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(publication_candidate))==0
    assert case.repo.active() is None


@pytest.mark.parametrize('failure',['malformed','oversized','security','manifest','extra_dependency','missing_dependency','relabelled_dependency'])
def test_arbitrary_registered_plan_and_pair_role_cannot_replace_transport_admission(storage,monkeypatch,failure):
    flow=planned_flow(storage,'adversarial-pair');saved=[];original=flow.stage._store_pair
    def capture(*args,**kwargs):
        saved.append((args,kwargs))
        return original(*args,**kwargs)
    monkeypatch.setattr(flow.stage,'_store_pair',capture)
    flow.stage(_context(flow.planned,flow.jobs))
    args,kwargs=saved[0]
    _,values,result,sources,engine_identity,original_facts=args
    values=dict(values);planned=kwargs['planned']
    if failure in {'malformed','oversized','security'}:
        body=({'malformed':b'{}','oversized':b' '*(MAXIMUM_BYTES+1)}.get(failure)
              if failure!='security' else encode_primary_plan(replace(flow.plan,security_id=flow.plan.security_id+999)))
        artifact=store_evidence(body,'application/json',flow.objects,flow.artifacts)
        links=tuple(SnapshotArtifact(artifact.content_sha256,link.role) if link.role=='primary_input_plan' else link for link in planned.artifacts)
        planned=replace(planned,plan_sha256=artifact.content_sha256,artifacts=links,
            manifest={**planned.manifest,'primary_input_plan_sha256':artifact.content_sha256})
        values['artifacts']=tuple(SnapshotArtifact(artifact.content_sha256,link.role) if link.role=='primary_input_plan' else link for link in values['artifacts'])
    elif failure=='manifest':
        planned=replace(planned,manifest={**planned.manifest,'financial_consumer':'CALLER_ASSERTED'})
    elif failure=='extra_dependency':
        artifact=store_evidence(b'synthetic unrelated dependency','text/plain',flow.objects,flow.artifacts)
        values['artifacts']=(*values['artifacts'],SnapshotArtifact(artifact.content_sha256,'primary_input_dependency'))
    else:
        target=next(link for link in planned.artifacts if link.role=='primary_input_dependency')
        values['artifacts']=tuple((SnapshotArtifact(link.content_sha256,'unrelated_role') if failure=='relabelled_dependency' else None)
            if link==target else link for link in values['artifacts'])
        values['artifacts']=tuple(link for link in values['artifacts'] if link is not None)
    parameters={**flow.planned.job.parameters,'primary_input_plan_sha256':planned.plan_sha256}
    flow.jobs.enqueue_scheduled(schedule_key='adversarial:producer',scheduled_for=NOW,kind='adversarial-producer',parameters=parameters,due_at=NOW)
    claim=flow.jobs.claim_next(owner='adversarial',lease_duration=LEASE,now=NOW,allowed_kinds={'adversarial-producer'})
    # Exercise the real pair writer with intentionally untrusted internal arguments.
    checkpoint=original(_context(claim,flow.jobs),values,result,sources,engine_identity,original_facts,planned=planned)
    case=NS(flow=flow,checkpoint=checkpoint,claim=claim,repo=PublicationRepository(flow.engine,clock=lambda:NOW,object_store=flow.objects),
        sources=tuple(s.source_observation_id for s in flow.stage._history.load(checkpoint['exact_bundle_id']).sources))
    before=tally(case)
    with pytest.raises(ValueError):register(case)
    assert tally(case)==before and case.repo.active() is None


def test_legacy_bootstrap_cannot_admit_a_plan_without_its_bound_pair(storage):
    from screener.postgres import current_company_snapshot
    case=staged(storage)
    with case.flow.engine.begin() as connection:
        connection.execute(current_company_snapshot.insert().values(security_id=case.checkpoint['security_id'],snapshot_id=case.checkpoint['snapshot_id']))
    before=tally(case)
    with pytest.raises(ValueError,match='exact_bundle_id'):
        case.repo.bootstrap(case.claim.lease,request_key='unbound-plan-bootstrap')
    assert tally(case)==before and case.repo.active() is None
