"""Immutable delivery return mechanics against disposable PostgreSQL and verified S3."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from time import monotonic, sleep
import subprocess
import sys

import pytest
from sqlalchemy import func, select, text
from screener.durable_jobs import DurableJobRepository, LeaseLost
from screener.postgres import source_item, source_observation, source_check_occurrence, source_item_current, source_validity_generation, durable_job, durable_job_item
from screener.source_observations import SourceObservationRepository, SourceItemIdentity, StoredSourceObservation, StaleSourceRevision
from test_publication_storage import storage, NOW, _claim, pytestmark
from test_source_observation_storage import _sec_filing_metadata


def record(storage,key,*,state="present",detected=None,revision=1,lease=None,clock=lambda:NOW,item=None):
    engine,artifact=storage
    lease=lease or _claim(engine,key)
    repo=SourceObservationRepository(engine,clock=clock)
    return repo.record(lease,item=item or SourceItemIdentity("SEC","filing","0000001800-26-000001","0000001800"),
        item_outcome_key=key,state=state,metadata={"revision":revision},source_url="https://www.sec.gov/filing",
        artifact_sha256=artifact.content_sha256 if state=="present" else None,detected_at=detected or NOW)


def current(engine):
    with engine.connect() as connection:
        return [dict(r) for r in connection.execute(select(source_item_current).order_by(source_item_current.c.source_item_id)).mappings()]


def state(engine):
    with engine.connect() as connection:
        return {t.name:[dict(r) for r in connection.execute(select(t).order_by(*t.primary_key)).mappings()] for t in
            (source_item,source_observation,source_check_occurrence,source_item_current,source_validity_generation,durable_job_item)}


def test_a_b_a_delivery_replay_unchanged_out_of_order_and_original_outcomes(storage):
    engine=storage[0];repo=SourceObservationRepository(engine)
    leases=[_claim(engine,f"delivery{i}") for i in range(1,6)]
    a1=record(storage,"A1",lease=leases[0])
    b2=record(storage,"B2",lease=leases[1],state="pending",revision=2,detected=NOW+timedelta(seconds=2))
    a3=record(storage,"A3",lease=leases[2],detected=NOW+timedelta(seconds=3))
    assert a1.delivery.source_observation_id==a3.delivery.source_observation_id!=b2.delivery.source_observation_id
    assert a1.created and not a3.created and a1.observation==a3.observation
    assert a3.observation.detecting_job_id==a1.delivery.job_id!=a3.delivery.job_id
    assert a3.delivery.occurrence_id!=a1.delivery.occurrence_id and a3.delivery.checked_at==NOW+timedelta(seconds=3)
    with engine.connect() as connection:generation=connection.scalar(select(source_validity_generation.c.generation))
    a4=record(storage,"A4",lease=leases[3],detected=NOW+timedelta(seconds=4))
    assert a4.delivery.occurrence_id!=a3.delivery.occurrence_id
    with engine.connect() as connection:assert connection.scalar(select(source_validity_generation.c.generation))==generation
    pointer=current(engine)
    for name,lease,original in (("A1",leases[0],a1),("A3",leases[2],a3)):
        replay=record(storage,name,lease=lease,detected=NOW+timedelta(seconds=100),clock=lambda:NOW+timedelta(seconds=100))
        assert replay.delivery==original.delivery and replay.created is False
        assert current(engine)==pointer
    late=record(storage,"old-time",lease=leases[4],state="unavailable",revision=0,detected=NOW-timedelta(seconds=10))
    assert late.delivery.checked_at==NOW-timedelta(seconds=10) and late.delivery.occurrence_id not in {r.delivery.occurrence_id for r in (a1,b2,a3,a4)}
    assert current(engine)==pointer
    with engine.connect() as connection:
        assert connection.scalar(select(source_validity_generation.c.generation))==generation
        for result in (a1,b2,a3,a4,late):
            row=connection.execute(select(source_check_occurrence).where(source_check_occurrence.c.occurrence_id==result.delivery.occurrence_id)).mappings().one()
            assert dict(row)==asdict(result.delivery)
            outcome=connection.scalar(select(durable_job_item.c.outcome).where(durable_job_item.c.job_id==result.delivery.job_id,
                durable_job_item.c.item_key==result.delivery.item_key))
            assert outcome==dict(source_item_id=result.item.source_item_id,source_observation_id=result.observation.source_observation_id,observation_sha256=result.observation.observation_sha256)
    assert all(not hasattr(r.delivery,"accepted") for r in (a1,b2,a3,a4,late))
    assert repo.delivery(a1.delivery.occurrence_id,source_item_id=a1.item.source_item_id,source_observation_id=a1.observation.source_observation_id)==a1.delivery


def test_old_reads_and_positional_construction_do_not_invent_delivery(storage):
    recorded=record(storage,"old-reads");repo=SourceObservationRepository(storage[0])
    assert recorded.delivery is not None
    assert StoredSourceObservation(recorded.item,recorded.observation,recorded.created).delivery is None
    for result in (repo.latest_item("SEC","filing",recorded.item.source_key),repo.latest_sec_filing(recorded.observation.source_observation_id),
        repo.by_sha256(recorded.observation.observation_sha256)):
        assert result.delivery is None and result.observation==recorded.observation
    assert repo.history(recorded.item.source_item_id)==(recorded.observation,)


def test_reclaimed_higher_generation_records_distinct_delivery(storage):
    engine=storage[0];lease=_claim(engine,"same-job")
    first=record(storage,"same-item",lease=lease)
    later=NOW+timedelta(minutes=6)
    jobs=DurableJobRepository(engine)
    recovered=jobs.recover_expired(now=later)
    assert len(recovered)==1 and recovered[0].next_retry_at is not None
    later=recovered[0].next_retry_at
    reclaimed=jobs.claim_next(owner="new-owner",lease_duration=timedelta(minutes=5),now=later,allowed_kinds={"same-job"})
    assert reclaimed.lease.job_id==lease.job_id and reclaimed.lease.generation==lease.generation+1
    second=record(storage,"same-item",lease=reclaimed.lease,detected=later,clock=lambda:later)
    assert second.observation==first.observation and not second.created
    assert second.delivery.occurrence_id!=first.delivery.occurrence_id
    assert second.delivery.ownership_generation==first.delivery.ownership_generation+1
    with pytest.raises(LeaseLost):record(storage,"old-generation",lease=lease,clock=lambda:later)


def test_edinet_list_actual_delivery_preserves_process_ordering_and_replay(storage):
    engine,artifact=storage;lease=_claim(engine,"edinet-list")
    repo=SourceObservationRepository(engine,clock=lambda:NOW)
    values=dict(index_date="2026-10-04",process_at=NOW,item_outcome_key="list",artifact_sha256=artifact.content_sha256,source_url="https://disclosure.edinet-fsa.go.jp/api/list")
    first=repo.record_edinet_list(lease,**values)
    replay=repo.record_edinet_list(lease,**values,detected_at=NOW+timedelta(seconds=3))
    assert first.delivery==replay.delivery and first.delivery.source_item_id==first.item.source_item_id
    assert first.delivery.job_id==lease.job_id and replay.created is False
    assert repo.latest_item("EDINET","aggregate","documents/2026-10-04").delivery is None
    before=state(engine)
    with pytest.raises(StaleSourceRevision,match="older than retained"):
        repo.record_edinet_list(lease,**{**values,"process_at":NOW-timedelta(seconds=1),"item_outcome_key":"older"})
    assert state(engine)==before


def test_witnessed_sec_removal_returns_its_actual_check_not_inventory_or_prior(storage):
    engine,artifact=storage;lease=_claim(engine,"removal");repo=SourceObservationRepository(engine,clock=lambda:NOW)
    inventory=repo.record(lease,item=SourceItemIdentity("SEC","inventory","quarterly-index/2026Q3"),item_outcome_key="inventory",
        state="present",metadata={"quarter":"2026Q3"},artifact_sha256=artifact.content_sha256,
        source_url="https://www.sec.gov/Archives/edgar/full-index/2026/QTR3/form.idx")
    accession="0000001800-26-000001"
    prior=repo.record(lease,item=SourceItemIdentity("SEC","filing",accession,"0000001800"),item_outcome_key="prior",
        state="present",metadata=_sec_filing_metadata(accession),artifact_sha256=artifact.content_sha256,source_url="https://www.sec.gov/prior")
    removed=repo.record_witnessed_sec_removal(lease,prior_observation_id=prior.observation.source_observation_id,item_outcome_key="removed",
        quarter="2026Q3",inventory_source_item_id=inventory.item.source_item_id,inventory_observation_id=inventory.observation.source_observation_id,
        inventory_observation_sha256=inventory.observation.observation_sha256,inventory_artifact_sha256=artifact.content_sha256,detected_at=NOW+timedelta(seconds=1))
    assert removed.delivery.item_key=="removed" and removed.delivery.source_observation_id==removed.observation.source_observation_id
    assert removed.delivery.source_item_id==prior.item.source_item_id!=inventory.item.source_item_id
    assert removed.delivery.occurrence_id not in {prior.delivery.occurrence_id,inventory.delivery.occurrence_id}
    assert removed.observation.state=="removed" and repo.latest_sec_filing(removed.observation.source_observation_id).delivery is None


@pytest.mark.parametrize("fault",["occurrence","item","observation"])
def test_reload_rejects_missing_and_cross_bound_deliveries(storage,fault):
    first=record(storage,"reload");other=record(storage,"other",item=SourceItemIdentity("SEC","filing","other","0000001800"))
    values=dict(occurrence_id=first.delivery.occurrence_id,source_item_id=first.item.source_item_id,source_observation_id=first.observation.source_observation_id)
    values[{"occurrence":"occurrence_id","item":"source_item_id","observation":"source_observation_id"}[fault]]=999999 if fault=="occurrence" else other.item.source_item_id if fault=="item" else other.observation.source_observation_id
    with pytest.raises(ValueError,match="retained occurrence/item/observation"):SourceObservationRepository(storage[0]).delivery(**values)


def test_exact_delivery_reload_in_new_client_and_process_after_current_change(storage):
    first=record(storage,"restart");record(storage,"new-current",state="removed",revision=2,detected=NOW+timedelta(seconds=2))
    engine=storage[0];engine.dispose()
    repo=SourceObservationRepository(engine)
    assert repo.delivery(first.delivery.occurrence_id,source_item_id=first.item.source_item_id,source_observation_id=first.observation.source_observation_id)==first.delivery
    process=subprocess.run([sys.executable,"-c","""
from dataclasses import asdict
import json,sys
from sqlalchemy import create_engine
from screener.storage_config import StorageSettings
from screener.source_observations import SourceObservationRepository
engine=create_engine(StorageSettings.from_env().database_url)
try:
 record=SourceObservationRepository(engine).delivery(int(sys.argv[1]),source_item_id=int(sys.argv[2]),source_observation_id=int(sys.argv[3]))
 print(json.dumps(asdict(record),default=str))
finally:engine.dispose()
""",str(first.delivery.occurrence_id),str(first.item.source_item_id),str(first.observation.source_observation_id)],capture_output=True,text=True,check=True,timeout=15)
    assert json.loads(process.stdout)==json.loads(json.dumps(asdict(first.delivery),default=str))


@pytest.mark.parametrize("replay",[False,True])
def test_late_outcome_failure_rolls_back_observation_check_pointer_generation(storage,monkeypatch,replay):
    engine=storage[0];lease=_claim(engine,"late-failure");repo=SourceObservationRepository(engine,clock=lambda:NOW)
    if replay:record(storage,"failure",lease=lease)
    before=state(engine)
    def fail(*args,**kwargs):raise RuntimeError("late outcome failure")
    monkeypatch.setattr(repo._jobs,"_record_item_outcome_on_connection",fail)
    with pytest.raises(RuntimeError,match="late outcome failure"):
        repo.record(lease,item=SourceItemIdentity("SEC","filing","0000001800-26-000001","0000001800"),item_outcome_key="failure",
            state="present",metadata={"revision":1},source_url="https://www.sec.gov/filing",artifact_sha256=storage[1].content_sha256)
    assert state(engine)==before


@pytest.mark.parametrize("table_name",["job","item","edinet-prior"])
@pytest.mark.parametrize("expires",[False,True])
def test_real_post_lock_live_lease_for_delivery_return(storage,table_name,expires):
    engine,artifact=storage;existing=record(storage,"initial")
    now=datetime.now(timezone.utc);jobs=DurableJobRepository(engine)
    jobs.enqueue_scheduled(schedule_key="live",scheduled_for=now,kind="live",parameters={},due_at=now)
    live=jobs.claim_next(owner="live",lease_duration=timedelta(seconds=1 if expires else 10),allowed_kinds={"live"})
    repo=SourceObservationRepository(engine)
    if table_name=="edinet-prior":
        fixed=SourceObservationRepository(engine,clock=lambda:NOW)
        old=fixed.record_edinet_list(_claim(engine,"initial-edinet"),index_date="2026-10-04",process_at=NOW,item_outcome_key="edinet",
            artifact_sha256=artifact.content_sha256,source_url="https://disclosure.edinet-fsa.go.jp/api/list")
        table,identity=source_observation,source_observation.c.source_observation_id==old.observation.source_observation_id
        operation=lambda:repo.record_edinet_list(live.lease,index_date="2026-10-04",process_at=NOW+timedelta(seconds=1),item_outcome_key="live",
            artifact_sha256=artifact.content_sha256,source_url="https://disclosure.edinet-fsa.go.jp/api/list")
    else:
        table=durable_job if table_name=="job" else source_item
        identity=durable_job.c.job_id==live.lease.job_id if table_name=="job" else source_item.c.source_item_id==existing.item.source_item_id
        operation=lambda:repo.record(live.lease,item=SourceItemIdentity("SEC","filing",existing.item.source_key,"0000001800"),
            item_outcome_key="live",state="pending",metadata={"revision":2},source_url="https://www.sec.gov/filing")
    before=state(engine)
    with engine.connect() as blocker:
        tx=blocker.begin();blocker.execute(select(table).where(identity).with_for_update()).one()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(operation)
            try:
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
                    deadline=monotonic()+3
                    while not observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")):
                        assert monotonic()<deadline and not future.done();sleep(.01)
                sleep(1.1 if expires else .05)
            finally:tx.rollback()
            if expires:
                with pytest.raises(LeaseLost):future.result(5)
            else:result=future.result(5)
    if expires:assert state(engine)==before
    else:
        assert result.delivery.job_id==live.lease.job_id and result.delivery.ownership_generation==live.lease.generation
        assert result.delivery.source_observation_id==result.observation.source_observation_id
