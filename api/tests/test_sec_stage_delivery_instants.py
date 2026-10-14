"""Original delivery instants at the public stage source-admission boundary."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta, timezone
from threading import Event
from types import SimpleNamespace
import pytest
from screener.job_runtime import JobContext
from screener.durable_jobs import LeaseToken
from screener.sec_ingestion import SecCandidateStageHandler
from screener.source_observations import SourceAcquisitionReceipt
from test_sec_resource_partial_acquisition import Fixture as PartialFixture
from test_sec_completed_capture import Fixture as ExistingFixture, Forbidden


class SourceAdmissionReached(Exception):pass


def stage(f, resource_job, checkpoint):
    request=f.handler.candidate_stage_request(resource_job,checkpoint)
    actual=replace(resource_job,job_id=507,occurrence_id=508,kind=request.kind,parameters=deepcopy(request.parameters),
        due_at=request.due_at,priority=request.priority,status='running',checkpoint=None,ownership_generation=1)
    engine=object()
    writer=SecCandidateStageHandler(edgar=Forbidden(),object_store=f,artifacts=f,observations=f,
        companies=SimpleNamespace(engine=engine),history=SimpleNamespace(engine=engine))
    def admitted(*args,**kwargs):raise SourceAdmissionReached('actual stage reached metadata boundary after full root admission')
    writer._filing_metadata=admitted
    writer._derive=lambda *a,**kw:pytest.fail('diagnostic entered financial derive')
    writer._fetch_leaf=lambda *a,**kw:pytest.fail('diagnostic entered leaf fetch')
    writer._store_pair=lambda *a,**kw:pytest.fail('diagnostic entered writer')
    return writer,JobContext(actual,Event(),Forbidden(),LeaseToken(507,'diagnostic',1),lambda:resource_job.due_at)


def partial():
    f=PartialFixture();f.job=replace(f.job,kind='sec-resource-fetch-v3')
    checkpoint=f.run()
    assert set(checkpoint['root_deliveries'])=={'complete_submission','accession_inventory','submissions','company_facts'}
    # Simulate how an aware instant is returned by a nonUTC PostgreSQL session, not a new check.
    records=[]
    for key,(request,stored,outcome) in list(f.receipts.items()):
        aware=stored.delivery.checked_at.astimezone(timezone(timedelta(hours=3)))
        stored=replace(stored,delivery=replace(stored.delivery,checked_at=aware))
        assert SourceAcquisitionReceipt.from_outcome(outcome).delivery==stored.delivery
        assert outcome['delivery']['checked_at'].endswith('+00:00')
        assert {k:v for k,v in outcome['delivery'].items() if k!='checked_at'}=={
            k:getattr(stored.delivery,k) for k in outcome['delivery'] if k!='checked_at'}
        f.receipts[key]=(request,stored,outcome);records.append(stored)
    def by_sha(digest):
        choices=[v for v in records if v.observation.observation_sha256==digest]
        assert len(choices)==1
        return replace(choices[0],created=False)
    def delivery(occurrence_id,**identity):
        choices=[v.delivery for v in records if v.delivery.occurrence_id==occurrence_id]
        assert len(choices)==1
        value=choices[0]
        assert (value.source_item_id,value.source_observation_id)==(identity['source_item_id'],identity['source_observation_id'])
        return value
    f.by_sha256=by_sha;f.delivery=delivery
    return f,checkpoint


def legacy():
    f=ExistingFixture()
    for key,value in list(f.deliveries.items()):
        f.deliveries[key]=replace(value,checked_at=value.checked_at.astimezone(timezone(timedelta(hours=3))))
    f.latest_sec_filing=f.sec_filing
    return f,deepcopy(f.job.checkpoint)


def test_partial_receipt_utc_admits_same_original_aware_instant_in_actual_stage():
    f,cp=partial();before=deepcopy(cp)
    writer,context=stage(f,f.job,cp)
    with pytest.raises(SourceAdmissionReached):writer(context)
    assert cp==before
    assert f.events.count('body')>=4


def test_original_same_spelling_neighbor_reaches_actual_stage_source_boundary():
    f,cp=legacy();before=deepcopy(cp)
    writer,context=stage(f,f.job,cp)
    with pytest.raises(SourceAdmissionReached):writer(context)
    assert cp==before and f.events.count('body')==4


def test_different_instant_refuses_actual_stage_before_financial_boundary():
    f,cp=legacy();one=cp['root_deliveries']['complete_submission'];
    from datetime import datetime
    one['checked_at']=(datetime.fromisoformat(one['checked_at'])+timedelta(seconds=1)).isoformat()
    writer,context=stage(f,f.job,cp)
    with pytest.raises(ValueError,match='captured delivery'):writer(context)


def test_raw_http_fidelity_is_not_relaxed_by_timezone_case():
    f,cp=legacy();f.row('complete_submission').observation.canonical_metadata['capture_fidelity']='DECODED_DECIMAL'
    writer,context=stage(f,f.job,cp)
    with pytest.raises(ValueError,match='raw HTTP'):writer(context)


def test_original_issuer_provenance_is_not_relaxed_by_timezone_case():
    f,cp=legacy();f.row('complete_submission').item.issuer_source_identifier='0000009999'
    writer,context=stage(f,f.job,cp)
    with pytest.raises(ValueError,match='root observation'):writer(context)
