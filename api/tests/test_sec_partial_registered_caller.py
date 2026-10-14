"""Registered strict profiles through real public owners; synthetic in-memory boundaries."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import threading

import pytest

from screener import automatic_ingestion as automatic, sec_inventory_plan as owner
from screener.artifacts import store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseToken
from screener.job_runtime import JobContext
from screener.sec_ingestion import SecResourceFetchHandler, _root_resources
from screener.source_observations import StoredSourceObservation
from test_sec_inventory_plan import fixture, parameters, NOW, finish, fixture_bodies, day_url
from test_sec_ingestion import _index, _row
from test_sec_completed_capture import Fixture as CompletedFixture
from test_sec_resource_partial_acquisition import Fixture as PartialFixture
from test_sec_finite_result_inspection import JobEngine
from test_automatic_ingestion import assembled


def partial_case(bodies=None):
    case = fixture(bodies)
    case.handler = owner.SecFiniteInventoryHandler(edgar=case.transport, object_store=case.objects,
        artifacts=case.artifacts, observations=case.sources, jobs=case.jobs, clock=lambda:NOW, kind=owner.PARTIAL_KIND)
    value = parameters("partial", request_revision=2)
    job = case.jobs.enqueue_scheduled(schedule_key=case.handler.namespace+"partial", scheduled_for=NOW,
        kind=owner.PARTIAL_KIND, parameters=value, due_at=NOW)
    job = replace(job, status="running", ownership_generation=1)
    case.jobs.jobs[job.job_id] = job
    case.context = JobContext(job, threading.Event(), case.jobs, LeaseToken(job.job_id,"worker",1), lambda:NOW)
    case.jobs.job_for_occurrence = DurableJobRepository(JobEngine(case.jobs)).job_for_occurrence
    return case


def plan(tmp_path, **changes):
    params = parameters("partial", request_revision=2)
    occurrence = dict(kind=owner.PARTIAL_KIND, parameters=params,
        schedule_key="automatic-ingestion:v1:sec-finite-inventory-v2:partial",
        scheduled_for=NOW.isoformat(), due_at=NOW.isoformat())
    occurrence.update(changes)
    path = tmp_path/"declared.json"
    path.write_text(json.dumps(dict(plan_revision=1, occurrences=[occurrence])))
    return path


def test_public_plan_registry_selects_only_new_resource_retention(tmp_path, monkeypatch):
    path = plan(tmp_path)
    declaration, = automatic.read_plan(path)
    assert declaration.kind == owner.PARTIAL_KIND and declaration.parameters["request_revision"] == 2
    settings, events, borrowed = assembled(monkeypatch, tmp_path, borrowed=True)
    settings = replace(settings, plan_path=path)
    assembly = automatic.AutomaticIngestion(settings, **borrowed)
    assembly.start()
    try:
        handlers = assembly.runtime.handlers
        assert handlers[owner.PARTIAL_KIND].resource_kind == "sec-resource-fetch-v3"
        assert handlers[owner.KIND].resource_kind == "sec-resource-fetch-v2"
        assert handlers["sec-resource-fetch-v3"]._retain_partial_acquisitions is True
        assert handlers["sec-resource-fetch-v3"]._annotate_annual is True
        assert handlers["sec-resource-fetch-v2"]._retain_partial_acquisitions is False
        assert handlers["sec-resource-fetch"]._annotate_annual is False
        assert assembly.runtime.worker_count == 1
    finally: assert assembly.stop()


@pytest.mark.parametrize("fault", ["revision", "namespace", "time", "priority", "params", "count"])
def test_new_raw_plan_refuses_before_parameter_leaf_factory(tmp_path, monkeypatch, fault):
    path = plan(tmp_path)
    raw = json.loads(path.read_text()); entry = raw["occurrences"][0]
    if fault == "revision": entry["parameters"]["request_revision"] = True
    elif fault == "namespace": entry["schedule_key"] = owner.NAMESPACE+"partial"
    elif fault == "time": entry["scheduled_for"] = NOW.replace(tzinfo=None).isoformat()
    elif fault == "priority": entry["priority"] = True
    elif fault == "params": entry["parameters"] = None
    else: raw["occurrences"] *= 1001
    path.write_text(json.dumps(raw))
    monkeypatch.setattr(owner.InventoryParameters2, "model_validate_json", lambda *a:pytest.fail("raw refusal reached leaf factory"))
    with pytest.raises(ValueError): automatic.read_plan(path)


@pytest.mark.parametrize("kind", [owner.KIND, owner.PARTIAL_KIND, "sec-recent-discovery"])
def test_duplicate_raw_plan_keys_refuse_shared_codec_and_valid_legacy_survives(tmp_path, kind):
    path = plan(tmp_path)
    if kind != owner.PARTIAL_KIND:
        value = json.loads(path.read_text()); entry = value["occurrences"][0]
        entry.update(kind=kind, schedule_key=f"automatic-ingestion:v1:{kind}:partial")
        entry["parameters"] = (parameters("partial") if kind == owner.KIND else
                               dict(start_date="2026-09-28", end_date="2026-09-28"))
        path.write_text(json.dumps(value))
    expected = automatic.read_plan(path)
    original = path.read_text()
    path.write_text(original.replace('"plan_revision": 1', '"plan_revision": 1, "plan_revision": 1'))
    with pytest.raises(ValueError, match="duplicate"): automatic.read_plan(path)
    path.write_text(original)
    assert automatic.read_plan(path) == expected


@pytest.mark.parametrize("kind", [owner.PARTIAL_KIND, "sec-recent-discovery"])
def test_json_mode_preserves_datetime_admission_without_loosening_python_input(tmp_path, kind):
    path = plan(tmp_path)
    value = json.loads(path.read_text())
    if kind != owner.PARTIAL_KIND:
        value["occurrences"][0].update(kind=kind, schedule_key=f"automatic-ingestion:v1:{kind}:partial",
            parameters=dict(start_date="2026-09-28", end_date="2026-09-28"))
    assert automatic.FinitePlan.model_validate_json(owner.encode(value)).occurrences[0].scheduled_for == NOW
    with pytest.raises(ValueError): automatic.FinitePlan.model_validate(value)


def test_new_shared_loop_recovery_inspection_and_duplicate_child_identity():
    raw = _row("10-K", "DUP", "1234", "2026-09-28", "0000001234-26-000001")
    bodies = fixture_bodies(); bodies[day_url(NOW.date()-timedelta(days=1))] = _index(raw, raw)
    case = partial_case(bodies)
    root, job = finish(case, case.context)
    assert type(root) is owner.InventoryRoot2 and root.request.producer_kind == owner.PARTIAL_KIND
    assert job.checkpoint["checkpoint_revision"] == 2
    rows = case.handler._read_chunks(root.days[0].chunks)
    assert len(rows) == 2 and all(type(r) is owner.IndexEntry2 for r in rows)
    assert rows[0].request.child == rows[1].request.child
    assert rows[0].ordinal != rows[1].ordinal and rows[0].physical_line != rows[1].physical_line
    child = rows[0].request.child
    assert child.kind == "sec-resource-fetch-v3" and child.schedule_key.startswith("sec-resource-fetch-v3:occurrence:")
    assert set(child.parameters.model_dump()) == {"accession", "cik", "form", "filename", "observation_id"}
    result = case.handler.inspect_completed_entry(job_id=job.job_id, occurrence_id=job.occurrence_id,
        schedule_key=case.handler.namespace+"partial", scheduled_for=NOW, day=root.days[0].day.isoformat(), entry_ordinal=1)
    assert result["entry"]["ordinal"] == 1 and result["capture"]["status"] == "NOT_COMPLETED"
    assert result["stage"]["status"] == "NOT_INSPECTED_CAPTURE_NOT_VERIFIED"
    assert root.financial_status == root.source_authority_status == root.listing_security_status == "NOT_ASSESSED"


def test_new_root_profile_substitution_and_old_build_refuse_without_reblessing(monkeypatch):
    case = partial_case(); root, job = finish(case, case.context)
    old = fixture().handler
    with pytest.raises(ValueError, match="producer kind"): old.recover_completed(job)
    with pytest.raises(ValueError): owner.load(owner.InventoryRoot, owner.encode(root.model_dump(mode="json")))
    with pytest.raises(ValueError): owner.load(owner.InventoryCheckpoint, owner.encode(job.checkpoint))
    chunk = case.handler._read(root.days[0].chunks[0])
    with pytest.raises(ValueError): owner.load(owner.Chunk, chunk)
    first = next(r for r in case.handler._read_chunks(root.days[0].chunks) if r.request)
    with pytest.raises(ValueError): owner.load(owner.ChildRef, owner.encode(first.request.child.model_dump(mode="json")))
    with pytest.raises(ValueError): owner.load(owner.ProducerRequest, owner.encode(root.request.model_dump(mode="json")))
    immutable = replace(job, parameters={**job.parameters,"producer_build_sha256":"0"*64})
    monkeypatch.setattr(case.jobs, "scheduled_job", lambda **kw:pytest.fail("old build read owner"))
    with pytest.raises(ValueError, match="build differs"): case.handler( replace(case.context, job=immutable))
    with pytest.raises(ValueError, match="build differs"): case.handler.recover_completed(immutable)


def test_v3_live_completed_recovery_and_candidate_gates():
    f = PartialFixture(); f.job = replace(f.job, kind="sec-resource-fetch-v3")
    checkpoint = f.run()
    candidate = f.handler.candidate_stage_request(f.job, checkpoint)
    assert candidate.parameters == {**f.job.parameters, **{key:checkpoint[key] for key in
        ("roots", "capture_revision", "root_deliveries")}}
    assert set(checkpoint) == {"accession", "cik", "filing_observation_id", "roots", "capture_revision", "root_deliveries"}
    legacy = {key:value for key,value in checkpoint.items() if key not in {"capture_revision", "root_deliveries"}}
    with pytest.raises(ValueError): f.handler.candidate_stage_request(f.job, legacy)
    with pytest.raises(ValueError): f.handler(replace(f.context(), job=replace(f.job, checkpoint=legacy)))
    default = SecResourceFetchHandler(edgar=f, object_store=f, artifacts=f, observations=f, jobs=f)
    with pytest.raises(ValueError, match="explicit partial"): default(f.context())
    completed = CompletedFixture(); completed.job = replace(completed.job, kind="sec-resource-fetch-v3")
    assert completed.recover()["status"] == "VERIFIED"
    completed.job = replace(completed.job, checkpoint=legacy)
    with pytest.raises(ValueError): completed.recover()


def test_selected_chunk_template_and_scalar_reservation_before_fanout():
    case = partial_case(); root, _ = finish(case, case.context)
    row = next(r for r in case.handler._read_chunks(root.days[0].chunks) if r.request)
    width, timestamp = "9"*19, "9999-12-31T23:59:59.999999+00:00"
    value = row.request.model_dump(mode="json")
    filing, source, child = value["filing"], value["filing"]["source"], value["child"]
    for name in ("job_id", "occurrence_id"): child[name] = width
    child.update(schedule_key="sec-resource-fetch-v3:occurrence:"+width+":"+"9"*10+":"+"a"*20+":"+"h"*64,
        scheduled_for=timestamp, due_at=timestamp, priority="-2147483648", max_attempts="3")
    child["parameters"].update(filename="", observation_id=width, form="10-QT/A")
    filing.update(receipt_key="sec-finite:filing:9999-12-31:99999", etag="", last_modified="", media_type="")
    source.update(source_url="", artifact_byte_size="9"*7)
    for name in ("occurrence_id", "source_item_id", "source_observation_id", "job_id", "ownership_generation"):
        source["delivery"][name] = width
    source["delivery"].update(checked_at=timestamp, item_key="filing:"+"a"*20+":"+"h"*64)
    assert len(owner.encode(value)) <= owner.REQUEST_FIXED_BYTES
    old = b"".join(owner.pack_chunks([row.model_copy(update={"request":None})]))
    new = b"".join(owner.pack_chunks([row], contract="SEC_INVENTORY_CHUNK2"))
    assert old.startswith(owner._CHUNK_PREFIX) and new.startswith(b'{"contract":"SEC_INVENTORY_CHUNK2"')
    assert len(owner._chunk_prefix("SEC_INVENTORY_CHUNK2")) == len(owner._CHUNK_PREFIX)
    _, _, digest = case.handler._actual_request(case.context.job)
    acquired, _ = case.handler.discovery.retained_day(case.context, root.days[0].day,
        request_identity_sha256=digest, maximum_bytes=owner.MAX_BODY_BYTES,
        maximum_text_bytes=owner.MAX_SOURCE_TEXT_BYTES, maximum_rows=owner.MAX_RECORDS,
        maximum_row_bytes=owner.MAX_ENTRY_BYTES)
    with pytest.raises(ValueError, match="frontier"):
        owner.day_frontier(acquired, [row], used_bytes=owner.MAX_TOTAL_BYTES,
                           remaining_requests=owner.MAX_RECORDS, resource_kind="sec-resource-fetch-v3",
                           chunk_contract="SEC_INVENTORY_CHUNK2")
