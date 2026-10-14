"""Prepared real registered worker phases; synthetic sources, no manual checkpoint/pair writes."""
from datetime import datetime, timedelta, timezone
import json
from threading import Event

from sqlalchemy import select

from screener import automatic_ingestion as automatic, sec_inventory_plan as inventory
from screener import sync
from screener.durable_jobs import DurableJobRepository, RetryPolicy
from screener.postgres import durable_job, durable_job_item, evidence_artifact
from screener.sec_ingestion import SecDiscoveryStopped
from screener.shared_companies import SharedCompanyRepository
from screener.figure_history import FigureHistoryRepository
from screener.sources.edgar import EdgarTransportResult, TICKER_MAP_URL
from screener.storage_config import AutomaticIngestionSettings
from test_sec_ingestion_storage import storage, database_url, pytestmark, _index, _row, _url, RESOURCE_ACCESSION
from test_automatic_ingestion_storage import fixture_responses
from test_sec_partial_registered_caller_storage import BoundedTransport


def test_actual_registered_partial_prefix_points_and_genuine_first_day_pair(storage,tmp_path,monkeypatch):
    engine,objects=storage
    instant=datetime.now(timezone.utc)
    operation="actual-recorded-entry"
    key="automatic-ingestion:v1:sec-finite-inventory-v2:"+operation
    params=dict(request_revision=2,operation_key=operation,operation_at=instant.isoformat(),start_date="2026-09-28",
        end_date="2026-09-29",expected_parent_release_id=None,producer_build_sha256=inventory.producer_build_sha256())
    path=tmp_path/"recorded-plan.json"
    path.write_text(json.dumps(dict(plan_revision=1,occurrences=[dict(kind=inventory.PARTIAL_KIND,parameters=params,
        schedule_key=key,scheduled_for=instant.isoformat(),due_at=instant.isoformat())])))
    settings=AutomaticIngestionSettings(True,path,tmp_path/"dedicated-cache")
    responses=fixture_responses()
    responses[TICKER_MAP_URL]=EdgarTransportResult(b'{"0":{"cik_str":1234,"ticker":"FIX","title":"Fixture"}}',
        TICKER_MAP_URL,"application/json","synthetic-listing",None)
    second_accession="0000001234-26-000002"
    second_url=_url(instant.date().replace(month=9,day=29))
    responses[second_url]=EdgarTransportResult(_index(_row("10-K","FIXTURE CORP","1234","2026-09-29",second_accession)),
        second_url,"text/plain","synthetic-second-day",None)
    edgar=BoundedTransport(responses)
    policy=RetryPolicy(base_delay=timedelta(seconds=60),max_delay=timedelta(seconds=60),jitter_seconds=lambda cap:cap)
    monkeypatch.setattr(automatic,"DurableJobRepository",lambda actual_engine:DurableJobRepository(actual_engine,retry_policy=policy))
    initial,initial_release,next_child,next_release,paired,pair_release=(Event() for _ in range(6))
    producer_id=[];second=[];stages=[]
    save=DurableJobRepository.save_checkpoint
    enqueue=DurableJobRepository.enqueue_child
    complete=DurableJobRepository.complete
    def initial_checkpoint(self,lease,checkpoint,**kwargs):
        if checkpoint.get("checkpoint_revision")==2 and checkpoint.get("days")==[] and not producer_id:
            producer_id.append(lease.job_id);initial.set()
            assert initial_release.wait(15),"release initial checkpoint barrier"
        return save(self,lease,checkpoint,**kwargs)
    def second_child(self,lease,**values):
        result=enqueue(self,lease,**values)
        if values["kind"]=="sec-resource-fetch-v3" and values["parameters"]["accession"]==second_accession and not second:
            second.append((values,result));next_child.set()
            assert next_release.wait(15),"release committed second-day child barrier"
            raise SecDiscoveryStopped("synthetic pause after committed second-day child; real fail/retry")
        return result
    def stage_complete(self,lease,**values):
        result=complete(self,lease,**values)
        if result.kind=="sec-candidate-stage" and result.parameters["accession"]==RESOURCE_ACCESSION and not stages:
            stages.append(result);paired.set()
            assert pair_release.wait(15),"release committed first-day stage barrier"
        return result
    monkeypatch.setattr(DurableJobRepository,"save_checkpoint",initial_checkpoint)
    monkeypatch.setattr(DurableJobRepository,"enqueue_child",second_child)
    monkeypatch.setattr(DurableJobRepository,"complete",stage_complete)
    args=dict(schedule_key=key,scheduled_for=instant,day="2026-09-28",entry_ordinal=0)
    def exact_producer(assembly):
        handlers=assembly.runtime._handlers
        assert handlers[inventory.KIND].stage_references is handlers[inventory.PARTIAL_KIND].stage_references is handlers["sec-candidate-stage"]
        jobs=assembly.runtime._repository
        with engine.connect() as connection:
            row=connection.execute(select(durable_job).where(durable_job.c.job_id==producer_id[0])).mappings().one()
        return handlers[inventory.PARTIAL_KIND],dict(job_id=row["job_id"],occurrence_id=row["occurrence_id"],**args),row
    def inspect_now(handler,identity):
        def denied(*args,**kwargs):raise AssertionError("point inspection invoked live/full/writer owner")
        with monkeypatch.context() as controls:
            for target,names in ((edgar,("fetch",)),(handler.jobs,("enqueue_child","enqueue_scheduled","save_checkpoint")),
                (handler.observations,("latest_sec_filing","latest_item","record","record_with_acquisition_receipt")),
                (SharedCompanyRepository,("snapshot","current","store_candidate","store_candidate_on_connection")),
                (FigureHistoryRepository,("load","load_on_connection","publication_identity_on_connection","store_on_connection")),
                (sync,("build_snapshot","_derive_evidence_result"))):
                for name in names:controls.setattr(target,name,denied)
            return handler.inspect_recorded_entry(**identity)
    assembly=automatic.AutomaticIngestion(settings,engine=engine,s3=objects.client,edgar=edgar)
    try:
        assembly.start();assert initial.wait(15)
        handler,identity,row=exact_producer(assembly)
        assert row["checkpoint"] is None
        with engine.connect() as connection:
            assert connection.scalar(select(durable_job_item.c.outcome).where(durable_job_item.c.job_id==row["job_id"],
                durable_job_item.c.item_key=="source-acquisition-receipt:sec-finite:ticker-map"))
            assert connection.scalar(select(evidence_artifact.c.content_sha256).limit(1))
        assert inspect_now(handler,identity)["status"]=="NO_RECORDED_PROGRESS"
        initial_release.set();assert next_child.wait(15)
        handler,identity,row=exact_producer(assembly)
        assert len(row["checkpoint"]["days"])==1 and row["checkpoint"]["root"] is None
        assert assembly.stop(timeout=.05) is False
    finally:
        initial_release.set();next_release.set();assert assembly.stop()
    handler,identity,row=exact_producer(assembly)
    assert row["status"]=="retry_wait" and "synthetic pause" in row["error_summary"]
    assert any("synthetic pause" in error for error in assembly.runtime.errors)
    assert inspect_now(handler,{**identity,"day":"2026-09-29"})["status"]=="NOT_CHECKPOINTED"
    values,second_job=second[0]
    second_exact=DurableJobRepository(engine).scheduled_job(job_id=second_job.job_id,occurrence_id=second_job.occurrence_id,
        schedule_key=values["child_key"],scheduled_for=values["scheduled_for"])
    assert second_exact.parameters["accession"]==second_accession and second_exact.status=="pending"
    saved_prefix=row["checkpoint"]
    resumed=automatic.AutomaticIngestion(settings,engine=engine,s3=objects.client,edgar=edgar)
    def timeout_diagnostics(phase):
        try:
            errors=resumed.runtime.errors
            with engine.connect() as connection:
                rows=connection.execute(select(durable_job.c.job_id,durable_job.c.occurrence_id,
                    durable_job.c.kind,durable_job.c.status,durable_job.c.ownership_generation,
                    durable_job.c.attempts,durable_job.c.due_at,durable_job.c.next_retry_at,
                    durable_job.c.checkpoint.is_not(None).label("checkpoint_present"),durable_job.c.error_summary)
                    .where(durable_job.c.kind.in_((inventory.PARTIAL_KIND,"sec-resource-fetch-v3","sec-candidate-stage")))
                    .order_by(durable_job.c.job_id).limit(17)).mappings().all()
            snapshot=dict(phase=phase,captured_at=datetime.now(timezone.utc).isoformat(),
                retained_runtime_errors=errors,job_scope="expected producer/resource/stage kinds; separate observations")
            if len(rows)>16:
                snapshot.update(jobs_status="CAPACITY_REFUSED",jobs=None)
            else:
                snapshot.update(jobs_status="PROJECTED",jobs=[{name:value.isoformat() if isinstance(value,datetime) else value
                    for name,value in row.items()} for row in rows])
            encoded=json.dumps(snapshot,ensure_ascii=False,allow_nan=False,separators=(",",":"))
            if len(encoded.encode("utf-8"))>2*1024*1024:
                print("recorded-entry timeout diagnostic:",json.dumps(dict(phase=phase,status="CAPACITY_REFUSED")),flush=True)
            else:
                print("recorded-entry timeout diagnostic:",encoded,flush=True)
        except Exception as error:
            message=str(error)
            detail=message if len(message)<=2000 else "diagnostic error message exceeds2000 characters"
            print("recorded-entry timeout diagnostic error:",phase,type(error).__name__,detail,flush=True)
    timed_out=False
    try:
        resumed.start();reached=paired.wait(15);timed_out=not reached
        if timed_out:timeout_diagnostics("before_drain")
        assert reached
        assert resumed.stop(timeout=.05) is False
    finally:
        pair_release.set()
        try:
            assert resumed.stop()
        finally:
            if timed_out:timeout_diagnostics("after_drain")
    handler,identity,row=exact_producer(resumed)
    assert row["status"]=="retry_wait" and row["checkpoint"]==saved_prefix
    assert row["next_retry_at"]>datetime.now(timezone.utc)
    result=inspect_now(handler,identity)
    assert result["status"]=="INSPECTED" and result["pair"]["status"]=="PAIR_REFERENCES_VERIFIED"
    assert result["pair"]["stage"]["job_id"]==stages[0].job_id
    assert result["progress"]["assessment"]=="DECLARED" and result["issuer_group"]["assessment"]=="DECLARED_NOT_VERIFIED"
    assert not result["root_body_verified"] and not result["collection_complete"]
    assert not result["pair"]["snapshot_payload_verified"] and not result["pair"]["exact_graph_verified"]
    assert result["financial_status"]==result["currentness_status"]=="NOT_ASSESSED"
    assert inspect_now(handler,{**identity,"day":"2026-09-29"})["status"]=="NOT_CHECKPOINTED"
    assert edgar.urls.count(second_url)==1
