"""Public opt-in continuation over synthetic transport and in-memory evidence owners."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import timedelta, timezone
import hashlib
import threading
from types import SimpleNamespace as NS

import pytest

from screener.artifacts import EvidenceArtifact
from screener.durable_jobs import LeaseToken
from screener.job_runtime import JobContext
from screener.object_store import VerifiedObject, ObjectVerificationError
from screener.sec_ingestion import SecResourceFetchHandler, SecResourcePending, SecDiscoveryStopped
from screener.source_observations import (SourceAcquisitionReceipt, StoredSourceObservation,
    SourceItemRecord, SourceObservationRecord, observation_sha256, _canonical_url, _optional_text)
from screener.source_validity import SourceDeliveryRecord
from screener.sources.edgar import EdgarTransportResult, NoXbrlDataError
from test_sec_completed_capture import Fixture as CompletedFixture
from test_sec_ingestion import NOW


class Fixture(CompletedFixture):
    def __init__(self):
        super().__init__()
        self.job = replace(self.job, checkpoint=None, status="running", ownership_generation=1)
        self.receipts, self.fetches, self.writes, self.children, self.checkpoints = {}, [], [], [], []
        self.diagnostics, self.next_id, self.crash_key = [], 100, None
        from screener.sec_ingestion import _root_resources
        self.resources = _root_resources(**{key:self.job.parameters[key] for key in ("accession", "cik", "filename")})
        self.responses = {r.url:EdgarTransportResult(self.raw[r.role], r.url, None, '"original"', None)
                          for r in self.resources}
        self.handler = SecResourceFetchHandler(edgar=self, object_store=self, artifacts=self,
            observations=self, jobs=self, clock=lambda: NOW, annotate_annual=True, retain_partial_acquisitions=True)

    def context(self, generation=1):
        return JobContext(replace(self.job, ownership_generation=generation), threading.Event(), self,
                          LeaseToken(self.job.job_id, "worker", generation), lambda: NOW)

    def latest_sec_filing(self, observation_id):
        assert observation_id == self.parent.observation.source_observation_id
        self.events.append("latest-parent")
        return deepcopy(self.parent)

    def fetch(self, url, *, maximum_bytes):
        self.fetches.append((url, maximum_bytes))
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    def put_verified(self, raw, media_type):
        digest = hashlib.sha256(raw).hexdigest()
        self.body_rows[digest] = raw
        return VerifiedObject(digest, "raw/sha256/"+digest, len(raw), NOW)

    def add_verified(self, value, media_type):
        artifact = EvidenceArtifact(value.content_sha256, value.object_key, value.byte_size, media_type, NOW, NOW)
        return self.artifact_rows.setdefault(value.content_sha256, artifact)

    def record_with_acquisition_receipt(self, lease, **value):
        key, request = value["receipt_key"], value["request_identity_sha256"]
        assert key not in self.receipts
        self.writes.append(key)
        ident = value["item"]
        self.next_id += 1
        item = SourceItemRecord(self.next_id, **asdict(ident), created_at=NOW)
        md = deepcopy(value["metadata"])
        canonical = dict(state=value["state"], metadata=md, artifact_sha256=value["artifact_sha256"],
            source_url=_canonical_url(value["source_url"]), etag=_optional_text("etag", value["etag"]),
            last_modified=_optional_text("last_modified", value["last_modified"]), source_published_at=None)
        digest = observation_sha256(**canonical)
        obs = SourceObservationRecord(self.next_id+100, item.source_item_id, digest, value["state"], md,
            canonical["artifact_sha256"], canonical["source_url"], canonical["etag"], canonical["last_modified"],
            None, NOW, lease.job_id)
        delivery = SourceDeliveryRecord(self.next_id+200, item.source_item_id, obs.source_observation_id,
                                        lease.job_id, lease.generation, value["item_outcome_key"], NOW)
        stored = StoredSourceObservation(item, obs, True, delivery)
        receipt = SourceAcquisitionReceipt(lease.job_id, request, key, ident, digest, delivery).to_outcome()
        self.receipts[key] = (request, stored, receipt)
        if self.crash_key == key:
            self.crash_key = None
            raise RuntimeError("after atomic receipt commit")
        return deepcopy(stored)

    def acquisition_receipt(self, *, producer_job_id, receipt_key, request_identity_sha256):
        entry = self.receipts.get(receipt_key)
        if entry is None:
            return None
        request, stored, outcome = entry
        receipt = SourceAcquisitionReceipt.from_outcome(outcome)
        if (request != request_identity_sha256 or receipt.producer_job_id != producer_job_id
                or receipt.delivery != stored.delivery):
            raise ValueError("immutable acquisition differs")
        return deepcopy(stored)

    def enqueue_child(self, lease, **request):
        self.children.append(deepcopy(request))

    def save_checkpoint(self, lease, checkpoint, **kwargs):
        self.checkpoints.append(deepcopy(checkpoint))
        return replace(self.job, checkpoint=checkpoint)

    def record_item_outcome(self, lease, **value):
        self.diagnostics.append(deepcopy(value))

    def run(self, generation=1):
        return self.handler(self.context(generation))


def test_committed_ready_survives_reclaim_without_replacing_original_response():
    f = Fixture()
    f.crash_key = "sec-resource-partial:1:ready:complete_submission"
    with pytest.raises(RuntimeError, match="atomic receipt"):
        f.run()
    original = f.receipts[f.writes[0]][1]
    f.responses[f.resources[0].url] = NoXbrlDataError("later remote absence")
    checkpoint = f.run(2)
    assert len(f.fetches) == len(f.writes) == 4
    assert checkpoint["roots"]["complete_submission"] == original.observation.observation_sha256
    assert checkpoint["root_deliveries"]["complete_submission"] == asdict(original.delivery) | {
        "checked_at":NOW.isoformat()}
    assert checkpoint["root_deliveries"]["complete_submission"]["ownership_generation"] == 1
    assert f.checkpoints == [checkpoint] and len(f.children) == 1


def test_two_actual_producer_requests_keep_same_hash_owner_response_and_distinct_deliveries():
    first, second = Fixture(), Fixture()
    second.job = replace(second.job, job_id=17, occurrence_id=18)
    one, two = first.run(), second.run()
    assert one["roots"] == two["roots"]
    for key in first.receipts:
        request1, stored1, receipt1 = first.receipts[key]
        request2, stored2, receipt2 = second.receipts[key]
        assert request1 != request2
        assert stored1.observation.observation_sha256 == stored2.observation.observation_sha256
        assert stored1.observation.canonical_metadata == stored2.observation.canonical_metadata
        assert "request_identity_sha256" not in stored1.observation.canonical_metadata["partial_acquisition"]
        assert receipt1["delivery"]["job_id"] == 7 and receipt2["delivery"]["job_id"] == 17
        assert receipt1["request_identity_sha256"] == request1
        assert receipt2["request_identity_sha256"] == request2


def test_identical_failures_across_actual_generations_keep_source_digest_but_new_delivery():
    f = Fixture()
    f.responses[f.resources[0].url] = NoXbrlDataError("same original absence")
    for generation in (1, 2):
        with pytest.raises(SecResourcePending): f.run(generation)
    first = f.receipts["sec-resource-partial:1:failure:complete_submission:1"]
    second = f.receipts["sec-resource-partial:1:failure:complete_submission:2"]
    assert first[0] == second[0]
    assert first[1].observation.observation_sha256 == second[1].observation.observation_sha256
    assert first[2]["delivery"]["occurrence_id"] != second[2]["delivery"]["occurrence_id"]
    assert first[2]["delivery"]["ownership_generation"] == 1
    assert second[2]["delivery"]["ownership_generation"] == 2


def test_registered_recovery_cap_refuses_before_body_read_without_refetch():
    f = Fixture()
    f.crash_key = "sec-resource-partial:1:ready:complete_submission"
    with pytest.raises(RuntimeError): f.run()
    stored = f.receipts[f.writes[0]][1]
    digest = stored.observation.artifact_sha256
    f.artifact_rows[digest] = replace(f.artifact_rows[digest], byte_size=8*1024*1024+1)
    f.events.clear()
    with pytest.raises(ValueError, match="artifact metadata"): f.run(2)
    assert "body" not in f.events and len(f.fetches) == len(f.writes) == 1


@pytest.mark.parametrize("fault", ["not_found", "structure"])
def test_failed_slot_replays_same_generation_then_new_claim_progresses(fault):
    f = Fixture()
    resource = f.resources[2]
    f.responses[resource.url] = (NoXbrlDataError("original404") if fault == "not_found" else
        EdgarTransportResult(b'{"cik":1234,"filings":{"recent":{"accessionNumber":[],"form":[]},"files":[]}}',
                             resource.url, "application/json", None, None))
    with pytest.raises(SecResourcePending) as first:
        f.run()
    key = "sec-resource-partial:1:failure:submissions:1"
    stored = f.receipts[key][1]
    before = (len(f.fetches), len(f.writes), deepcopy(f.receipts))
    f.responses[resource.url] = EdgarTransportResult(f.raw["submissions"], resource.url, None, None, None)
    with pytest.raises(SecResourcePending) as replay:
        f.run()
    assert str(first.value) == str(replay.value)
    assert (len(f.fetches), len(f.writes), f.receipts) == before
    phase = stored.observation.canonical_metadata["partial_acquisition"]
    assert phase["phase"] == "FAILED_PENDING"
    if fault == "not_found":
        assert stored.observation.state == "pending" and stored.observation.artifact_sha256 is None
        assert phase["http_headers_captured"] is False
        assert all(phase[n] is None for n in ("http_response_url", "http_media_type", "http_etag", "http_last_modified"))
        assert str(replay.value.__cause__) == "original404"
    else:
        assert stored.observation.state == "present" and stored.observation.artifact_sha256 in f.body_rows
        assert phase["http_media_type"] == "application/json"
    checkpoint = f.run(2)
    assert checkpoint["root_deliveries"]["complete_submission"]["ownership_generation"] == 1
    assert checkpoint["root_deliveries"]["submissions"]["ownership_generation"] == 2
    assert key in f.receipts and f.checkpoints == [checkpoint]


def test_utc_original_delivery_and_nullable_raw_headers_give_same_child_after_interruption():
    f = Fixture()
    first = f.run()
    child = deepcopy(f.children[0])
    for key, (request, stored, outcome) in list(f.receipts.items()):
        delivery = replace(stored.delivery, checked_at=NOW.astimezone(timezone(timedelta(hours=3))))
        f.receipts[key] = request, replace(stored, delivery=delivery), outcome
    replay = f.run(2)
    assert first == replay and f.children[-1] == child
    assert len(f.fetches) == len(f.writes) == 4
    assert all(d["checked_at"].endswith("+00:00") for d in replay["root_deliveries"].values())
    for _, stored, _ in f.receipts.values():
        assert stored.observation.canonical_metadata["partial_acquisition"]["http_media_type"] is None
        assert f.artifact_rows[stored.observation.artifact_sha256].media_type == "application/first-registration"


@pytest.mark.parametrize("field,value", [("job_id", True), ("occurrence_id", None),
    ("occurrence_id", 2**63), ("ownership_generation", 0), ("priority", 1.0),
    ("max_attempts", True), ("kind", "sec-candidate-stage"), ("due_at", NOW.replace(tzinfo=None))],
    ids=["job-bool", "no-occurrence", "big-occurrence", "zero-generation", "priority-float",
         "attempts-bool", "kind", "naive"])
def test_runtime_identity_refuses_before_any_owner(field, value):
    f = Fixture()
    context = f.context()
    context = replace(context, job=replace(context.job, **{field:value}))
    f.events.clear()
    with pytest.raises(ValueError):
        f.handler(context)
    assert f.events == [] and not f.fetches and not f.writes


@pytest.mark.parametrize("field", ["due_at", "priority", "max_attempts", "deadline_at"])
def test_changed_immutable_request_cannot_replace_ready_slot(field):
    f = Fixture()
    f.crash_key = "sec-resource-partial:1:ready:complete_submission"
    with pytest.raises(RuntimeError): f.run()
    f.job = replace(f.job, **{field:NOW+timedelta(seconds=1) if field.endswith("at") else 2})
    with pytest.raises(ValueError, match="immutable acquisition"):
        f.run(2)
    assert len(f.fetches) == len(f.writes) == 1


@pytest.mark.parametrize("change", ["phase", "revision", "parent", "raw_headers", "delivery", "missing", "corrupt"])
def test_retained_contradiction_or_corruption_cannot_refetch(change):
    f = Fixture()
    f.crash_key = "sec-resource-partial:1:ready:complete_submission"
    with pytest.raises(RuntimeError): f.run()
    key = f.writes[0]
    request, stored, receipt = f.receipts[key]
    md = stored.observation.canonical_metadata
    if change == "phase": md["partial_acquisition"]["phase"] = "FAILED_PENDING"
    elif change == "revision": md["partial_acquisition"]["revision"] = True
    elif change == "parent": md["parent_observation_id"] = True
    elif change == "raw_headers": md["partial_acquisition"]["http_etag"] = "different"
    elif change == "delivery":
        f.receipts[key] = request, replace(stored, delivery=replace(stored.delivery, job_id=99)), receipt
    elif change == "missing": del f.body_rows[stored.observation.artifact_sha256]
    else: f.body_rows[stored.observation.artifact_sha256] = b"corrupt"
    with pytest.raises((ValueError, KeyError, ObjectVerificationError)):
        f.run(2)
    assert len(f.fetches) == len(f.writes) == 1


def test_current_ready_validation_refuses_without_rewriting_original_receipt(monkeypatch):
    f = Fixture()
    f.run()
    before = deepcopy(f.receipts)
    import screener.sec_ingestion as owner
    def now_pending(*args): raise SecResourcePending("new validator pending")
    monkeypatch.setattr(owner, "_validate_submissions", now_pending)
    with pytest.raises(SecResourcePending, match="new validator"):
        f.run(2)
    assert f.receipts == before and len(f.fetches) == len(f.writes) == 4


@pytest.mark.parametrize("limit", ["body", "text", "metadata", "failure"])
def test_frontiers_refuse_before_source_or_artifact_write(limit):
    f = Fixture()
    resource = f.resources[0]
    if limit == "failure": result = NoXbrlDataError("x"*2001)
    else:
        result = EdgarTransportResult(b"x"*(8*1024*1024+1) if limit == "body" else b"raw",
            resource.url, "x"*(65537 if limit == "text" else 33000) if limit in {"text", "metadata"} else None,
            "y"*33000 if limit == "metadata" else None, None)
    f.responses[resource.url] = result
    before = deepcopy(f.artifact_rows)
    with pytest.raises(ValueError): f.run()
    assert not f.writes and not f.receipts and f.artifact_rows == before


@pytest.mark.parametrize("kind", ["json", "pending_subclass", "not_found_subclass"])
def test_unclassified_errors_propagate_without_receipt(kind):
    f = Fixture()
    if kind == "json":
        f.responses[f.resources[1].url] = EdgarTransportResult(b"malformed", f.resources[1].url, None, None, None)
        expected = ValueError
    elif kind == "not_found_subclass":
        class Other(NoXbrlDataError): pass
        f.responses[f.resources[0].url] = Other("not the exact contract")
        expected = Other
    else:
        class Other(SecResourcePending): pass
        from dataclasses import replace as change
        f.resources = (change(f.resources[0], validator=lambda *a: (_ for _ in ()).throw(Other("other"))),) + f.resources[1:]
        import screener.sec_ingestion as owner
        from unittest.mock import patch
        with patch.object(owner, "_root_resources", return_value=f.resources):
            with pytest.raises(Other): f.run()
        assert not f.writes
        return
    with pytest.raises(expected): f.run()
    assert not any(":failure:" in key for key in f.receipts)


def test_stop_and_latest_parent_rejection_precede_receipt_reuse():
    f = Fixture()
    ctx = f.context()
    ctx.stopping.set()
    with pytest.raises(SecDiscoveryStopped): f.handler(ctx)
    assert not f.fetches and not f.writes
    f.parent.observation.state = "removed"
    with pytest.raises(ValueError, match="not present"): f.run()
    assert not f.fetches


@pytest.mark.parametrize("value", [None, 0, 1, "yes"])
def test_opt_in_requires_exact_bool_and_capture2(value):
    f = Fixture()
    with pytest.raises(ValueError):
        SecResourceFetchHandler(edgar=f, object_store=f, artifacts=f, observations=f, jobs=f,
                                annotate_annual=True, retain_partial_acquisitions=value)
    with pytest.raises(ValueError):
        SecResourceFetchHandler(edgar=f, object_store=f, artifacts=f, observations=f, jobs=f,
                                retain_partial_acquisitions=True)
