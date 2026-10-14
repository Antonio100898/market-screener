"""One-row inspection through actual inventory/capture/scheduling owners; synthetic bytes."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
import hashlib
import json
import threading

import pytest

from screener import sec_inventory_plan as owner
from screener.artifacts import store_evidence
from screener.durable_jobs import DurableJobRepository, JobIdentityConflict, LeaseToken
from screener.job_runtime import JobContext
from screener.sec_ingestion import SecResourceFetchHandler, _root_resources
from screener.source_observations import StoredSourceObservation
from test_sec_inventory_plan import fixture, finish, NOW, fixture_bodies, day_url
from test_sec_completed_capture import Fixture
from test_sec_ingestion import _index, _row


class JobEngine:
    def __init__(self, jobs): self.jobs, self.row = jobs, None
    def connect(self): return self
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, statement):
        values = statement.compile().params
        key = values['schedule_key_1']; scheduled = values['scheduled_for_1']
        number = self.jobs.schedules.get((key, scheduled))
        self.row = None if number is None else {**asdict(self.jobs.jobs[number]), 'matched_occurrence_id':number}
        return self
    def mappings(self): return self
    def one_or_none(self): return self.row


def case(bodies=None):
    value = fixture(bodies)
    value.jobs.job_for_occurrence = DurableJobRepository(JobEngine(value.jobs)).job_for_occurrence
    value.root, value.job = finish(value, value.jobs.context())
    value.args = dict(job_id=value.job.job_id, occurrence_id=value.job.occurrence_id,
        schedule_key=owner.NAMESPACE+value.job.parameters['operation_key'], scheduled_for=NOW,
        day='2026-09-28', entry_ordinal=0)
    return value


def inspect(value, **changes):
    return value.handler.inspect_completed_entry(**{**value.args, **changes})


def complete_resource(value):
    rows = value.handler._read_chunks(value.root.days[0].chunks)
    child = rows[0].request.child
    job = replace(value.jobs.jobs[child.job_id], status='running', ownership_generation=1, owner='resource')
    value.jobs.jobs[job.job_id] = job
    sources = value.sources
    def by_sha(digest):
        matches = [obs for (_, sha), obs in sources.observations.items() if sha == digest]
        assert len(matches) == 1
        obs = matches[0]
        item = next(item for item in sources.items.values() if item.source_item_id == obs.source_item_id)
        return StoredSourceObservation(item, obs, False)
    def filing(identifier):
        obs = next(obs for obs in sources.observations.values() if obs.source_observation_id == identifier)
        return by_sha(obs.observation_sha256)
    sources.by_sha256 = by_sha
    sources.sec_filing = filing
    sources.latest_sec_filing = filing
    sources.delivery = lambda occurrence_id, **unused:next(d for d in sources.deliveries.values() if d.occurrence_id == occurrence_id)
    sources.record = lambda lease, **fields:sources.record_with_acquisition_receipt(lease,
        receipt_key='resource:'+str(lease.job_id)+':'+fields['item_outcome_key'],
        request_identity_sha256='a'*64, **fields)
    raw = Fixture().raw
    resources = _root_resources(accession=job.parameters['accession'], cik=job.parameters['cik'], filename=job.parameters['filename'])
    value.transport.bodies.update({r.url:raw[r.role] for r in resources})
    resource = SecResourceFetchHandler(edgar=value.transport, object_store=value.objects,
        artifacts=value.artifacts, observations=sources, jobs=value.jobs, clock=lambda:NOW, annotate_annual=True)
    context = JobContext(job, threading.Event(), value.jobs, LeaseToken(job.job_id,'resource',1), lambda:NOW)
    checkpoint = resource(context)
    value.jobs.jobs[job.job_id] = replace(value.jobs.jobs[job.job_id], checkpoint=checkpoint, status='succeeded')
    return resource, child


def forbid(value, monkeypatch):
    for obj, names in ((value.transport, ('fetch',)), (value.jobs, ('enqueue_child','save_checkpoint')),
            (value.sources, ('record_with_acquisition_receipt',))):
        for name in names: monkeypatch.setattr(obj,name,lambda *a,**kw:pytest.fail('inspection wrote/fetched'))


def test_actual_pending_capture_and_original_rows_no_writes(monkeypatch):
    value = case(); forbid(value,monkeypatch)
    result = inspect(value)
    assert result['entry']['physical_line'] == 4 and result['entry']['filing_date'] == '2026-09-27'
    assert result['capture']['status'] == 'NOT_COMPLETED'
    assert result['stage']['status'] == 'NOT_INSPECTED_CAPTURE_NOT_VERIFIED'
    assert result['listing_security_status'] == result['financial_status'] == result['currentness_status'] == 'NOT_ASSESSED'
    assert inspect(value,entry_ordinal=3)['issuer_group_ordinal'] is None
    for ordinal in (1,2):
        other = inspect(value,entry_ordinal=ordinal)
        assert other['capture'] is None and other['stage']['status'] == 'NOT_APPLICABLE_TO_RETAINED_ROW'
    result['entry']['company_name'] = 'changed'
    assert inspect(value)['entry']['company_name'] == 'A'


@pytest.mark.parametrize('stage', ['matched','absent','conflict'])
def test_actual_verified_capture_stage_owner_and_no_financial_followup(monkeypatch,stage):
    value = case(); resource, child = complete_resource(value)
    request = resource.candidate_stage_request(value.jobs.jobs[child.job_id],value.jobs.jobs[child.job_id].checkpoint)
    number = value.jobs.schedules[request.schedule_key,request.scheduled_for]
    if stage == 'absent': del value.jobs.schedules[request.schedule_key,request.scheduled_for]
    elif stage == 'conflict':value.jobs.jobs[number] = replace(value.jobs.jobs[number],parameters={'wrong':1})
    else:value.jobs.jobs[number] = replace(value.jobs.jobs[number],status='running',checkpoint={'unqualified':True},ownership_generation=5)
    forbid(value,monkeypatch)
    if stage == 'conflict':
        with pytest.raises(JobIdentityConflict):inspect(value)
    else:
        result = inspect(value)
        assert result['capture']['status'] == 'VERIFIED'
        assert result['capture']['all_applicable_root_structures_validated'] is True
        assert result['capture']['roots'][0]['root_structure_validated'] is False
        assert result['stage']['status'] == ('MATCHED_ASSOCIATION' if stage == 'matched' else 'ABSENT_ASSOCIATION')
        if stage == 'matched':assert result['stage']['checkpoint_present'] and 'checkpoint' not in result['stage']


class IntSubclass(int): pass


@pytest.mark.parametrize('field,bad', [(f,v) for f in ('job_id','occurrence_id') for v in (True,1.0,IntSubclass(1),0,-1,2**63)]
    + [('schedule_key','bad'),('schedule_key',owner.NAMESPACE+' x'),('schedule_key',owner.NAMESPACE+'é'*513),
       ('scheduled_for',NOW.replace(tzinfo=None)),('day','2026-02-30'),('day','2026-W41-1'),
       ('day',date(2026,9,28)),('entry_ordinal',True),('entry_ordinal',1.0),('entry_ordinal',IntSubclass(1)),
       ('entry_ordinal',-1),('entry_ordinal',100000)])
def test_strict_scalars_before_any_owner(monkeypatch,field,bad):
    value = case()
    monkeypatch.setattr(value.jobs,'scheduled_job',lambda **kw:pytest.fail('bad scalar reached owner'))
    with pytest.raises(ValueError):inspect(value,**{field:bad})


@pytest.mark.parametrize('ordinal', [4,99])
def test_absent_retained_entry_is_not_empty_inspection(ordinal):
    with pytest.raises(ValueError,match='absent'):inspect(case(),entry_ordinal=ordinal)


@pytest.mark.parametrize('fault', ['status','checkpoint','generation','build'])
@pytest.mark.parametrize('ordinal', [0,1])
def test_final_root_drift_refuses_every_return(monkeypatch,fault,ordinal):
    value = case(); old = value.handler._read
    def read(ref):
        raw = old(ref)
        if ref.artifact_sha256 == value.root.days[0].chunks[0].artifact_sha256:
            # Trigger after full recover, on the one selected-chunk reread.
            read.count += 1
            if read.count == 2:
                if fault == 'build':monkeypatch.setattr(owner,'producer_build_sha256',lambda:'f'*64)
                else:
                    change={'status':dict(status='running'),'checkpoint':dict(checkpoint={'changed':True}),
                            'generation':dict(ownership_generation=9)}[fault]
                    value.jobs.jobs[value.job.job_id] = replace(value.job,**change)
        return raw
    read.count = 0; monkeypatch.setattr(value.handler,'_read',read)
    with pytest.raises(ValueError):inspect(value,entry_ordinal=ordinal)


@pytest.mark.parametrize('fault', ['immutable','returned','checkpoint'])
def test_post_capture_bridge_refuses_changed_identity_or_verified_checkpoint(monkeypatch,fault):
    value = case(); _, child = complete_resource(value)
    actual = SecResourceFetchHandler.recover_completed_capture
    def capture(handler,**identity):
        result = actual(handler,**identity)
        if fault == 'immutable':value.jobs.jobs[child.job_id] = replace(value.jobs.jobs[child.job_id],priority=99)
        elif fault == 'returned':result['job']['job_id'] = child.job_id+100
        else:value.jobs.jobs[child.job_id] = replace(value.jobs.jobs[child.job_id],checkpoint={'changed':True})
        return result
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    with pytest.raises((ValueError,JobIdentityConflict)):inspect(value)


def test_capacity_returns_refs_and_still_checks_root(monkeypatch):
    value = case(); actual = SecResourceFetchHandler.recover_completed_capture
    def capture(handler,**identity):
        result = actual(handler,**identity)
        result['untruncated_evidence'] = ''
        size = len(json.dumps(result,sort_keys=True,separators=(',',':')).encode())
        result['untruncated_evidence'] = 'x'*(owner.MAX_ENVELOPE_BYTES-size-64)
        assert len(json.dumps(result,sort_keys=True,separators=(',',':')).encode()) <= owner.MAX_ENVELOPE_BYTES
        return result
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    result = inspect(value)
    assert result['status'] == 'CAPACITY_UNAVAILABLE' and result['gap']['required_is_lower_bound']
    assert result['gap']['required_bytes'] > owner.MAX_ENVELOPE_BYTES
    assert 'entry' not in result and result['entry_ordinal'] == 0


def test_duplicate_physical_rows_share_child_without_collapsing():
    bodies = fixture_bodies();row = _row('10-K','A','1234','2026-09-27','0000001234-26-000001')
    bodies[day_url(date(2026,9,28))] = _index(row,row)
    value = case(bodies)
    one,two = inspect(value),inspect(value,entry_ordinal=1)
    assert one['entry']['request']['child'] == two['entry']['request']['child']
    assert one['entry']['ordinal'] != two['entry']['ordinal']
    assert one['entry']['physical_line'] != two['entry']['physical_line']


@pytest.mark.parametrize('status', ['pending','running','retry_wait','failed'])
def test_actual_operational_capture_status_is_not_stage_absence(status):
    value = case();row = value.handler._read_chunks(value.root.days[0].chunks)[0]
    child = row.request.child
    value.jobs.jobs[child.job_id] = replace(value.jobs.jobs[child.job_id],status=status)
    result = inspect(value)
    assert result['capture']['status'] == ('FAILED' if status == 'failed' else 'NOT_COMPLETED')
    assert result['stage']['status'] == 'NOT_INSPECTED_CAPTURE_NOT_VERIFIED'


@pytest.mark.parametrize('status', ['ASSOCIATION_UNAVAILABLE','CAPACITY_UNAVAILABLE','ROOT_STRUCTURE_UNAVAILABLE'])
def test_capture_gap_composition_and_legitimate_later_progress(monkeypatch,status):
    value = case();actual = SecResourceFetchHandler.recover_completed_capture
    def capture(handler,**identity):
        result = actual(handler,**identity)
        result['status'] = status;result['gap'] = {'reason':'labelled owner protocol gap'}
        number = identity['job_id']
        value.jobs.jobs[number] = replace(value.jobs.jobs[number],status='running',ownership_generation=7)
        return result
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    result = inspect(value)
    assert result['capture']['status'] == status and result['capture']['gap']['reason']
    assert result['stage']['status'] == 'NOT_INSPECTED_CAPTURE_NOT_VERIFIED'


def test_full_recovery_then_only_selected_chunk_reread(monkeypatch):
    value = case();calls=[];old=value.handler._read
    monkeypatch.setattr(value.handler,'_read',lambda ref:(calls.append(ref.artifact_sha256),old(ref))[1])
    inspect(value,entry_ordinal=1)
    expected=[value.job.checkpoint['root']['artifact_sha256'],
        *[r.artifact_sha256 for r in value.root.ticker_chunks],
        *[r.artifact_sha256 for r in value.root.issuer_group_chunks],
        *[r.artifact_sha256 for day in value.root.days for r in day.chunks],
        value.root.days[0].chunks[0].artifact_sha256]
    assert calls == expected


def test_old_stamp_refusal_and_original_exception_identity(monkeypatch):
    value = case();old=value.job
    value.jobs.jobs[old.job_id] = replace(old,parameters={**old.parameters,'producer_build_sha256':'f'*64})
    with pytest.raises(ValueError,match='build'):inspect(value)
    value.jobs.jobs[old.job_id]=old
    error=RuntimeError('actual original owner refusal')
    def capture(*args,**kw):raise error
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    with pytest.raises(RuntimeError) as failure:inspect(value)
    assert failure.value is error


def test_combined_capacity_return_also_has_root_end_fence(monkeypatch):
    value = case();actual=SecResourceFetchHandler.recover_completed_capture
    def capture(handler,**identity):
        result=actual(handler,**identity);result['original_evidence']='x'*owner.MAX_ENVELOPE_BYTES
        value.jobs.jobs[value.job.job_id]=replace(value.job,ownership_generation=8)
        return result
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    with pytest.raises(ValueError,match='root changed'):inspect(value)


@pytest.mark.parametrize('delta',[-1,0,1])
def test_combined_exact_byte_frontier_without_truncating_valid_capture(monkeypatch,delta):
    value=case();actual=SecResourceFetchHandler.recover_completed_capture;padding=['']
    def capture(handler,**identity):
        result=actual(handler,**identity);result['exact_protocol_evidence']=padding[0]
        assert len(json.dumps(result,sort_keys=True,separators=(',',':')).encode()) <= owner.MAX_ENVELOPE_BYTES
        return result
    monkeypatch.setattr(SecResourceFetchHandler,'recover_completed_capture',capture)
    baseline=inspect(value)
    size=len(json.dumps(baseline,sort_keys=True,separators=(',',':')).encode())
    padding[0]='x'*(owner.MAX_ENVELOPE_BYTES+delta-size)
    result=inspect(value)
    if delta<=0:
        assert result['status']=='INSPECTED'
        assert result['capture']['exact_protocol_evidence']==padding[0]
        assert len(json.dumps(result,sort_keys=True,separators=(',',':')).encode())==owner.MAX_ENVELOPE_BYTES+delta
    else:
        assert result['status']=='CAPACITY_UNAVAILABLE' and result['gap']['required_bytes']>owner.MAX_ENVELOPE_BYTES


@pytest.mark.parametrize('field',['scheduled_for','due_at'])
def test_authentic_offset_child_uses_instant_comparison_and_keeps_original_text(field):
    value=case();records=list(value.handler._read_chunks(value.root.days[0].chunks));row=records[0]
    child=row.request.child
    shifted=getattr(child,field).astimezone(timezone(timedelta(hours=3)))
    records[0]=row.model_copy(update={'request':row.request.model_copy(update={'child':child.model_copy(update={field:shifted})})})
    raw,=owner.pack_chunks(records);artifact=store_evidence(raw,owner.MEDIA_TYPE,value.objects,value.artifacts)
    chunk=value.root.days[0].chunks[0].model_copy(update={'artifact_sha256':artifact.content_sha256,'byte_size':artifact.byte_size})
    days=(value.root.days[0].model_copy(update={'chunks':(chunk,)}),*value.root.days[1:])
    value.root=value.root.model_copy(update={'days':days})
    root_artifact=store_evidence(owner.encode(value.root.model_dump(mode='json')),owner.MEDIA_TYPE,value.objects,value.artifacts)
    checkpoint=deepcopy(value.job.checkpoint);checkpoint['days']=[d.model_dump(mode='json') for d in days]
    checkpoint['root']=dict(artifact_sha256=root_artifact.content_sha256,byte_size=root_artifact.byte_size)
    value.job=replace(value.job,checkpoint=checkpoint);value.jobs.jobs[value.job.job_id]=value.job
    assert value.handler.recover_completed(value.job)==value.root
    result=inspect(value)
    assert result['status']=='INSPECTED' and result['capture']['status']=='NOT_COMPLETED'
    assert result['entry']['request']['child'][field].endswith('+03:00')
    assert value.jobs.jobs[value.job.job_id].checkpoint==checkpoint


@pytest.mark.parametrize('field',['owner','error_summary'])
@pytest.mark.parametrize('bad',['x'*65537,'é'*32769,['wrong-type'],'embedded\0nul'],ids=['codepoints','utf8','type','nul'])
def test_stage_text_refusal_precedes_combined_serializer(monkeypatch,field,bad):
    value=case();resource,child=complete_resource(value)
    request=resource.candidate_stage_request(value.jobs.jobs[child.job_id],value.jobs.jobs[child.job_id].checkpoint)
    number=value.jobs.schedules[request.schedule_key,request.scheduled_for]
    value.jobs.jobs[number]=replace(value.jobs.jobs[number],**{field:bad})
    original=owner._capture_result_json
    def serializer(data):
        if data.get('stage',{}).get('status')=='MATCHED_ASSOCIATION':pytest.fail('unbounded diagnostic reached serializer')
        return original(data)
    monkeypatch.setattr(owner,'_capture_result_json',serializer)
    with pytest.raises(ValueError):inspect(value)


@pytest.mark.parametrize('field',['owner','error_summary'])
@pytest.mark.parametrize('valid',[None,'x'*65536,'é'*32768],ids=['none','ascii-boundary','utf8-boundary'])
def test_stage_text_valid_boundaries_keep_complete_diagnostic(field,valid):
    value=case();resource,child=complete_resource(value)
    request=resource.candidate_stage_request(value.jobs.jobs[child.job_id],value.jobs.jobs[child.job_id].checkpoint)
    number=value.jobs.schedules[request.schedule_key,request.scheduled_for]
    value.jobs.jobs[number]=replace(value.jobs.jobs[number],**{field:valid})
    result=inspect(value)
    assert result['status']=='INSPECTED' and result['stage'][field]==valid
