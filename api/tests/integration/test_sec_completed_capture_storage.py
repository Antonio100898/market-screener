"""Prepared real UUID SQL/S3 completed-source recovery; synthetic SEC transport."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
import subprocess
import sys

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from screener import postgres
from screener.artifacts import store_evidence
from screener.source_observations import SourceItemIdentity
from test_sec_exact_annotation_storage import setup_flow, revise_parent
from test_sec_ingestion_storage import storage, database_url, NOW, LEASE, _context, pytestmark


def identity(flow, key):
    return dict(job_id=flow.root_claim.job.job_id, occurrence_id=flow.root_claim.job.occurrence_id,
                schedule_key="resource:"+key, scheduled_for=NOW)


def rows(engine):
    with engine.connect() as connection:
        return {table.name:sorted(json.dumps(dict(row), sort_keys=True, default=str)
            for row in connection.execute(select(table)).mappings()) for table in postgres.metadata.sorted_tables}


def readonly(flow, monkeypatch):
    for owner, names in ((flow.resource._edgar, ("fetch",)), (flow.jobs, ("enqueue_child", "save_checkpoint")),
        (flow.source, ("latest_sec_filing", "latest_item", "record"))):
        for name in names:
            monkeypatch.setattr(owner, name, lambda *a,**kw:pytest.fail("readonly recovery wrote or selected current"))


@pytest.mark.parametrize("kind", ["sec-resource-fetch", "sec-resource-fetch-v2"])
def test_actual_capture2_both_kinds_original_deliveries_and_no_table_change(storage, monkeypatch, kind):
    key="capture-positive"
    flow=setup_flow(storage,key)
    original=identity(flow,key)
    if kind.endswith("v2"):
        new=flow.jobs.enqueue_scheduled(schedule_key="resource:actual-v2", scheduled_for=NOW,
            kind=kind, parameters=flow.root_claim.job.parameters, due_at=NOW)
        claim=flow.jobs.claim_next(owner="v2-resource",lease_duration=LEASE,now=NOW,allowed_kinds={kind})
        assert claim.job.job_id==new.job_id
        checkpoint=flow.resource(_context(claim,flow.jobs))
        flow.jobs.complete(claim.lease,final_checkpoint=checkpoint,now=NOW)
        original=dict(job_id=new.job_id,occurrence_id=new.occurrence_id,
                      schedule_key="resource:actual-v2",scheduled_for=NOW)
    before=rows(flow.engine)
    readonly(flow,monkeypatch)
    result=flow.resource.recover_completed_capture(**original)
    assert result["status"]=="VERIFIED" and result["job"]["kind"]==kind
    assert all(ref["delivery"]["job_id"]==original["job_id"] for ref in result["roots"])
    result["checkpoint"]["roots"]["company_facts"]="f"*64
    assert flow.resource.recover_completed_capture(**original)["status"]=="VERIFIED"
    assert rows(flow.engine)==before


@pytest.mark.parametrize("state", ["changed", "removed"])
def test_actual_historical_parent_recovers_after_current_change(storage,monkeypatch,state):
    key="capture-parent-"+state
    flow=setup_flow(storage,key)
    revise_parent(flow,state)
    before=rows(flow.engine)
    readonly(flow,monkeypatch)
    result=flow.resource.recover_completed_capture(**identity(flow,key))
    assert result["status"]=="VERIFIED"
    assert result["filing"]["source_observation_id"]==flow.parent.observation.source_observation_id
    assert rows(flow.engine)==before


@pytest.mark.parametrize("contradiction", ["foreign_job", "parent_revision"])
def test_actual_valid_borrowed_delivery_or_parent_revision_refuses(storage,monkeypatch,contradiction):
    key="capture-wrong-"+contradiction
    flow=setup_flow(storage,key,complete_root=False)
    checkpoint=deepcopy(flow.root_checkpoint)
    role="accession_inventory"
    stored=flow.source.by_sha256(checkpoint["roots"][role])
    metadata=dict(stored.observation.canonical_metadata)
    lease=flow.candidate.lease
    if contradiction=="parent_revision":
        revised=revise_parent(flow,"changed")
        metadata.update(parent_observation_id=revised.observation.source_observation_id,
                        parent_observation_sha256=revised.observation.observation_sha256)
        lease=flow.root_claim.lease
    retained=flow.source.record(lease,item=SourceItemIdentity("SEC",stored.item.item_kind,
        stored.item.source_key,stored.item.issuer_source_identifier,stored.item.parent_source_item_id),
        item_outcome_key="root:"+role,state="present",metadata=metadata,
        source_url=stored.observation.source_url,artifact_sha256=stored.observation.artifact_sha256,
        detected_at=NOW)
    d=retained.delivery
    checkpoint["roots"][role]=retained.observation.observation_sha256
    checkpoint["root_deliveries"][role]=dict(occurrence_id=d.occurrence_id,source_item_id=d.source_item_id,
        source_observation_id=d.source_observation_id,job_id=d.job_id,ownership_generation=d.ownership_generation,
        item_key=d.item_key,checked_at=d.checked_at.isoformat())
    flow.jobs.complete(flow.root_claim.lease,final_checkpoint=checkpoint,now=NOW)
    before=rows(flow.engine)
    readonly(flow,monkeypatch)
    with pytest.raises(ValueError):flow.resource.recover_completed_capture(**identity(flow,key))
    assert rows(flow.engine)==before


def test_actual_reclaim_keeps_original_generation_and_offset_stage_key(storage,monkeypatch):
    key="capture-reclaim"
    flow=setup_flow(storage,key,complete_root=False)
    checkpoint=deepcopy(flow.root_checkpoint)
    request=flow.resource.candidate_stage_request(flow.root_claim.job,checkpoint)
    expired=flow.jobs.recover_expired(now=NOW+timedelta(minutes=6))
    retry=next(job.next_retry_at for job in expired if job.job_id==flow.root_claim.job.job_id)
    claim=flow.jobs.claim_next(owner="reclaimed",lease_duration=LEASE,now=retry,
                              allowed_kinds={flow.root_claim.job.kind})
    flow.jobs.complete(claim.lease,final_checkpoint=checkpoint,now=retry)
    before=rows(flow.engine)
    readonly(flow,monkeypatch)
    result=flow.resource.recover_completed_capture(**identity(flow,key))
    assert result["status"]=="VERIFIED" and result["job"]["ownership_generation"]>1
    assert all(ref["delivery"]["ownership_generation"]==1 for ref in result["roots"])
    assert result["checkpoint"]==checkpoint
    assert flow.resource.candidate_stage_request(claim.job,result["checkpoint"])==request
    assert rows(flow.engine)==before


@pytest.mark.parametrize("gap", ["capacity", "missing_body"])
def test_actual_capacity_or_missing_body_no_recovery_writes(storage,monkeypatch,gap):
    key="capture-body-"+gap
    flow=setup_flow(storage,key,complete_root=False)
    checkpoint=deepcopy(flow.root_checkpoint)
    role="company_facts"
    stored=flow.source.by_sha256(checkpoint["roots"][role])
    if gap=="capacity":
        body=flow.raw+b" "*(32*1024*1024+1-len(flow.raw))
        artifact=store_evidence(body,"application/json",flow.objects,flow.artifacts)
        retained=flow.source.record(flow.root_claim.lease,item=SourceItemIdentity("SEC",stored.item.item_kind,
            stored.item.source_key,stored.item.issuer_source_identifier,stored.item.parent_source_item_id),
            item_outcome_key="root:"+role,state="present",metadata=stored.observation.canonical_metadata,
            source_url=stored.observation.source_url,artifact_sha256=artifact.content_sha256,detected_at=NOW)
        checkpoint["roots"][role]=retained.observation.observation_sha256
        d=retained.delivery
        checkpoint["root_deliveries"][role]=dict(occurrence_id=d.occurrence_id,source_item_id=d.source_item_id,
            source_observation_id=d.source_observation_id,job_id=d.job_id,ownership_generation=d.ownership_generation,
            item_key=d.item_key,checked_at=d.checked_at.isoformat())
    else:
        artifact=flow.artifacts.get(stored.observation.artifact_sha256)
        flow.objects.client.delete_object(Bucket=flow.objects.bucket,Key=artifact.object_key)
    flow.jobs.complete(flow.root_claim.lease,final_checkpoint=checkpoint,now=NOW)
    before=rows(flow.engine)
    readonly(flow,monkeypatch)
    if gap=="capacity":
        monkeypatch.setattr(flow.objects,"read_verified",lambda *a:pytest.fail("capacity read body"))
        result=flow.resource.recover_completed_capture(**identity(flow,key))
        assert result["status"]=="CAPACITY_UNAVAILABLE" and result["gap"]["required_bytes"]==32*1024*1024+1
    else:
        with pytest.raises(ClientError):flow.resource.recover_completed_capture(**identity(flow,key))
    assert rows(flow.engine)==before


def test_fresh_process_scalar_recovery_has_same_references_and_no_writes(storage):
    key="capture-process"
    flow=setup_flow(storage,key)
    expected=flow.resource.recover_completed_capture(**identity(flow,key))
    before=rows(flow.engine)
    args={**identity(flow,key),"scheduled_for":NOW.isoformat()}
    code="""import json,sys
from datetime import datetime
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.artifacts import SqlArtifactRepository
from screener.object_store import ImmutableObjectStore,create_s3_client
from screener.source_observations import SourceObservationRepository
from screener.durable_jobs import DurableJobRepository
from screener.sec_ingestion import SecResourceFetchHandler
s=StorageSettings.from_env(); e=create_engine(s.database_url)
h=SecResourceFetchHandler(edgar=None,object_store=ImmutableObjectStore(create_s3_client(s),s.s3_bucket),artifacts=SqlArtifactRepository(e),observations=SourceObservationRepository(e),jobs=DurableJobRepository(e))
i=json.loads(sys.argv[1]);i['scheduled_for']=datetime.fromisoformat(i['scheduled_for'])
print(json.dumps(h.recover_completed_capture(**i),sort_keys=True));e.dispose()
"""
    child=subprocess.run([sys.executable,"-c",code,json.dumps(args)],env={**os.environ,"PYTHONDONTWRITEBYTECODE":"1"},
                         capture_output=True,text=True,timeout=30)
    assert child.returncode==0,child.stderr
    assert json.loads(child.stdout)==expected
    assert rows(flow.engine)==before
