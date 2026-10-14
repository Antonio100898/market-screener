"""Prepared UUID inspection composition over labelled synthetic SEC transport."""
from copy import deepcopy
from datetime import datetime
import json
import os
import subprocess
import sys

import pytest

from screener import sec_inventory_plan as owner
from screener.sec_ingestion import SecResourceFetchHandler, _root_resources
from test_sec_inventory_plan_storage import setup, complete
from test_sec_completed_capture_storage import rows
from test_sec_completed_capture import Fixture
from test_sec_ingestion_storage import database_url, storage
from test_publication_storage import NOW, LEASE
from screener.job_runtime import JobContext
from threading import Event


pytestmark=pytest.mark.skipif(os.getenv('RUN_STORAGE_INTEGRATION')!='1',reason='root-exclusive UUID SQL/S3 required')


def args(job):
    return dict(job_id=job.job_id,occurrence_id=job.occurrence_id,
        schedule_key=owner.NAMESPACE+job.parameters['operation_key'],
        scheduled_for=datetime.fromisoformat(job.parameters['operation_at']),day='2026-09-28',entry_ordinal=0)


def readonly(handler,jobs,sources,monkeypatch):
    for obj,names in ((handler.edgar,('fetch',)),(jobs,('enqueue_child','save_checkpoint')),
            (sources,('record','record_with_acquisition_receipt','latest_sec_filing','latest_item'))):
        for name in names:monkeypatch.setattr(obj,name,lambda *a,**kw:pytest.fail('inspection wrote/fetched/selectedcurrent'))


def test_actual_pending_orphan_nonroute_inspection_changes_no_tables(storage,monkeypatch):
    handler,jobs,sources,context,objects=setup(storage,key='inspect-pending')
    try:
        root,job=complete(handler,jobs,context);before=rows(storage[0]);readonly(handler,jobs,sources,monkeypatch)
        result=handler.inspect_completed_entry(**args(job))
        assert result['capture']['status']=='NOT_COMPLETED' and result['stage']['status']=='NOT_INSPECTED_CAPTURE_NOT_VERIFIED'
        assert result['entry']['physical_line']==4
        orphan=handler.inspect_completed_entry(**{**args(job),'entry_ordinal':3})
        assert orphan['issuer_group_ordinal'] is None and orphan['capture']['status']=='NOT_COMPLETED'
        nonroute=handler.inspect_completed_entry(**{**args(job),'entry_ordinal':1})
        assert nonroute['capture'] is None and nonroute['stage']['status']=='NOT_APPLICABLE_TO_RETAINED_ROW'
        assert rows(storage[0])==before
    finally:objects.client.close()


def test_actual_verified_capture_exact_stage_association_no_tables_change(storage,monkeypatch):
    handler,jobs,sources,context,objects=setup(storage,key='inspect-verified')
    try:
        root,job=complete(handler,jobs,context)
        child=handler._read_chunks(root.days[0].chunks)[0].request.child
        claim=jobs.claim_next(owner='inspection-resource',lease_duration=LEASE,now=NOW,allowed_kinds={'sec-resource-fetch-v2'})
        assert claim and claim.job.job_id==child.job_id
        raw=Fixture().raw
        resources=_root_resources(accession=claim.job.parameters['accession'],cik=claim.job.parameters['cik'],filename=claim.job.parameters['filename'])
        handler.edgar.bodies.update({r.url:raw[r.role] for r in resources})
        resource=SecResourceFetchHandler(edgar=handler.edgar,object_store=objects,artifacts=handler.artifacts,
            observations=sources,jobs=jobs,clock=lambda:NOW,annotate_annual=True)
        checkpoint=resource(JobContext(claim.job,Event(),jobs,claim.lease,lambda:NOW))
        jobs.complete(claim.lease,final_checkpoint=checkpoint,now=NOW)
        before=rows(storage[0]);readonly(handler,jobs,sources,monkeypatch)
        result=handler.inspect_completed_entry(**args(job))
        assert result['capture']['status']=='VERIFIED' and result['stage']['status']=='MATCHED_ASSOCIATION'
        assert result['stage']['job_status']=='pending' and result['financial_status']=='NOT_ASSESSED'
        assert rows(storage[0])==before
    finally:objects.client.close()


def test_fresh_process_pending_row_result_matches_and_no_mutation(storage,monkeypatch):
    handler,jobs,sources,context,objects=setup(storage,key='inspect-restart')
    try:
        root,job=complete(handler,jobs,context);before=rows(storage[0])
        request=args(job);expected=handler.inspect_completed_entry(**request)
        request['scheduled_for']=request['scheduled_for'].isoformat()
        script="""
import json,sys
from datetime import datetime
from screener import sec_inventory_plan as owner
from screener.storage_config import StorageSettings
from screener.postgres import create_postgres_engine
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository
from screener.source_observations import SourceObservationRepository
from screener.object_store import ImmutableObjectStore,create_s3_client
class NoFetch:
 def fetch(self,*a,**kw):raise AssertionError('inspection fetched')
settings=StorageSettings.from_env();engine=create_postgres_engine(settings)
objects=ImmutableObjectStore(create_s3_client(settings),settings.s3_bucket)
try:
 request=json.loads(sys.argv[1]);request['scheduled_for']=datetime.fromisoformat(request['scheduled_for'])
 handler=owner.SecFiniteInventoryHandler(edgar=NoFetch(),object_store=objects,artifacts=SqlArtifactRepository(engine),observations=SourceObservationRepository(engine),jobs=DurableJobRepository(engine))
 print(json.dumps(handler.inspect_completed_entry(**request),sort_keys=True))
finally:objects.client.close();engine.dispose()
"""
        process=subprocess.run([sys.executable,'-c',script,json.dumps(request)],capture_output=True,text=True,check=True,timeout=30)
        assert json.loads(process.stdout)==expected and rows(storage[0])==before
    finally:objects.client.close()
