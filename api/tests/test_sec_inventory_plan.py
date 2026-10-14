"""Finite caller contracts over returned in-memory owner records; no network/SQL."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta, timezone
from types import SimpleNamespace
import json
import threading

import pytest

from screener import sec_inventory_plan as owner
from screener.durable_jobs import LeaseToken, JobIdentityConflict
from screener.job_runtime import JobContext
from screener.object_store import ImmutableObjectStore
from screener.sec_ingestion import parse_all_form_index, parse_form_index, resource_route, _DAILY_INDEX_URL
from screener.source_observations import (
    SourceItemIdentity, SourceItemRecord, SourceObservationRecord, StoredSourceObservation, observation_sha256, _canonical_url,
)
from screener.source_validity import SourceDeliveryRecord
from screener.sources.edgar import EdgarTransportResult, EdgarPayloadTooLarge, NoXbrlDataError, TICKER_MAP_URL
from test_market_captures import Repository
from test_object_store import MemoryS3
from test_sec_ingestion import _index, _row, _job, NOW


def parameters(key="finite", **changes):
    value = dict(request_revision=1, operation_key=key, operation_at=NOW.isoformat(),
        start_date="2026-09-28", end_date="2026-09-29", expected_parent_release_id=None,
        producer_build_sha256=owner.producer_build_sha256())
    value.update(changes)
    return value


def day_url(day):
    return _DAILY_INDEX_URL.format(year=day.year, quarter=(day.month-1)//3+1, ymd=day.strftime("%Y%m%d"))


def fixture_bodies():
    ticker = json.dumps({
        "slash/~": {"cik_str":1234,"ticker":"AAA","title":"A"},
        "second": {"cik_str":1234,"ticker":"AAB","title":"A"},
        "dup": {"cik_str":9876,"ticker":"AAA","title":"B"},
        "bool": {"cik_str":True,"ticker":"BOOL","title":"Broken"},
        "idle": {"cik_str":4444,"ticker":"IDLE","title":"No requests"},
        "null": None,
    }, ensure_ascii=False, separators=(",", ":")).encode()
    rows = (
        _row("10-K", "A", "1234", "2026-09-27", "0000001234-26-000001"),
        "",
        _row("8-K", "EVENT", "1234", "2026-09-28", "0000001234-26-000002"),
        _row("10-KX", "PREFIX", "1234", "2026-09-28", "0000001234-26-000003"),
        _row("20-F", "ORPHAN", "9999", "2026-09-28", "0000009999-26-000004"),
    )
    return {TICKER_MAP_URL:ticker, day_url(date(2026,9,28)):_index(*rows),
        day_url(date(2026,9,29)):NoXbrlDataError("labelled synthetic404")}


class RetainedTransport:
    def __init__(self, bodies=None):
        self.bodies, self.calls = bodies or fixture_bodies(), []
        self.media_type = None
        self.override_media_type = False

    def fetch(self, url, *, maximum_bytes=None):
        self.calls.append((url, maximum_bytes))
        value = self.bodies[url]
        if isinstance(value, Exception):
            raise value
        if maximum_bytes is not None and len(value) > maximum_bytes:
            raise EdgarPayloadTooLarge("synthetic raw body over cap")
        media = self.media_type if self.override_media_type else "text/plain" if url.endswith(".idx") else "application/json"
        return EdgarTransportResult(value, url, media,
            "synthetic-etag", "synthetic-validator")


class MemoryJobs:
    """Synthetic durable owner supplies actual returned records, not caller IDs."""
    def __init__(self):
        self.jobs, self.schedules = {}, {}

    def enqueue_scheduled(self, *, schedule_key, scheduled_for, kind, parameters, due_at, priority=30,
                          max_attempts=3, deadline_at=None, **unused):
        identity = (schedule_key, scheduled_for)
        if identity in self.schedules:
            job = self.jobs[self.schedules[identity]]
            if (job.kind, job.parameters, job.due_at, job.priority, job.max_attempts, job.deadline_at) != (
                    kind, parameters, due_at, priority, max_attempts, deadline_at):
                raise JobIdentityConflict("synthetic immutable schedule conflict")
            return job
        number = len(self.jobs)+1
        job = replace(_job(deepcopy(parameters), kind=kind), job_id=number, occurrence_id=number,
            due_at=due_at, priority=priority, max_attempts=max_attempts, deadline_at=deadline_at)
        self.jobs[number], self.schedules[identity] = job, number
        return job

    def enqueue_child(self, lease, *, child_key, scheduled_for, **values):
        values.pop("now", None)
        return self.enqueue_scheduled(schedule_key=child_key, scheduled_for=scheduled_for, **values)

    def scheduled_job(self, *, job_id, occurrence_id, schedule_key, scheduled_for):
        if self.schedules.get((schedule_key,scheduled_for)) != job_id:
            raise JobIdentityConflict("synthetic exact child association missing")
        job = self.jobs[job_id]
        if job.occurrence_id != occurrence_id:
            raise JobIdentityConflict("synthetic exact occurrence differs")
        return job

    def save_checkpoint(self, lease, checkpoint, *, now):
        job = replace(self.jobs[lease.job_id], checkpoint=deepcopy(checkpoint))
        self.jobs[lease.job_id] = job
        return job

    def context(self, key="finite", generation=1, **changes):
        params = parameters(key, **changes)
        job = self.enqueue_scheduled(schedule_key=owner.NAMESPACE+key, scheduled_for=NOW,
            kind=owner.KIND, parameters=params, due_at=NOW)
        job = replace(job, status="running", owner="worker", ownership_generation=generation)
        self.jobs[job.job_id] = job
        return JobContext(job, threading.Event(), self, LeaseToken(job.job_id,"worker",generation), lambda:NOW)


class MemorySources:
    """Synthetic source owner mints and retains source/check/receipt records."""
    def __init__(self):
        self.items, self.observations, self.deliveries, self.receipts, self.current = {}, {}, {}, {}, {}
        self.writes = 0

    def acquisition_receipt(self, *, producer_job_id, receipt_key, request_identity_sha256):
        entry = self.receipts.get((producer_job_id,receipt_key))
        if entry is None:
            return None
        digest, source = entry
        if digest != request_identity_sha256:
            raise ValueError("synthetic exact receipt request differs")
        return deepcopy(replace(source,created=False))

    def record_with_acquisition_receipt(self, lease, *, receipt_key, request_identity_sha256, item,
            item_outcome_key, state, metadata, source_url, artifact_sha256=None, etag=None,
            last_modified=None, source_published_at=None, detected_at=None):
        assert (lease.job_id,receipt_key) not in self.receipts, "caller should recover committed receipt"
        source_url = _canonical_url(source_url)
        self.writes += 1
        identity = (item.source_system,item.item_kind,item.source_key)
        if identity not in self.items:
            self.items[identity] = SourceItemRecord(len(self.items)+1,item.source_system,item.item_kind,
                item.source_key,item.issuer_source_identifier,item.parent_source_item_id,NOW)
        stored_item = self.items[identity]
        digest = observation_sha256(state=state,metadata=metadata,source_url=source_url,
            artifact_sha256=artifact_sha256,etag=etag,last_modified=last_modified,source_published_at=source_published_at)
        obs_key = (stored_item.source_item_id,digest)
        created = obs_key not in self.observations
        if created:
            self.observations[obs_key] = SourceObservationRecord(len(self.observations)+1,stored_item.source_item_id,
                digest,state,deepcopy(metadata),artifact_sha256,source_url,etag,last_modified,
                source_published_at,detected_at,lease.job_id)
        obs = self.observations[obs_key]
        delivery_key = (lease.job_id,lease.generation,item_outcome_key,obs.source_observation_id)
        if delivery_key not in self.deliveries:
            self.deliveries[delivery_key] = SourceDeliveryRecord(len(self.deliveries)+1,stored_item.source_item_id,
                obs.source_observation_id,lease.job_id,lease.generation,item_outcome_key,detected_at)
        delivery = self.deliveries[delivery_key]
        source = StoredSourceObservation(stored_item,obs,created,delivery)
        self.current[stored_item.source_item_id] = delivery
        self.receipts[lease.job_id,receipt_key] = (request_identity_sha256,deepcopy(source))
        return source


def fixture(bodies=None):
    jobs, sources, artifacts = MemoryJobs(), MemorySources(), Repository()
    objects = ImmutableObjectStore(MemoryS3(), "explicit-synthetic-inventory")
    transport = RetainedTransport(bodies)
    handler = owner.SecFiniteInventoryHandler(edgar=transport,object_store=objects,artifacts=artifacts,
        observations=sources,jobs=jobs,clock=lambda:NOW)
    return SimpleNamespace(jobs=jobs,sources=sources,artifacts=artifacts,objects=objects,
        transport=transport,handler=handler)


def finish(case, context):
    checkpoint = case.handler(context)
    job = replace(case.jobs.jobs[context.job.job_id],status="succeeded",checkpoint=checkpoint)
    case.jobs.jobs[job.job_id] = job
    return case.handler.recover_completed(job), job


def test_full_finite_caller_preserves_all_rows_groups_orphans_unavailable_and_actual_children(monkeypatch):
    case = fixture()
    root, job = finish(case, case.jobs.context())
    assert (root.ticker_entry_count,root.issuer_group_count,root.index_row_count,root.resource_request_count) == (6,3,4,2)
    entries = case.handler._read_chunks(root.ticker_chunks)
    groups = case.handler._read_chunks(root.issuer_group_chunks)
    assert entries[0].pointer == "/slash~1~0" and entries[3].normalized_cik is None
    assert entries[0].interpretation == "AMBIGUOUS" and entries[1].interpretation == "USABLE_ISSUER_ASSOCIATION"
    assert groups[0].ticker_entry_ordinals == (0,1) and groups[0].assessment == "CIK_ASSOCIATION_ONLY"
    rows = case.handler._read_chunks(root.days[0].chunks)
    assert [row.physical_line for row in rows] == [4,6,7,8]
    assert [row.route for row in rows] == ["RESOURCE_V2","NONFINANCIAL_PREFIX","UNSUPPORTED_RESOURCE_ROUTE","RESOURCE_V2"]
    assert rows[0].filing_date == date(2026,9,27) and rows[-1].issuer_group_ordinal is None
    assert root.days[0].orphan_request_count == 1 and root.days[1].acquisition.source.state == "unavailable"
    assert root.days[1].row_count == 0 and root.days[1].chunks == ()
    assert root.source_authority_status == root.financial_status == root.listing_security_status == "NOT_ASSESSED"
    assert all(maximum == owner.MAX_BODY_BYTES for _,maximum in case.transport.calls)
    before = (case.sources.writes,len(case.jobs.jobs),deepcopy(case.sources.current))
    def forbidden(*args,**kwargs):
        pytest.fail("completed recovery performed acquisition or enqueue")
    monkeypatch.setattr(case.transport,"fetch",forbidden)
    monkeypatch.setattr(case.sources,"record_with_acquisition_receipt",forbidden)
    monkeypatch.setattr(case.jobs,"enqueue_child",forbidden)
    assert case.handler.recover_completed(job) == root
    assert before == (case.sources.writes,len(case.jobs.jobs),case.sources.current)


def test_parser_legacy_prefix_view_and_all_physical_rows_share_exact_validator():
    body = fixture_bodies()[day_url(date(2026,9,28))]
    all_rows = parse_all_form_index(body)
    assert tuple(row.filing for row in all_rows if row.filing.form.startswith(("10-K","10-Q","20-F","40-F","6-K"))) == parse_form_index(body)
    assert resource_route(all_rows[2].filing)[0] == "UNSUPPORTED_RESOURCE_ROUTE"
    with pytest.raises(ValueError,match="row ceiling"):
        parse_all_form_index(body,maximum_rows=3)
    with pytest.raises(ValueError,match="record admission"):
        parse_all_form_index(body,maximum_row_bytes=10)


@pytest.mark.parametrize("change", [
    {"request_revision":True},{"request_revision":1.0},{"expected_parent_release_id":True},
    {"expected_parent_release_id":1.0},{"extra":"unused"},{"producer_build_sha256":"A"*64},
    {"operation_key":"\0bad"},{"operation_at":"2026-10-09T12:00:00"},{"end_date":"2026-10-31"},
])
def test_request_strict_identity_unknown_fields_and_dates_before_any_execution(change):
    value = parameters()
    value.update(change)
    with pytest.raises(ValueError):
        owner.validate_parameters(value)


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":Infinity}',b'[]'])
def test_ticker_duplicate_nonfinite_or_nonobject_refuses(raw):
    with pytest.raises(ValueError):
        owner.ticker_records(raw)


def test_ticker_bool_float_malformed_and_many_same_issuer_records_never_choose_first():
    raw = json.dumps({"a":{"cik_str":True,"ticker":"T","title":"A"},
        "b":{"cik_str":1.0,"ticker":"T","title":"A"},
        **{str(i):{"cik_str":42,"ticker":"T"+str(i),"title":"A"} for i in range(20)}}).encode()
    entries, groups = owner.ticker_records(raw)
    assert entries[0].normalized_cik is entries[1].normalized_cik is None
    assert len(entries) == 22 and len(groups) == 1 and groups[0].ticker_entry_ordinals == tuple(range(2,22))


@pytest.mark.parametrize("boundary", ["raw-upload","ticker-receipt","filing-receipt","child-commit","chunk-store","root-store"])
def test_interruption_recovers_original_receipts_and_children_without_repeat_http(monkeypatch,boundary):
    case = fixture()
    context = case.jobs.context()
    fired = [False]
    if boundary in {"raw-upload","chunk-store","root-store"}:
        import screener.artifacts as artifacts_owner
        target = case.objects
        original = target.put_verified
        def interrupt(data,media_type):
            result = original(data,media_type)
            trigger = (boundary == "raw-upload" and data == case.transport.bodies[TICKER_MAP_URL]
                or boundary == "chunk-store" and b'SEC_INVENTORY_CHUNK1' in data
                or boundary == "root-store" and b'SEC_INVENTORY_ENUMERATION1' in data)
            if trigger and not fired[0]:
                fired[0] = True
                raise RuntimeError("synthetic interruption")
            return result
        monkeypatch.setattr(target,"put_verified",interrupt)
    elif boundary in {"ticker-receipt","filing-receipt"}:
        original = case.sources.record_with_acquisition_receipt
        def interrupt(*args,**kwargs):
            result = original(*args,**kwargs)
            trigger = kwargs["receipt_key"] == "sec-finite:ticker-map" if boundary=="ticker-receipt" else kwargs["receipt_key"].startswith("sec-finite:filing:")
            if trigger and not fired[0]:
                fired[0] = True
                raise RuntimeError("synthetic interruption")
            return result
        monkeypatch.setattr(case.sources,"record_with_acquisition_receipt",interrupt)
    else:
        original = case.jobs.enqueue_child
        def interrupt(*args,**kwargs):
            result = original(*args,**kwargs)
            if not fired[0]:
                fired[0] = True
                raise RuntimeError("synthetic interruption")
            return result
        monkeypatch.setattr(case.jobs,"enqueue_child",interrupt)
    with pytest.raises(RuntimeError,match="synthetic interruption"):
        case.handler(context)
    assert fired[0]
    previous_receipts = deepcopy(case.sources.receipts)
    previous_calls = list(case.transport.calls)
    restarted = case.jobs.context(generation=2)
    root, job = finish(case,restarted)
    assert root.resource_request_count == 2
    for identity,(digest,source) in previous_receipts.items():
        assert case.sources.receipts[identity][1].delivery == source.delivery
    if boundary != "raw-upload":
        assert case.transport.calls.count((TICKER_MAP_URL,owner.MAX_BODY_BYTES)) == 1
    else:
        assert case.transport.calls.count((TICKER_MAP_URL,owner.MAX_BODY_BYTES)) == 2
    assert len([j for j in case.jobs.jobs.values() if j.kind=="sec-resource-fetch-v2"]) == 2


def test_later_equal_bytes_occurrence_checks_again_and_retains_new_source_deliveries():
    case = fixture()
    first, _ = finish(case,case.jobs.context(key="first"))
    second, _ = finish(case,case.jobs.context(key="second"))
    assert first.listing.source.observation_sha256 == second.listing.source.observation_sha256
    assert first.listing.source.delivery.occurrence_id != second.listing.source.delivery.occurrence_id
    assert first.listing.source.delivery.job_id != second.listing.source.delivery.job_id
    assert len([j for j in case.jobs.jobs.values() if j.kind=="sec-resource-fetch-v2"]) == 4


@pytest.mark.parametrize("ceiling", ["requests","encoded","chunks"])
def test_whole_day_frontier_refuses_before_any_filing_or_child_fanout(monkeypatch,ceiling):
    case = fixture()
    context = case.jobs.context()
    original = owner.day_frontier
    def bound(acquired,records,**kwargs):
        if ceiling == "requests":
            kwargs["remaining_requests"] = 1
        elif ceiling == "encoded":
            kwargs["used_bytes"] = owner.MAX_TOTAL_BYTES
        else:
            kwargs["remaining_chunks"] = 0
        return original(acquired,records,**kwargs)
    monkeypatch.setattr(owner,"day_frontier",bound)
    with pytest.raises(ValueError,match="frontier|before filing"):
        case.handler(context)
    assert not any(item.item_kind=="filing" for item in case.sources.items.values())
    assert not any(job.kind=="sec-resource-fetch-v2" for job in case.jobs.jobs.values())
    assert any(key=="sec-finite:day:2026-09-28" for _,key in case.sources.receipts)


def test_utf8_entry_bound_precedes_ticker_factory_and_does_not_truncate(monkeypatch):
    raw = owner.encode({"too-big":{"cik_str":42,"ticker":"OK","title":"😀"*40000},"other":{"cik_str":43,"ticker":"B","title":"B"}})
    monkeypatch.setattr(owner,"TickerEntry",lambda **kwargs:pytest.fail("oversized entry reached model factory"))
    with pytest.raises(ValueError,match="entry exceeds"):
        owner.ticker_records(raw)


def test_recorded_child_mutation_and_changed_parent_build_request_refuse(monkeypatch):
    case = fixture()
    root, job = finish(case,case.jobs.context())
    child = case.handler._read_chunks(root.days[0].chunks)[0].request.child
    original = case.jobs.jobs[child.job_id]
    case.jobs.jobs[child.job_id] = replace(original,parameters={**original.parameters,"observation_id":999999})
    with pytest.raises(ValueError,match="child immutable"):
        case.handler.recover_completed(job)
    case.jobs.jobs[child.job_id] = original
    for key,value in (("expected_parent_release_id",7),("producer_build_sha256","b"*64)):
        forged = replace(job,parameters={**job.parameters,key:value})
        with pytest.raises(ValueError):
            case.handler.recover_completed(forged)


def test_same_bytes_changed_and_missing_http_media_type_survive_exact_receipt_recovery(monkeypatch):
    case = fixture()
    first, first_job = finish(case, case.jobs.context(key="original-header"))
    artifact = case.artifacts.get(first.listing.source.artifact_sha256)
    case.transport.override_media_type = True
    case.transport.media_type = "application/vnd.synthetic.changed+json"
    changed, changed_job = finish(case, case.jobs.context(key="changed-header"))
    case.transport.media_type = None
    missing, missing_job = finish(case, case.jobs.context(key="missing-header"))
    assert first.listing.source.artifact_sha256 == changed.listing.source.artifact_sha256 == missing.listing.source.artifact_sha256
    assert case.artifacts.get(first.listing.source.artifact_sha256).media_type == artifact.media_type == "application/json"
    assert changed.listing.media_type == "application/vnd.synthetic.changed+json"
    assert missing.listing.media_type is None and missing.listing.headers_captured is True
    assert missing.days[1].acquisition.headers_captured is False
    assert len({root.listing.source.observation_sha256 for root in (first,changed,missing)}) == 3
    for root in (changed,missing):
        day = root.days[0]
        assert day.acquisition.media_type == root.listing.media_type
        rows = case.handler._read_chunks(day.chunks)
        assert rows[0].request.filing.media_type == root.listing.media_type
    monkeypatch.setattr(case.transport,"fetch",lambda *a,**kw:pytest.fail("media replay fetched again"))
    assert case.handler.recover_completed(first_job) == first
    assert case.handler.recover_completed(changed_job) == changed
    assert case.handler.recover_completed(missing_job) == missing


@pytest.mark.parametrize("part", ["listing", "day"])
def test_completed_counterfeit_logical_acquisition_receipt_refuses_without_mutable_calls(monkeypatch, part):
    case = fixture()
    context = case.jobs.context()
    checkpoint = owner.load(owner.InventoryCheckpoint, owner.encode(case.handler(context)))
    request, digest = case.handler._actual_request(context.job)[1:]
    original = checkpoint.listing if part == "listing" else checkpoint.days[0].acquisition
    stored = case.sources.acquisition_receipt(producer_job_id=context.job.job_id,
        receipt_key=original.receipt_key, request_identity_sha256=digest)
    item, obs = stored.item, stored.observation
    wrong_key = "sec-finite:alias:" + part
    actual_wrong = case.sources.record_with_acquisition_receipt(context.lease, receipt_key=wrong_key,
        request_identity_sha256=digest, item=SourceItemIdentity(item.source_system,item.item_kind,item.source_key,
            item.issuer_source_identifier,item.parent_source_item_id), item_outcome_key=stored.delivery.item_key,
        state=obs.state,metadata=obs.canonical_metadata,source_url=obs.source_url,
        artifact_sha256=obs.artifact_sha256,etag=obs.etag,last_modified=obs.last_modified,detected_at=NOW)
    wrong_ref = case.handler._source_ref(wrong_key, actual_wrong)
    if part == "listing":
        checkpoint = checkpoint.model_copy(update={"listing":wrong_ref})
    else:
        days = (checkpoint.days[0].model_copy(update={"acquisition":wrong_ref}), *checkpoint.days[1:])
        checkpoint = checkpoint.model_copy(update={"days":days})
    root = case.handler._root(checkpoint,request,4,2)
    from screener.artifacts import store_evidence
    artifact = store_evidence(owner.encode(root.model_dump(mode="json")),owner.MEDIA_TYPE,case.objects,case.artifacts)
    checkpoint = checkpoint.model_copy(update={"root":owner.ArtifactRef(artifact_sha256=artifact.content_sha256,byte_size=artifact.byte_size)})
    job = replace(case.jobs.jobs[context.job.job_id],status="succeeded",checkpoint=checkpoint.model_dump(mode="json"))
    case.jobs.jobs[job.job_id] = job
    def forbidden(*a,**kw):pytest.fail("counterfeit completed read performed mutable/source path")
    monkeypatch.setattr(case.transport,"fetch",forbidden)
    monkeypatch.setattr(case.sources,"record_with_acquisition_receipt",forbidden)
    monkeypatch.setattr(case.jobs,"enqueue_child",forbidden)
    with pytest.raises(ValueError,match="listing receipt|daily acquisition"):
        case.handler.recover_completed(job)



def test_fixed_scalar_allowance_covers_bigint_widths_headers_and_actual_returned_records():
    # Quoted width markers overestimate numbers. This dictionary cannot validate as IDs.
    width = "9" * 19
    timestamp = "9999-12-31T23:59:59.999999+00:00"
    source = dict(observation_sha256="h"*64,item_kind="filing",source_key="a"*20,
        parent_source_item_id=None,role=None,artifact_sha256="h"*64,artifact_byte_size="9"*7,
        captured_source=None,source_url="",state="present",delivery=dict(occurrence_id=width,
            source_item_id=width,source_observation_id=width,job_id=width,ownership_generation=width,
            item_key="filing:"+"a"*20+":"+"h"*64,checked_at=timestamp))
    filing = dict(receipt_key="sec-finite:filing:9999-12-31:99999",source_system="SEC",
        issuer_source_identifier="9"*10,source=source,etag="",last_modified="",media_type="",headers_captured=True)
    child = dict(job_id=width,occurrence_id=width,
        schedule_key="sec-resource-fetch-v2:occurrence:"+width+":"+"9"*10+":"+"a"*20+":"+"h"*64,
        scheduled_for=timestamp,kind="sec-resource-fetch-v2",parameters=dict(accession="a"*20,
            cik="9"*10,form="10-QT/A",filename="",observation_id=width),
        due_at=timestamp,priority="-2147483648",max_attempts="3",deadline_at=None)
    admission = dict(capture_revision="2",filing=filing,child=child)
    assert set(admission)==set(owner.AdmissionRequest.model_fields)
    assert set(filing)==set(owner.AcquisitionRef.model_fields)
    assert set(source)==set(owner.SourceRef.model_fields)
    assert set(source["delivery"])==set(owner.SourceRef.model_fields["delivery"].annotation.model_fields)
    assert set(child)==set(owner.ChildRef.model_fields)
    assert set(child["parameters"])==set(owner.ResourceParameters.model_fields)
    fixed_bytes = len(owner.encode(admission))
    assert fixed_bytes <= owner.REQUEST_FIXED_BYTES
    case = fixture()
    root, _ = finish(case, case.jobs.context())
    rows = case.handler._read_chunks(root.days[0].chunks)
    for row in rows:
        if row.request is None:
            continue
        actual = row.request
        variable = dict(source_url=actual.filing.source.source_url,etag=actual.filing.etag,
            last_modified=actual.filing.last_modified,media_type=actual.filing.media_type,
            filename=actual.child.parameters.filename)
        assert len(owner.encode(actual.model_dump(mode="json"))) <= fixed_bytes + len(owner.encode(variable))
