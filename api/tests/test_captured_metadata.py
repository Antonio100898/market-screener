"""Existing SEC metadata definitions from registered bytes; no acquisition."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pytest

from screener import captured_metadata as owner, complete_input_plan as plans
from test_complete_input_plan import fixture, NOW


def submissions():
    return dict(cik='0000000001',stateOfIncorporation='DE',stateOfIncorporationDescription='Delaware',sic='2834',
        filings=dict(files=[dict(name='CIK0000000001-submissions-001.json',filingFrom='2020-01-01',filingTo='2022-01-01',filingCount=1)],
            recent=dict(form=['10-K','10-Q','6-K','8-K'],filingDate=['2026-03-01','2026-05-01','2026-05-01','2026-06-01'],
                reportDate=['2025-12-31','2026-03-31','',''],accessionNumber=['0000000001-26-000001','0000000001-26-000002','0000000001-26-000003','0000000001-26-000004'],
                primaryDocument=['annual.htm','q.htm','six.htm','event.htm'],items=['','','','1.01, 2.02'])))


def retained(f,data=None):
    sha=f.put(data or submissions())
    f.stored.observation.artifact_sha256=sha
    f.stored.observation.canonical_metadata={'role':'submissions','cik':'0000000001','capture_revision':2,'capture_fidelity':'RAW_HTTP_BYTES'}
    ref=dict(source={**f.source,'role':'submissions','artifact_byte_size':len(f.objects.rows[sha]),'captured_source':{**f.source['captured_source'],'role':'submissions','artifact_sha256':sha},'artifact_sha256':sha},accession='0000000001-26-000001',form='10-K',filing_date='2026-03-01')
    return owner.SubmissionsRef.model_validate_json(json.dumps(ref)),sha


def test_registered_metadata_reuses_existing_definitions(fixture):
    f=fixture;ref,sha=retained(f)
    value=owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})
    assert value['incorporation']=='DE' and value['first_visible_filing']=='2020-01-01'
    assert value['material_events_scanned_since']=='2026-03-01'
    assert value['quarterly_filing']['accession']=='0000000001-26-000003'
    assert value['quarterly_filing']['period'] is None
    assert value['listing_status']=='NOT_ASSESSED'
    assert value['coverage']=='CAPTURED_RECENT_INDEX'
    assert value['sector']==owner.sector_for('2834')
    assert value['material_events']==owner.material_events(submissions())[0]


def test_integrated_metadata_recovery_never_reselects(fixture,monkeypatch):
    f=fixture;ref,sha=retained(f);r=f.record.model_dump(mode='json')
    r['sources']=[ref.source.model_dump(mode='json')];r['submissions']=ref.model_dump(mode='json')
    a=json.loads(json.dumps(f.accounting));a['universe'][0]['artifact_sha256s'].append(sha)
    plan=f.plan(plans.load(plans.InputRecord,plans.encode(r)),a);resolution=f.resolver.resolve(plan)
    monkeypatch.setattr(plans,'resolve_submissions',lambda *a,**kw:pytest.fail('reselected metadata'))
    output=f.resolver.verify(resolution,plan)
    assert output.inputs[0].metadata_sha256 is not None
    assert f.objects.reads[sha]==4 # first metadata parse; replay validates immutable output without reselection


@pytest.mark.parametrize('change',[
    lambda d:d.update(cik='2'),lambda d:d['filings']['recent'].update(items=['']),
    lambda d:d['filings']['recent'].update(filingDate=['bad']*4),
    lambda d:d['filings']['recent'].update(reportDate=[None]*4),
    lambda d:d.update(stateOfIncorporation=True),lambda d:d.update(sic=True),
    lambda d:d['filings']['files'][0].update(filingFrom=20200101),
    lambda d:d['filings'].update(files='bad'),
])
def test_metadata_incomplete_context_refuses_instead_of_scanned_empty(fixture,change):
    d=submissions();change(d);f=fixture;ref,sha=retained(f,d)
    with pytest.raises((ValueError,TypeError)):
        owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})


def test_registered_source_binding_precedes_parse(fixture):
    f=fixture;ref,sha=retained(f);f.stored.item.issuer_source_identifier='2'
    f.objects.read_verified=lambda *a:pytest.fail('foreign source parsed')
    with pytest.raises(ValueError,match='issuer'):owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})


def test_missing_or_wrong_role_cannot_be_empty_metadata(fixture):
    f=fixture;ref,sha=retained(f);f.stored.observation.canonical_metadata['role']='company_facts'
    with pytest.raises(ValueError,match='source|root'):owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})


def test_submissions_new_consumer_byte_quota_precedes_parse(fixture):
    f=fixture;ref,sha=retained(f);f.artifacts.rows[sha]=replace(f.artifacts.rows[sha],byte_size=owner.MAX_BODY_BYTES+1)
    f.objects.read_verified=lambda *a:pytest.fail('oversized bytes loaded')
    with pytest.raises(ValueError):owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})


def test_quarterly_actual_api_wrapper_preserves_network_error_and_pure_ties(monkeypatch):
    from screener import api
    calls=[]
    monkeypatch.setattr(api._edgar,'submissions',lambda cik:calls.append(cik) or submissions())
    actual=api._latest_quarterly_filing('0000000001')
    assert actual==owner.latest_quarterly_from_submissions('0000000001',submissions())
    assert actual['url']=='https://www.sec.gov/Archives/edgar/data/1/000000000126000003/0000000001-26-000003-index.htm'
    assert api._latest_quarterly_filing('E12345') is None and calls==['0000000001']
    monkeypatch.setattr(api._edgar,'submissions',lambda *a:(_ for _ in ()).throw(RuntimeError('fixture error')))
    assert api._latest_quarterly_filing('1') is None


@pytest.mark.parametrize('form',['10-Q','10-Q/A','6-K','6-K/A'])
def test_quarterly_existing_forms_and_nullable_period_document(form):
    d=submissions();r=d['filings']['recent'];r.update(form=[form],filingDate=['2026-01-01'],reportDate=[''],accessionNumber=['one'],primaryDocument=[''])
    actual=owner.latest_quarterly_from_submissions('1',d)
    assert actual['form']==form and actual['document'] is None and actual['period'] is None


def test_legacy_quarterly_zip_and_no_candidates_unchanged():
    assert owner.latest_quarterly_from_submissions('1',{}) is None
    d=submissions();d['filings']['recent']['primaryDocument']=[]
    assert owner.latest_quarterly_from_submissions('1',d) is None



def test_forged_self_consistent_metadata_is_not_semantic_verification(fixture):
    # Association-only recovery trusts the immutable minted checkpoint. Standalone
    # forged bytes are not proof this metadata was computed by its declared build.
    f=fixture;ref,sha=retained(f);r=f.record.model_dump(mode='json');r['sources']=[ref.source.model_dump(mode='json')];r['submissions']=ref.model_dump(mode='json')
    a=json.loads(json.dumps(f.accounting));a['universe'][0]['artifact_sha256s'].append(sha)
    p=f.plan(plans.load(plans.InputRecord,plans.encode(r)),a);resolution=f.resolver.resolve(p)
    out=json.loads(f.objects.rows[resolution]);metadata=json.loads(f.objects.rows[out['inputs'][0]['metadata_sha256']])
    metadata['result']['incorporation']='FORGED';out['inputs'][0]['metadata_sha256']=f.put(metadata)
    recovered=f.resolver.verify(f.put(out),p)
    assert recovered.financial_status=='NOT_ASSESSED'
    assert metadata['semantic_verification']=='MINTED_CHECKPOINT_TRUST_ONLY'
    # A real completed durable job has the original immutable resolution hash;
    # replacing it is not a legitimate checkpoint replay.
    assert f.put(out)!=resolution


def test_metadata_capsule_replay_rejects_wrong_build_and_source(fixture):
    f=fixture;ref,sha=retained(f);r=f.record.model_dump(mode='json');r['sources']=[ref.source.model_dump(mode='json')];r['submissions']=ref.model_dump(mode='json')
    a=json.loads(json.dumps(f.accounting));a['universe'][0]['artifact_sha256s'].append(sha)
    p=f.plan(plans.load(plans.InputRecord,plans.encode(r)),a);resolution=f.resolver.resolve(p)
    out=json.loads(f.objects.rows[resolution]);original=json.loads(f.objects.rows[out['inputs'][0]['metadata_sha256']])
    for change in (lambda m:m.update(resolver_build_sha256='f'*64),lambda m:m['result']['source']['source']['delivery'].update(job_id=99)):
        m=json.loads(json.dumps(original));change(m);out['inputs'][0]['metadata_sha256']=f.put(m)
        with pytest.raises(ValueError,match='source|build|delivery'):f.resolver.verify(f.put(out),p)



def test_wrong_declared_filing_date_cannot_bind_captured_index(fixture):
    f=fixture;ref,_=retained(f);v=ref.model_dump(mode='json');v['filing_date']='2026-03-02'
    ref=owner.SubmissionsRef.model_validate_json(json.dumps(v))
    with pytest.raises(ValueError,match='filing/date'):owner.resolve_submissions(ref,'0000000001',artifacts=f.artifacts,objects=f.objects,observations=f.observations,payloads={})


@pytest.mark.parametrize('change',[
    lambda s:s.pop('fidelity'),lambda s:s.update(extra='ignored'),
    lambda s:s.update(source_item_id=12.0),lambda s:s.update(source_observation_id=True),
])
def test_original_captured_source_exact_fields_and_scalar_types(fixture,change):
    v=fixture.record.model_dump(mode='json');change(v['sources'][0]['captured_source'])
    with pytest.raises(ValueError):plans.load(plans.InputRecord,plans.encode(v))
