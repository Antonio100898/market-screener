"""Real finite runtime/receipts/request recovery over labelled synthetic transport."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from time import monotonic, sleep
import subprocess
import sys
import threading

import pytest
from sqlalchemy import func, select, text, update

from screener import sec_inventory_plan as owner
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.job_runtime import DurableJobRuntime, JobContext
from screener.object_store import ImmutableObjectStore, create_s3_client
from screener.postgres import durable_job, source_item, source_check_occurrence, company_snapshot, active_data_release, data_release, current_company_snapshot
from screener.source_observations import SourceObservationRepository
from screener.storage_config import StorageSettings
from test_publication_storage import storage, NOW, LEASE, pytestmark
from test_source_delivery_storage import state
from test_sec_inventory_plan import RetainedTransport, parameters


def setup(storage, key="actual-finite", clock=lambda:NOW, transport=None):
    engine = storage[0]
    settings = StorageSettings.from_env()
    objects = ImmutableObjectStore(create_s3_client(settings), settings.s3_bucket)
    jobs, artifacts = DurableJobRepository(engine), SqlArtifactRepository(engine)
    sources = SourceObservationRepository(engine,clock=clock)
    handler = owner.SecFiniteInventoryHandler(edgar=transport or RetainedTransport(), object_store=objects,
        artifacts=artifacts, observations=sources, jobs=jobs, clock=clock)
    params = parameters(key)
    scheduled = datetime.fromisoformat(params["operation_at"])
    job = jobs.enqueue_scheduled(schedule_key=owner.NAMESPACE+key,scheduled_for=scheduled,
        kind=owner.KIND,parameters=params,due_at=NOW,priority=24)
    claimed = jobs.claim_next(owner="actual-finite-worker",lease_duration=LEASE,now=clock,
        allowed_kinds={owner.KIND})
    assert claimed and claimed.job.job_id==job.job_id
    context = JobContext(claimed.job,threading.Event(),jobs,claimed.lease,clock)
    return handler,jobs,sources,context,objects


def complete(handler,jobs,context):
    final = handler(context)
    job = jobs.complete(context.lease,final_checkpoint=final,now=context._clock)
    return handler.recover_completed(job),job


def test_actual_runtime_finishes_finite_request_cohort_and_no_financial_activation(storage,monkeypatch):
    handler,jobs,sources,context,objects = setup(storage)
    with storage[0].connect() as connection:
        active_before = [dict(row) for row in connection.execute(select(active_data_release)).mappings()]
    assert active_before == [dict(singleton_id=1, release_id=None)]
    runtime = DurableJobRuntime(jobs,lambda now:(),{owner.KIND:handler},worker_count=1,clock=lambda:NOW)
    from screener.durable_jobs import ClaimedJob
    try:
        runtime._run_claimed(ClaimedJob(context.job,context.lease))
        assert runtime.errors==()
        job = jobs.scheduled_job(job_id=context.job.job_id,occurrence_id=context.job.occurrence_id,
            schedule_key=owner.NAMESPACE+context.job.parameters["operation_key"],
            scheduled_for=datetime.fromisoformat(context.job.parameters["operation_at"]))
        root = handler.recover_completed(job)
        assert job.status=="succeeded" and root.resource_request_count==2 and root.index_row_count==4
        assert root.ticker_entry_count==6 and root.issuer_group_count==3
        assert root.days[1].acquisition.source.state=="unavailable"
        with storage[0].connect() as connection:
            assert connection.scalar(select(func.count()).select_from(company_snapshot))==0
            assert [dict(row) for row in connection.execute(select(active_data_release)).mappings()] == active_before
            assert connection.scalar(select(active_data_release.c.release_id).where(active_data_release.c.singleton_id==1)) is None
            assert connection.scalar(select(func.count()).select_from(data_release))==0
            assert connection.scalar(select(func.count()).select_from(current_company_snapshot))==0
            children=connection.execute(select(durable_job).where(durable_job.c.kind=="sec-resource-fetch-v2")).mappings().all()
        assert len(children)==2 and all(row["status"]=="pending" for row in children)
        before=state(storage[0])
        def forbidden(*a,**kw):pytest.fail("completed recovery performed live acquisition/enqueue")
        monkeypatch.setattr(handler.edgar,"fetch",forbidden)
        monkeypatch.setattr(sources,"record_with_acquisition_receipt",forbidden)
        monkeypatch.setattr(jobs,"enqueue_child",forbidden)
        assert handler.recover_completed(job)==root and state(storage[0])==before
    finally:
        objects.client.close()


@pytest.mark.parametrize("boundary",["ticker-receipt","filing-receipt","child-commit","day-checkpoint","root-store"])
def test_actual_interrupted_fanout_reclaim_recovers_original_delivery_and_child(storage,monkeypatch,boundary):
    handler,jobs,sources,context,objects=setup(storage,key="actual-reclaim")
    fired=[False]
    if boundary in {"ticker-receipt","filing-receipt"}:
        original=sources.record_with_acquisition_receipt
        def interrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            trigger=kwargs["receipt_key"]=="sec-finite:ticker-map" if boundary=="ticker-receipt" else kwargs["receipt_key"].startswith("sec-finite:filing:")
            if trigger and not fired[0]:fired[0]=True;raise RuntimeError("synthetic interruption after source commit")
            return result
        monkeypatch.setattr(sources,"record_with_acquisition_receipt",interrupt)
    elif boundary=="child-commit":
        original=jobs.enqueue_child
        def interrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            if not fired[0]:fired[0]=True;raise RuntimeError("synthetic interruption after child commit")
            return result
        monkeypatch.setattr(jobs,"enqueue_child",interrupt)
    elif boundary=="day-checkpoint":
        original=jobs.save_checkpoint
        def interrupt(*args,**kwargs):
            result=original(*args,**kwargs)
            if args[1].get("days") and not fired[0]:fired[0]=True;raise RuntimeError("synthetic interruption after day checkpoint")
            return result
        monkeypatch.setattr(jobs,"save_checkpoint",interrupt)
    else:
        original=objects.put_verified
        def interrupt(data,media_type):
            result=original(data,media_type)
            if b'SEC_INVENTORY_ENUMERATION1' in data and not fired[0]:
                fired[0]=True;raise RuntimeError("synthetic interruption after root upload")
            return result
        monkeypatch.setattr(objects,"put_verified",interrupt)
    try:
        with pytest.raises(RuntimeError,match="synthetic interruption"):
            handler(context)
        assert fired[0]
        original_deliveries={}
        digest=handler._actual_request(context.job)[2]
        for key in ("sec-finite:ticker-map","sec-finite:day:2026-09-28","sec-finite:filing:2026-09-28:0"):
            captured=sources.acquisition_receipt(producer_job_id=context.job.job_id,receipt_key=key,request_identity_sha256=digest)
            if captured is not None:original_deliveries[key]=captured.delivery
        later=NOW+timedelta(minutes=6)
        expired=jobs.recover_expired(now=later)
        assert any(row.job_id==context.job.job_id for row in expired)
        retry=next(row.next_retry_at for row in expired if row.job_id==context.job.job_id)
        reclaimed=jobs.claim_next(owner="actual-reclaimed",lease_duration=LEASE,now=retry,allowed_kinds={owner.KIND})
        assert reclaimed.lease.generation==context.lease.generation+1
        handler.clock=lambda:retry
        handler.discovery._clock=lambda:retry
        sources._clock=lambda:retry
        resumed=JobContext(reclaimed.job,threading.Event(),jobs,reclaimed.lease,lambda:retry)
        root,job=complete(handler,jobs,resumed)
        assert root.resource_request_count==2
        for key,delivery in original_deliveries.items():
            assert sources.acquisition_receipt(producer_job_id=job.job_id,receipt_key=key,
                request_identity_sha256=digest).delivery==delivery
        assert handler.edgar.calls.count(("https://www.sec.gov/files/company_tickers.json",owner.MAX_BODY_BYTES))==1
        with storage[0].connect() as connection:
            assert connection.scalar(select(func.count()).select_from(durable_job).where(durable_job.c.kind=="sec-resource-fetch-v2"))==2
    finally:
        objects.client.close()


def test_actual_completed_fresh_process_recovery_refuses_fetch_record_enqueue_and_build(storage,tmp_path):
    handler,jobs,sources,context,objects=setup(storage,key="actual-fresh-child")
    try:
        root,job=complete(handler,jobs,context)
        script=tmp_path/"readonly-finite-recovery.py"
        script.write_text("""import json,sys
from datetime import datetime
from types import SimpleNamespace
from sqlalchemy import create_engine
from screener import sec_inventory_plan as owner,sync
from screener.artifacts import SqlArtifactRepository
from screener.durable_jobs import DurableJobRepository
from screener.object_store import ImmutableObjectStore,create_s3_client
from screener.source_observations import SourceObservationRepository
from screener.storage_config import StorageSettings
def forbidden(*args,**kwargs):raise AssertionError('completed finite recovery used mutable/source/build path')
settings=StorageSettings.from_env();engine=create_engine(settings.database_url);client=create_s3_client(settings)
jobs=DurableJobRepository(engine);sources=SourceObservationRepository(engine)
job=jobs.scheduled_job(job_id=int(sys.argv[1]),occurrence_id=int(sys.argv[2]),schedule_key=sys.argv[3],scheduled_for=datetime.fromisoformat(sys.argv[4]))
jobs.enqueue_child=forbidden;sources.record_with_acquisition_receipt=forbidden;sources.latest_item=forbidden;sources.latest_sec_filing=forbidden;sync._derive_evidence_result=forbidden
handler=owner.SecFiniteInventoryHandler(edgar=SimpleNamespace(fetch=forbidden),object_store=ImmutableObjectStore(client,settings.s3_bucket),artifacts=SqlArtifactRepository(engine),observations=sources,jobs=jobs)
try:print(json.dumps(handler.recover_completed(job).model_dump(mode='json'),sort_keys=True))
finally:client.close();engine.dispose()
""")
        result=subprocess.run([sys.executable,str(script),str(job.job_id),str(job.occurrence_id),
            owner.NAMESPACE+job.parameters["operation_key"],job.parameters["operation_at"]],
            cwd=os.getcwd(),env=os.environ.copy(),capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stderr
        assert owner.load(owner.InventoryRoot,result.stdout.encode())==root
    finally:
        objects.client.close()


def test_actual_source_lock_wait_expiry_rolls_back_uncommitted_finite_acquisition(storage):
    handler,jobs,sources,context,objects=setup(storage,key="actual-seed")
    try:
        first,job=complete(handler,jobs,context)
        clock=[NOW]
        second,second_jobs,second_sources,next_context,next_objects=setup(storage,key="actual-expiring",clock=lambda:clock[0])
        try:
            before=state(storage[0])
            with storage[0].connect() as blocker:
                transaction=blocker.begin()
                blocker.execute(select(source_item.c.source_item_id).where(
                    source_item.c.source_item_id==first.listing.source.delivery.source_item_id).with_for_update()).scalar_one()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(second,next_context)
                    try:
                        deadline=monotonic()+5
                        while True:
                            with storage[0].connect() as observer:
                                blocked=observer.scalar(text("SELECT EXISTS(SELECT1 FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock' AND query LIKE '%source_item%' AND query LIKE '%FOR UPDATE%')".replace("SELECT1","SELECT 1")))
                            if blocked:break
                            assert monotonic()<deadline and not future.done()
                            sleep(.01)
                        clock[0]=NOW+timedelta(minutes=6)
                    finally:transaction.rollback()
                    with pytest.raises(LeaseLost):future.result(timeout=10)
            assert state(storage[0])==before
            digest=second._actual_request(next_context.job)[2]
            assert second_sources.acquisition_receipt(producer_job_id=next_context.job.job_id,
                receipt_key="sec-finite:ticker-map",request_identity_sha256=digest) is None
        finally:next_objects.client.close()
    finally:objects.client.close()


def test_actual_equal_body_media_header_change_and_later_occurrence_are_exact(storage):
    first,jobs,sources,context,objects=setup(storage,key="actual-original-header")
    try:
        root,job=complete(first,jobs,context)
        transport=RetainedTransport()
        transport.override_media_type=True
        transport.media_type=None
        second,new_jobs,new_sources,next_context,next_objects=setup(storage,key="actual-missing-header",transport=transport)
        try:
            later,later_job=complete(second,new_jobs,next_context)
            assert later.listing.media_type is None and later.listing.headers_captured is True
            assert root.listing.source.artifact_sha256==later.listing.source.artifact_sha256
            assert root.listing.source.observation_sha256!=later.listing.source.observation_sha256
            assert root.listing.source.delivery.occurrence_id!=later.listing.source.delivery.occurrence_id
            assert first.recover_completed(job)==root and second.recover_completed(later_job)==later
        finally:next_objects.client.close()
    finally:objects.client.close()
