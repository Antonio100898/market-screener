"""Actual scheduling association recovery over real owned PostgreSQL fixtures."""
from dataclasses import asdict, replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from screener.durable_jobs import JobIdentityConflict
from screener.postgres import durable_job, job_schedule_occurrence
from test_sec_exact_annotation_storage import setup_flow, storage
from test_sec_ingestion_storage import _context, NOW, LEASE, pytestmark, database_url


def lookup(jobs, request):
    return jobs.job_for_occurrence(**asdict(request))


def count_jobs(engine):
    with engine.connect() as connection:
        return connection.scalar(select(func.count()).select_from(durable_job))


def test_actual_sec_return_and_readonly_lookup_preserve_capture2_and_running_child(storage,monkeypatch):
    flow=setup_flow(storage,"stage-association",complete_root=False)
    context=_context(flow.root_claim,flow.jobs)
    request=flow.resource.candidate_stage_request(flow.root_claim.job,flow.root_checkpoint)
    result=flow.resource.enqueue_candidate(context,flow.root_checkpoint)
    assert result.job_id==flow.candidate.job.job_id and result.occurrence_id==flow.candidate.job.occurrence_id
    assert result.status=="running" and result.parameters==request.parameters
    assert request.max_attempts==result.max_attempts==3 and request.deadline_at is result.deadline_at is None
    for role,delivery in flow.root_checkpoint["root_deliveries"].items():
        assert result.parameters["root_deliveries"][role]["checked_at"]==delivery["checked_at"]
    before=count_jobs(flow.engine)
    monkeypatch.setattr(flow.jobs,"enqueue_child",lambda *a,**kw:pytest.fail("readonly lookup enqueued"))
    monkeypatch.setattr(flow.source,"latest_sec_filing",lambda *a:pytest.fail("readonly lookup selected latest"))
    assert lookup(flow.jobs,request)==result
    assert count_jobs(flow.engine)==before


def test_actual_commit_lost_return_recovered_without_enqueue_and_after_parent_reclaim(storage,monkeypatch):
    flow=setup_flow(storage,"lost-stage-return",complete_root=False)
    context=_context(flow.root_claim,flow.jobs)
    # Shape-only plan SHA declaration; no registered primary-input admission is claimed.
    request=flow.resource.candidate_stage_request(flow.root_claim.job,flow.root_checkpoint,
        primary_input_plan_sha256="e"*64)
    assert lookup(flow.jobs,request) is None
    original=flow.jobs.enqueue_child
    returned=[]
    def commit_then_interrupt(*args,**kwargs):
        actual=original(*args,**kwargs)
        returned.append(actual)
        raise RuntimeError("synthetic interruption after committed stage job")
    monkeypatch.setattr(flow.jobs,"enqueue_child",commit_then_interrupt)
    with pytest.raises(RuntimeError,match="after committed"):
        flow.resource.enqueue_candidate(context,flow.root_checkpoint,primary_input_plan_sha256="e"*64)
    monkeypatch.setattr(flow.jobs,"enqueue_child",lambda *a,**kw:pytest.fail("lost return lookup enqueued"))
    recovered=lookup(flow.jobs,request)
    assert recovered==returned[0] and recovered.status=="pending"
    before=count_jobs(flow.engine)
    later=NOW+timedelta(minutes=6)
    expired=flow.jobs.recover_expired(now=later)
    retry=next(job.next_retry_at for job in expired if job.job_id==flow.root_claim.job.job_id)
    reclaimed=flow.jobs.claim_next(owner="reclaimed-resource",lease_duration=LEASE,now=retry,
        allowed_kinds={flow.root_claim.job.kind})
    assert reclaimed.job.job_id==flow.root_claim.job.job_id
    again=flow.resource.candidate_stage_request(reclaimed.job,flow.root_checkpoint,primary_input_plan_sha256="e"*64)
    assert again==request and lookup(flow.jobs,again).job_id==recovered.job_id
    assert count_jobs(flow.engine)==before


def test_actual_missing_orphan_and_conflicting_occurrence_refuse_without_writes(storage):
    flow=setup_flow(storage,"occurrence-conflicts")
    request=flow.resource.candidate_stage_request(flow.root_claim.job,flow.root_checkpoint)
    missing=replace(request,schedule_key="sec-candidate-stage:never-enqueued")
    before=count_jobs(flow.engine)
    assert lookup(flow.jobs,missing) is None
    with flow.engine.begin() as connection:
        connection.execute(job_schedule_occurrence.insert().values(schedule_key=missing.schedule_key,
            scheduled_for=missing.scheduled_for))
    with pytest.raises(JobIdentityConflict,match="no associated"):
        lookup(flow.jobs,missing)
    assert count_jobs(flow.engine)==before
    values=asdict(request)
    for changes in ({"kind":"different"},{"priority":request.priority+1},{"max_attempts":4},
            {"due_at":request.due_at+timedelta(seconds=1)},{"deadline_at":request.due_at+timedelta(days=1)},
            {"parameters":{**request.parameters,"observation_id":float(request.parameters["observation_id"])}},
            {"parameters":{**request.parameters,"capture_revision":True}}):
        with pytest.raises(JobIdentityConflict):
            flow.jobs.job_for_occurrence(**{**values,**changes})
    assert count_jobs(flow.engine)==before


def test_actual_child_status_checkpoint_and_generation_changes_keep_immutable_lookup(storage):
    flow=setup_flow(storage,"mutable-stage")
    request=flow.resource.candidate_stage_request(flow.root_claim.job,flow.root_checkpoint)
    running=lookup(flow.jobs,request)
    assert running.status=="running" and running.job_id==flow.candidate.job.job_id
    flow.jobs.complete(flow.candidate.lease,final_checkpoint={"diagnostic":"scheduling only"},now=NOW)
    succeeded=lookup(flow.jobs,request)
    assert succeeded.status=="succeeded" and succeeded.checkpoint=={"diagnostic":"scheduling only"}
    assert succeeded.job_id==running.job_id and succeeded.occurrence_id==running.occurrence_id


def test_actual_pending_retry_wait_and_reclaim_are_diagnostics_only(storage):
    flow=setup_flow(storage,"diagnostic-stage",complete_root=False)
    request=flow.resource.candidate_stage_request(flow.root_claim.job,flow.root_checkpoint,
        primary_input_plan_sha256="f"*64)
    actual=flow.resource.enqueue_candidate(_context(flow.root_claim,flow.jobs),flow.root_checkpoint,
        primary_input_plan_sha256="f"*64)
    assert lookup(flow.jobs,request).status=="pending"
    claimed=flow.jobs.claim_next(owner="new-stage",lease_duration=LEASE,now=NOW,allowed_kinds={"sec-candidate-stage"})
    assert claimed.job.job_id==actual.job_id
    failed=flow.jobs.fail(claimed.lease,error_summary="synthetic temporary error",now=NOW)
    assert failed.status=="retry_wait" and lookup(flow.jobs,request).status=="retry_wait"
    reclaimed=flow.jobs.claim_next(owner="retry-stage",lease_duration=LEASE,now=failed.next_retry_at,
        allowed_kinds={"sec-candidate-stage"})
    again=lookup(flow.jobs,request)
    assert again.job_id==actual.job_id and again.occurrence_id==actual.occurrence_id
    assert again.ownership_generation==reclaimed.lease.generation==claimed.lease.generation+1
