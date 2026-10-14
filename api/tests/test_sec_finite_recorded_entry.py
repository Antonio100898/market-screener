"""Point-read public owners over labelled in-memory checkpoint/source/job boundaries."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from screener import automatic_ingestion as automatic, sec_inventory_plan as owner
from screener.artifacts import store_evidence
from screener.figure_history import FigureHistoryRepository
from screener.sec_ingestion import SecCandidateStageHandler
from screener.shared_companies import SharedCompanyRepository
from test_sec_finite_result_inspection import case as complete_case, complete_resource, forbid, inspect as inspect_completed
from test_sec_inventory_plan import fixture_bodies, day_url, NOW
from test_sec_ingestion import _index, _row
from test_automatic_ingestion import assembled


def point(value, **changes):
    return value.handler.inspect_recorded_entry(**{**value.args, **changes})


def value(bodies=None, *, status="running", prefix=1):
    current = complete_case(bodies)
    cp = deepcopy(current.job.checkpoint)
    cp["days"] = cp["days"][:prefix]
    cp["root"] = None
    current.job = replace(current.job, status=status, checkpoint=cp)
    current.jobs.jobs[current.job.job_id] = current.job
    engine = current.jobs.job_for_occurrence.__self__.engine
    current.jobs.engine = current.sources.engine = current.artifacts.engine = engine
    companies = SharedCompanyRepository(engine)
    current.handler.stage_references = SecCandidateStageHandler(edgar=current.transport, object_store=current.objects,
        artifacts=current.artifacts, observations=current.sources, companies=companies, history=FigureHistoryRepository(engine))
    return current


@pytest.mark.parametrize("status", ["pending", "running", "retry_wait", "failed"])
def test_no_checkpoint_is_no_recorded_progress_despite_retained_work(status, monkeypatch):
    current = value(status=status)
    current.jobs.jobs[current.job.job_id] = replace(current.job, checkpoint=None)
    assert current.sources.receipts
    monkeypatch.setattr(current.handler, "_recover_source", lambda *a:pytest.fail("no checkpoint inspected source"))
    result = point(current)
    assert result["status"] == "NO_RECORDED_PROGRESS" and result["producer"]["job_status"] == status
    assert result["progress"] is None and not result["collection_complete"]


@pytest.mark.parametrize("status", ["running", "retry_wait", "failed"])
def test_checkpointed_row_delegates_real_pending_owner_and_leaves_counts_declared(status, monkeypatch):
    current = value(status=status)
    forbid(current, monkeypatch)
    monkeypatch.setattr(current.handler, "_completed", lambda *a:pytest.fail("point replayed root"))
    monkeypatch.setattr(current.handler, "_ticker_records", lambda *a:pytest.fail("point read listing/groups"))
    result = point(current)
    assert result["status"] == "INSPECTED" and result["entry"]["physical_line"] == 4
    assert result["pair"]["status"] == "CAPTURE_UNAVAILABLE"
    assert result["pair"]["capture_status"] == "NOT_COMPLETED"
    assert result["progress"]["assessment"] == "DECLARED" and result["progress"]["checkpointed_days"] == 1
    assert result["issuer_group"]["assessment"] == "DECLARED_NOT_VERIFIED"
    assert result["selected_row_verified"] and not result["root_body_verified"] and not result["cohort_sources_verified"]
    assert result["financial_status"] == result["currentness_status"] == "NOT_ASSESSED"


def test_uncheckpointed_day_does_not_search_for_existing_real_row_requests(monkeypatch):
    bodies = fixture_bodies()
    bodies[day_url(NOW.date())] = _index(_row("10-K", "SECOND", "1234", "2026-09-29", "0000001234-26-000002"))
    current = value(bodies)
    assert any(j.parameters.get("accession") == "0000001234-26-000002" for j in current.jobs.jobs.values())
    monkeypatch.setattr(current.handler, "_recover_source", lambda *a:pytest.fail("uncheckpointed day read source"))
    result = point(current, day="2026-09-29")
    assert result["status"] == "NOT_CHECKPOINTED" and "pair" not in result


def test_unavailable_day_and_present_empty_day_are_distinct():
    unavailable = value(prefix=2)
    assert point(unavailable, day="2026-09-29")["status"] == "DAY_SOURCE_UNAVAILABLE"
    bodies = fixture_bodies(); bodies[day_url(NOW.date())] = _index()
    empty = value(bodies, prefix=2)
    result = point(empty, day="2026-09-29")
    assert result["status"] == "ROW_NOT_IN_VERIFIED_DAY" and result["selected_day_bytes_verified"]


@pytest.mark.parametrize("ordinal", [1,2])
def test_nonroutes_preserve_diagnostics_without_pair_dependency(ordinal):
    current = value(); current.handler.stage_references = None
    result = point(current, entry_ordinal=ordinal)
    assert result["status"] == "INSPECTED" and result["pair"] is None and result["entry"]["request"] is None


def test_completed_capture_delegates_once_to_actual_stage_operational_owner(monkeypatch):
    current = value()
    complete_resource(current)
    calls = []
    actual = current.handler.stage_references.recover_completed_pair_references
    def observe(**quartet):
        calls.append(quartet)
        return actual(**quartet)
    monkeypatch.setattr(current.handler.stage_references, "recover_completed_pair_references", observe)
    forbid(current, monkeypatch)
    result = point(current)
    assert len(calls) == 1 and result["pair"]["status"] == "STAGE_NOT_COMPLETED"
    assert result["pair"]["root_capture_bytes_verified"]
    assert not result["pair"]["snapshot_payload_verified"] and not result["pair"]["exact_graph_verified"]


def test_same_engine_is_required_only_for_routed_delegation():
    current = value(); current.handler.stage_references._history.engine = object()
    with pytest.raises(ValueError, match="same-engine"): point(current)
    current.handler.stage_references = None
    with pytest.raises(ValueError, match="same-engine"): point(current)


@pytest.mark.parametrize("fault", ["status", "generation", "checkpoint", "child_status", "child_checkpoint"])
def test_child_and_producer_stamps_bracket_owner_delegation(monkeypatch, fault):
    current = value()
    actual = current.handler.stage_references.recover_completed_pair_references
    def drift(**quartet):
        result = actual(**quartet)
        job_id = quartet["job_id"] if fault.startswith("child") else current.job.job_id
        job = current.jobs.jobs[job_id]
        change = (dict(status="failed") if fault in {"status","child_status"} else
                  dict(ownership_generation=job.ownership_generation+1) if fault == "generation" else
                  dict(checkpoint={"changed":True}))
        current.jobs.jobs[job_id] = replace(job, **change)
        return result
    monkeypatch.setattr(current.handler.stage_references, "recover_completed_pair_references", drift)
    with pytest.raises(ValueError, match="changed"): point(current)


@pytest.mark.parametrize("fault", ["days", "refs", "count", "revision", "digest", "partition", "root"])
def test_raw_checkpoint_refusals_precede_models_and_body_reads(monkeypatch, fault):
    current = value(); cp = deepcopy(current.job.checkpoint)
    if fault == "days": cp["days"] *= 32
    elif fault == "refs": cp["ticker_chunks"] *= 1025
    elif fault == "count": cp["days"][0]["row_count"] = True
    elif fault == "revision": cp["checkpoint_revision"] = 2
    elif fault == "digest": cp["request_identity_sha256"] = "a"*64
    elif fault == "partition": cp["days"][0]["request_count"] += 1
    else: cp["root"] = current.root.model_dump(mode="json")["listing"]["source"]
    current.jobs.jobs[current.job.job_id] = replace(current.job, checkpoint=cp)
    monkeypatch.setattr(owner.InventoryCheckpoint, "model_validate_json", lambda *a:pytest.fail("raw refusal reached factory"))
    monkeypatch.setattr(current.handler, "_recover_source", lambda *a:pytest.fail("raw refusal read source"))
    with pytest.raises(ValueError): point(current)


def test_succeeded_missing_checkpoint_or_root_refuses():
    current = value(status="succeeded")
    with pytest.raises(ValueError, match="final root"): point(current)
    current.jobs.jobs[current.job.job_id] = replace(current.job, checkpoint=None)
    with pytest.raises(ValueError, match="checkpoint"): point(current)


def test_duplicate_physical_entries_share_one_genuine_child_and_group_is_unverified():
    row = _row("10-K", "DUP", "1234", "2026-09-28", "0000001234-26-000001")
    bodies = fixture_bodies(); bodies[day_url(NOW.date()-timedelta(days=1))] = _index(row,row)
    current = value(bodies)
    one,two = point(current),point(current,entry_ordinal=1)
    assert one["entry"]["request"]["child"] == two["entry"]["request"]["child"]
    assert one["entry"]["physical_line"] != two["entry"]["physical_line"]
    assert one["issuer_group"]["assessment"] == "DECLARED_NOT_VERIFIED"


def test_original_raw_row_larger_than_schedule_text_ceiling_still_admits():
    row=_row("10-K","WIDE","1234","2026-09-28","0000001234-26-000001")+" "*65537
    bodies=fixture_bodies();bodies[day_url(NOW.date()-timedelta(days=1))]=_index(row)
    current=value(bodies)
    result=point(current)
    assert result["selected_row_verified"] and result["entry"]["source_row"]==row
    assert len(result["entry"]["source_row"])>65536


def test_combined_capacity_keeps_only_bounded_identity_without_truncated_evidence(monkeypatch):
    current=value()
    actual=owner._capture_result_json
    def tight_frontier(result):
        if "entry" in result:return None,owner.MAX_ENVELOPE_BYTES+1,True
        return actual(result)
    monkeypatch.setattr(owner,"_capture_result_json",tight_frontier)
    result=point(current)
    assert result["status"]=="CAPACITY_UNAVAILABLE" and result["gap"]["required_is_lower_bound"]
    assert "entry" not in result and "pair" not in result and not result["collection_complete"]


@pytest.mark.parametrize("fault", ["type", "ordinal"])
def test_late_day_chunk_substitution_refuses_before_entry_factories(monkeypatch,fault):
    current=value();ref=current.root.days[0].chunks[0]
    raw=json.loads(current.handler._read(ref))
    if fault=="type":
        raw["records"][-1]=current.handler._read_chunks(current.root.ticker_chunks)[0].model_dump(mode="json")
    else:raw["records"][-1]["ordinal"]-=1
    artifact=store_evidence(owner.encode(raw),owner.MEDIA_TYPE,current.objects,current.artifacts)
    cp=deepcopy(current.job.checkpoint)
    cp["days"][0]["chunks"][0].update(artifact_sha256=artifact.content_sha256,byte_size=artifact.byte_size)
    current.jobs.jobs[current.job.job_id]=replace(current.job,checkpoint=cp)
    monkeypatch.setattr(owner.Chunk,"model_validate_json",lambda *a:pytest.fail("late malformed INDEX reached leaf factory"))
    with pytest.raises(ValueError,match="INDEX/ordinal"):point(current)


def test_profile_namespace_and_old_build_refuse_before_sources(monkeypatch):
    current = value()
    monkeypatch.setattr(current.handler,"_recover_source",lambda *a:pytest.fail("bad authority read source"))
    with pytest.raises(ValueError,match="namespace"): point(current,schedule_key=current.args["schedule_key"].replace("v1:","v2:"))
    current.jobs.jobs[current.job.job_id] = replace(current.job,parameters={**current.job.parameters,"producer_build_sha256":"0"*64})
    with pytest.raises(ValueError,match="build differs"): point(current)


def test_shared_predicate_preserves_old_verified_group_orphan_checks():
    current = complete_case()
    assert current.handler.recover_completed(current.job) == current.root
    chunk = current.handler._read_chunks(current.root.days[0].chunks)
    _, request, digest = current.handler._actual_request(current.job)
    day = current.root.days[0]; stored = current.handler._recover_source(day.acquisition,request,digest)
    _, raw = owner.read_registered_artifact(stored.observation.artifact_sha256,current.objects,current.artifacts,maximum=owner.MAX_BODY_BYTES)
    row = owner.parse_all_form_index(raw)[0]
    changed = chunk[0].model_copy(update={"issuer_group_ordinal":2})
    current.handler._verify_recorded_row(changed,row,day,stored,request,digest)
    with pytest.raises(ValueError): current.handler._verify_recorded_row(changed,row,day,stored,request,digest,group_by_cik={row.filing.cik:0})


def test_registry_injects_same_actual_stage_into_both_finite_profiles(monkeypatch,tmp_path):
    settings,events,borrowed = assembled(monkeypatch,tmp_path,borrowed=True)
    assembly = automatic.AutomaticIngestion(settings,**borrowed);assembly.start()
    try:
        handlers=assembly.runtime.handlers; stage=handlers["sec-candidate-stage"]
        assert handlers[owner.KIND].stage_references is handlers[owner.PARTIAL_KIND].stage_references is stage
    finally: assert assembly.stop()
