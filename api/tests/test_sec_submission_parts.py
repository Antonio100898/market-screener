"""Source registration controls through public handlers and memory repositories."""
from contextlib import nullcontext
from dataclasses import replace
import hashlib
import json
from threading import Event
from types import SimpleNamespace
from datetime import timedelta

import pytest

from screener import sec_ingestion
from screener.artifacts import store_evidence
from screener.durable_jobs import DurableJobRepository, LeaseLost, LeaseToken
from screener.job_runtime import JobContext
from screener.source_observations import (
    SourceAcquisitionReceipt, SourceItemIdentity, SourceItemRecord, SourceObservationRecord,
    StoredSourceObservation, SourceObservationRepository, SourceObservationIdentityConflict, observation_sha256,
)
from screener.source_validity import SourceDeliveryRecord
from screener.sources.edgar import EdgarTransportResult
from test_sec_ingestion import NOW, RootObjectStore, RootArtifacts, _job, _root_payloads
from test_sec_submission_documents import _document, _submission


class _MemoryEngine:
    def connect(self):
        return nullcontext(self)
    def begin(self):
        return nullcontext(self)


class _MemoryJobs:
    def __init__(self, engine, events):
        self.engine, self.events = engine, events
        self.rows, self.associations, self.receipts, self.children = {}, {}, {}, []
    def scheduled_job(self, *, job_id, occurrence_id, schedule_key, scheduled_for, _raw_work_budget=None):
        self.events.append(("scheduled", job_id))
        assert self.associations[job_id] == (occurrence_id, schedule_key, scheduled_for)
        return self.rows[job_id]
    def _item_outcome_on_connection(self, connection, job_id, item_key, *, _raw_work_budget=None):
        return self.receipts.get((job_id, item_key))
    def _lock_live_lease(self, connection, lease, now, *, _raw_work_budget=None):
        row = self.rows[lease.job_id]
        current = now() if callable(now) else now
        if (row.status != "running" or row.owner != lease.owner
                or row.ownership_generation != lease.generation or row.lease_expires_at <= current):
            raise LeaseLost("unit lease is no longer live")
        return vars(row)
    def save_checkpoint(self, lease, checkpoint, *, now, _raw_work_budget=None):
        self._lock_live_lease(self.engine, lease, now)
        self.events.append(("checkpoint", lease.job_id))
        self.rows[lease.job_id] = replace(self.rows[lease.job_id], checkpoint=checkpoint)
        return self.rows[lease.job_id]
    def enqueue_child(self, lease, **values):
        self.children.append(values)
        return SimpleNamespace(**values)
    def record_item_outcome(self, lease, *, item_key, status, outcome, error_summary, now):
        self._lock_live_lease(self.engine, lease, now)
        self.events.append(("item_outcome", item_key, status, error_summary))
        return SimpleNamespace(item_key=item_key, status=status, outcome=outcome, error_summary=error_summary)


class _MemoryObjects(RootObjectStore):
    def __init__(self, events):
        super().__init__()
        self.events, self.data = events, {}
    def put_verified(self, data, media_type):
        result = super().put_verified(data, media_type)
        self.data[result.content_sha256] = bytes(data)
        self.events.append(("put", result.content_sha256))
        return result
    def read_verified(self, object_key, digest, byte_size):
        self.events.append(("body", digest))
        data = self.data[digest]
        assert object_key == "raw/sha256/" + digest
        assert len(data) == byte_size and hashlib.sha256(data).hexdigest() == digest
        return data


class _MemoryArtifacts(RootArtifacts):
    def __init__(self, engine, events):
        super().__init__()
        self.engine, self.events, self.records = engine, events, {}
    def add_verified(self, verified, media_type):
        self.events.append(("artifact", verified.content_sha256))
        if verified.content_sha256 not in self.records:
            self.records[verified.content_sha256] = super().add_verified(verified, media_type)
        return self.records[verified.content_sha256]
    def get(self, digest):
        return self.records[digest]


class _MemorySources:
    def __init__(self, engine, jobs, events):
        self.engine, self._jobs, self.events = engine, jobs, events
        self.records, self.items, self.deliveries = {}, {}, {}
        self._clock = lambda: NOW
        self.parent = None
    def by_sha256(self, digest, *, _raw_work_budget=None):
        return replace(self.records[digest], delivery=None)
    def latest_sec_filing(self, observation_id, *, _raw_work_budget=None):
        assert self.parent.observation.source_observation_id == observation_id
        return replace(self.parent, delivery=None)
    def delivery(self, occurrence_id, *, source_item_id, source_observation_id):
        return self.deliveries[(occurrence_id, source_item_id, source_observation_id)]
    def acquisition_receipt(self, *, producer_job_id, receipt_key, request_identity_sha256, _raw_work_budget=None):
        row = self._jobs.receipts.get((producer_job_id, sec_ingestion._SOURCE_RECEIPT_PREFIX + receipt_key))
        if row is None:
            return None
        receipt = SourceAcquisitionReceipt.from_outcome(row["outcome"], _raw_work_budget=_raw_work_budget)
        assert receipt.request_identity_sha256 == request_identity_sha256
        stored = self.records[receipt.observation_sha256]
        if _raw_work_budget is not None:
            _raw_work_budget.receipt_observation(stored.observation)
        assert stored.delivery == receipt.delivery
        return stored
    def record_with_acquisition_receipt(self, lease, *, receipt_key, request_identity_sha256,
            item, item_outcome_key, state, metadata, source_url, artifact_sha256,
            detected_at, etag=None, last_modified=None, source_published_at=None, _raw_work_budget=None):
        self._jobs._lock_live_lease(self.engine, lease, self._clock)
        identity = tuple(vars(item).values())
        if identity not in self.items:
            self.items[identity] = SourceItemRecord(len(self.items) + 1, *identity, NOW)
        actual_item = self.items[identity]
        metadata = json.loads(json.dumps(metadata))
        digest = observation_sha256(state=state, metadata=metadata, source_url=source_url,
            artifact_sha256=artifact_sha256, etag=etag, last_modified=last_modified,
            source_published_at=source_published_at)
        observation_id = len(self.records) + 1
        observation = SourceObservationRecord(observation_id, actual_item.source_item_id, digest,
            state, metadata, artifact_sha256, source_url, etag, last_modified, source_published_at,
            detected_at, lease.job_id)
        delivery = SourceDeliveryRecord(len(self.deliveries) + 1, actual_item.source_item_id,
            observation_id, lease.job_id, lease.generation, item_outcome_key, NOW)
        stored = StoredSourceObservation(actual_item, observation, True, delivery)
        self.records[digest] = stored
        self.deliveries[(delivery.occurrence_id, actual_item.source_item_id, observation_id)] = delivery
        receipt = SourceAcquisitionReceipt(lease.job_id, request_identity_sha256, receipt_key, item, digest, delivery)
        self._jobs.receipts[(lease.job_id, sec_ingestion._SOURCE_RECEIPT_PREFIX + receipt_key)] = dict(
            status="succeeded", error_summary=None, attempts=1, outcome=receipt.to_outcome())
        self.events.append(("source", actual_item.source_key))
        return stored


@pytest.fixture
def submission_parts_flow():
    def build(*, headers=None, response_padding=0):
        accession, cik, filename = "0000000001-26-000001", "0000000001", "edgar/data/1/0000000001-26-000001.txt"
        primary = "annual.htm"
        names = (primary, "R1.htm", "R2.htm", "R3.htm", "annual_htm.xml", "FilingSummary.xml", "issuer.xsd", "issuer_pre.xml")
        documents = []
        for sequence, name in enumerate(names, 1):
            document = _document(name=name, sequence=sequence, payload=b"<row>" + name.encode() + b"</row>\n")
            values = (headers or {}).get(name, {})
            if "type" in values:
                document = document.replace(b"<TYPE>XML", b"<TYPE>" + values["type"].encode())
            if "description" in values:
                document = document.replace(b"<DESCRIPTION>labelled synthetic part", b"<DESCRIPTION>" + values["description"].encode())
            documents.append(document)
        container = _submission(*documents)
        inventory = dict(directory=dict(name="/Archives/edgar/data/1/" + accession.replace("-", ""),
            item=[dict(name=name) for name in names]))
        submissions = dict(cik=1, filings=dict(recent=dict(accessionNumber=[accession], form=["10-K"],
            filingDate=[NOW.date().isoformat()], reportDate=["2025-12-31"], primaryDocument=[primary])))
        events, engine = [], _MemoryEngine()
        jobs = _MemoryJobs(engine, events)
        objects, artifacts = _MemoryObjects(events), _MemoryArtifacts(engine, events)
        sources = _MemorySources(engine, jobs, events)
        def add_job(identity, kind, parameters, key):
            job = replace(_job(parameters, kind=kind), job_id=identity, occurrence_id=identity,
                          lease_expires_at=NOW + timedelta(minutes=5))
            jobs.rows[identity] = job
            jobs.associations[identity] = (identity, key, NOW)
            return JobContext(job, Event(), jobs, LeaseToken(identity, "worker", 1), lambda: NOW)
        filing_context = add_job(2, "sec-recent-discovery", {}, "synthetic-index")
        filing_artifact = store_evidence(b"synthetic official filing index\n", "text/plain", objects, artifacts)
        parent = sources.record_with_acquisition_receipt(filing_context.lease, receipt_key="filing:" + accession,
            request_identity_sha256="f" * 64, item=SourceItemIdentity("SEC", "filing", accession, cik),
            item_outcome_key="filing:" + accession, state="present",
            metadata=dict(accession=accession, cik=cik, form="10-K", archive_filename=filename,
                          filing_date=NOW.date().isoformat()),
            source_url="https://www.sec.gov/Archives/edgar/daily-index/2026/QTR3/form.20260929.idx",
            artifact_sha256=filing_artifact.content_sha256, detected_at=NOW)
        sources.parent = parent
        resource_context = add_job(4, "sec-resource-fetch-v3", dict(accession=accession, cik=cik,
            form="10-K", filename=filename, observation_id=parent.observation.source_observation_id), "synthetic-resource")
        company_facts = json.loads(_root_payloads()["company_facts"])
        company_facts["cik"] = int(cik)
        company_facts["facts"]["us-gaap"]["Assets"]["units"]["USD"][0].update(accn=accession, form="10-K")
        payloads = dict(complete_submission=container, accession_inventory=json.dumps(inventory).encode(),
            submissions=json.dumps(submissions).encode(), company_facts=json.dumps(company_facts).encode())
        urls = {resource.url: resource.role for resource in sec_ingestion._root_resources(accession=accession, cik=cik, filename=filename)}
        class Transport:
            def fetch(self, url, *, maximum_bytes):
                role = urls[url]
                response = url + ("?padding=" + "x" * response_padding if role == "complete_submission" and response_padding else "")
                return EdgarTransportResult(payloads[role], response, "text/plain", None, None)
        owner = sec_ingestion.SecResourceFetchHandler(edgar=Transport(), object_store=objects,
            artifacts=artifacts, observations=sources, jobs=jobs, clock=lambda: NOW,
            annotate_annual=True, retain_partial_acquisitions=True)
        checkpoint = owner(resource_context)
        jobs.rows[4] = replace(jobs.rows[4], status="succeeded", owner=None, lease_expires_at=None,
                               checkpoint=checkpoint, finished_at=NOW)
        selected = sec_ingestion._required_retained_documents(inventory, submissions,
            parent=parent, accession=accession, cik=cik, form="10-K").auxiliaries
        raw_receipts = [row["outcome"] for (job_id, _), row in jobs.receipts.items() if job_id in {2, 4}
            and row["outcome"]["source_item"]["source_key"] != "companyfacts/" + cik]
        raw_receipts.sort(key=lambda value: (value["delivery"]["source_item_id"], value["delivery"]["source_observation_id"], value["delivery"]["occurrence_id"]))
        budget = sec_ingestion._AdmissionBudget()
        _, resource_stamp = sec_ingestion._submission_part_codec(sec_ingestion._capture_job_stamp(jobs.rows[4]), budget)
        execution = json.loads(sec_ingestion._submission_part_execution(budget))
        parameters = dict(request_revision=1, operation_key="synthetic-parts", operation_at=NOW.isoformat(),
            execution=execution, resource=dict(job_id=4, occurrence_id=4, schedule_key="synthetic-resource",
                scheduled_for=NOW.isoformat(), job_stamp_sha256=hashlib.sha256(resource_stamp).hexdigest()),
            acquisitions=raw_receipts, documents=[dict(document=name, source_role=role) for name, role in selected],
            extraction_revision="SEC_NAMED_DOCUMENTS_1")
        context = add_job(9, "sec-submission-parts-v1", parameters, "synthetic-parts")
        native = store_evidence(b"separately retained named primary\n", "text/html", objects, artifacts)
        association = dict(job_id=9, occurrence_id=9, schedule_key="synthetic-parts", scheduled_for=NOW.isoformat())
        events.clear()
        objects.puts.clear()
        return SimpleNamespace(owner=owner, context=context, jobs=jobs, observations=sources,
            artifacts=artifacts, objects=objects, events=events, association=association,
            selected=selected, native=native, container=container, parent=parent)
    return build



from dataclasses import replace
import json

import pytest

from screener import sec_ingestion
from screener.artifacts import store_evidence
from screener.source_observations import SourceItemIdentity
from screener.sources import sec_submission


def test_public_parts_register_seven_and_replay_without_put(submission_parts_flow):
    flow = submission_parts_flow()
    native_before = flow.artifacts.get(flow.native.content_sha256)
    children_before = list(flow.jobs.children)
    result = flow.owner.derive_registered_auxiliaries(flow.context)
    assert len(result["acquisitions"]) == len(flow.selected) == 7
    assert len(flow.objects.puts) == 7
    assert flow.artifacts.get(flow.native.content_sha256) == native_before
    assert flow.jobs.children == children_before
    for value, (document, role) in zip(result["acquisitions"], flow.selected, strict=True):
        stored = flow.observations.acquisition_receipt(producer_job_id=9,
            receipt_key="sec-submission-part:1:" + document,
            request_identity_sha256=result["request_identity_sha256"])
        assert stored.observation.canonical_metadata["role"] == role
        assert stored.observation.canonical_metadata["derived_document"]["fidelity"] == "EXACT_CONTAINER_BYTES"
        assert stored.item.parent_source_item_id == flow.parent.item.source_item_id
        assert stored.observation.source_url.endswith("0000000001-26-000001.txt")
        assert stored.observation.etag is stored.observation.last_modified is stored.observation.source_published_at is None
    flow.objects.puts.clear()
    replay = flow.owner.derive_registered_auxiliaries(flow.context)
    assert replay == result and flow.objects.puts == []


def test_source_metadata_failed_producer_needs_no_live_current_or_body(submission_parts_flow, monkeypatch):
    flow = submission_parts_flow()
    flow.owner.derive_registered_auxiliaries(flow.context)
    flow.jobs.rows[9] = replace(flow.jobs.rows[9], status="failed", owner=None, lease_expires_at=None)
    def denied(*args, **kwargs):
        raise AssertionError("source-only metadata must not ask for live/current/body work")
    monkeypatch.setattr(sec_ingestion, "_submission_part_execution", denied)
    monkeypatch.setattr(flow.jobs, "_lock_live_lease", denied)
    monkeypatch.setattr(flow.observations, "latest_sec_filing", denied)
    monkeypatch.setattr(flow.objects, "read_verified", denied)
    metadata = sec_ingestion._admit_submission_part_source_metadata(flow.owner, flow.association,
        sec_ingestion._AdmissionBudget())
    assert len(metadata.receipts) == 7 and not hasattr(metadata, "bodies")
    assert json.loads(metadata.execution) == flow.context.job.parameters["execution"]
    sec_ingestion._recheck_submission_part_source_metadata(flow.owner, metadata)


def test_public_parts_envelope_over_refuses_before_any_put(submission_parts_flow):
    flow = submission_parts_flow(response_padding=30000,
        headers={"issuer_pre.xml": {"type": "T" * 4096, "description": "D" * 4096}})
    with pytest.raises(sec_ingestion._AdmissionCapacity, match="encoded-byte ceiling"):
        flow.owner.derive_registered_auxiliaries(flow.context)
    assert flow.objects.puts == []
    assert not any(event[0] in {"source", "artifact", "checkpoint"} for event in flow.events)


@pytest.mark.parametrize("field", ["type", "description"])
def test_public_parts_nul_header_writer_and_replay_agree(submission_parts_flow, field):
    document = "issuer_pre.xml"
    flow = submission_parts_flow(headers={document: {field: "bad\0header"}})
    with pytest.raises(ValueError, match="header text"):
        flow.owner.derive_registered_auxiliaries(flow.context)
    assert flow.objects.puts == []
    metadata = sec_ingestion._admit_submission_part_source_metadata(flow.owner, flow.association,
        sec_ingestion._AdmissionBudget())
    part = sec_submission.decode_named_documents(flow.container, (document,)).documents[0]
    role = dict(flow.selected)[document]
    resource = sec_ingestion._SecResource(role, "resource", metadata.accession + "/" + document,
        metadata.container.observation.source_url, True)
    child_metadata = sec_ingestion._resource_metadata(resource=resource, role=role, accession=metadata.accession,
        cik=metadata.cik, form=metadata.form, filename=metadata.filename, parent=metadata.parent)
    child_metadata.update(document=document, derived_document=sec_ingestion._submission_part_origin(metadata, part))
    artifact = store_evidence(part.payload, "application/octet-stream", flow.objects, flow.artifacts)
    flow.observations.record_with_acquisition_receipt(flow.context.lease,
        receipt_key="sec-submission-part:1:" + document, request_identity_sha256=metadata.request_identity_sha256,
        item=SourceItemIdentity("SEC", "resource", metadata.accession + "/" + document,
            metadata.cik, metadata.parent.item.source_item_id), item_outcome_key="derived:" + metadata.accession + ":" + document,
        state="present", metadata=child_metadata, source_url=metadata.container.observation.source_url,
        artifact_sha256=artifact.content_sha256, detected_at=flow.context._clock())
    flow.objects.puts.clear()
    with pytest.raises(ValueError, match="header text"):
        flow.owner.derive_registered_auxiliaries(flow.context)
    assert flow.objects.puts == []

from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event

import pytest

from screener import sec_ingestion
from screener.durable_jobs import LeaseToken
from screener.job_runtime import JobContext
from screener.source_observations import SourceAcquisitionReceipt, SourceItemIdentity, observation_sha256
from screener.source_validity import SourceDeliveryRecord


def test_actual_hot_fingerprints_bind_decoder_and_reset_each_pair(monkeypatch):
    opened = []
    original_open = Path.open
    def tracked_open(path, *args, **kwargs):
        opened.append(path)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", tracked_open)
    budget = sec_ingestion._AdmissionBudget()
    first = json.loads(sec_ingestion._submission_part_execution(budget))
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get() is None
    initial_count = len(opened)
    initial_reads = budget.read_bytes
    second = json.loads(sec_ingestion._submission_part_execution(budget))
    assert first == second
    assert budget.read_bytes > initial_reads
    assert sum(path.name == "sec_submission.py" for path in opened[initial_count:]) == 1
    assert len(set(opened[initial_count:])) == len(opened[initial_count:])
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get() is None
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get() is None
    from screener.captured_assembly import engine_identity
    from screener.sec_inventory_plan import producer_build_sha256
    assert second["engine_identity_sha256"] == engine_identity()
    assert second["producer_build_sha256"] == producer_build_sha256()


@pytest.mark.parametrize("exhausted_field", ["read_bytes", "work_bytes"])
def test_fingerprint_capacity_failure_restores_outer_context_before_file_read(monkeypatch, exhausted_field):
    def forbidden_open(*args, **kwargs):
        raise AssertionError("capacity refusal must precede source-file read")
    monkeypatch.setattr(Path, "open", forbidden_open)
    outer = sec_ingestion._AdmissionBudget()
    token = sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.set(outer)
    outer_pair = (outer, {})
    pair_token = sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.set(outer_pair)
    try:
        exhausted = sec_ingestion._AdmissionBudget(**{exhausted_field: (64 if exhausted_field == "read_bytes" else 128) * 1024 * 1024})
        with pytest.raises(sec_ingestion._AdmissionCapacity):
            sec_ingestion._submission_part_execution(exhausted)
        assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get() is outer
        assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get() is outer_pair
    finally:
        sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.reset(pair_token)
        sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.reset(token)


class _PinPairFile:
    def __init__(self):
        self.raw, self.opens = b"pair-source-one", 0
    def stat(self):
        return SimpleNamespace(st_size=len(self.raw))
    def open(self, mode):
        assert mode == "rb"
        self.opens += 1
        return io.BytesIO(self.raw)


def test_pin_pair_overlap_sha_once_import_snapshot_and_next_pair_change(monkeypatch):
    from screener import captured_assembly, sec_inventory_plan
    path, hashed = _PinPairFile(), []
    actual_sha = hashlib.sha256
    def tracked_sha(raw=b""):
        if raw.startswith(b"pair-source-"):
            hashed.append(raw)
        return actual_sha(raw)
    monkeypatch.setattr(hashlib, "sha256", tracked_sha)
    def owner_hash(owner, encode=False):
        budget = sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get()
        return sec_ingestion._submission_part_source_hashes((("pair.py", path),), budget,
            owner_kind=owner, encode_result=encode)["pair.py"]
    monkeypatch.setattr(captured_assembly, "engine_identity", lambda: owner_hash("engine", True))
    def producer():
        # Both initial imported-source capture and the current-source check use this owner.
        imported = owner_hash("producer")
        current = owner_hash("producer")
        assert imported == current
        return current
    monkeypatch.setattr(sec_inventory_plan, "producer_build_sha256", producer)
    first_budget = sec_ingestion._AdmissionBudget()
    first = json.loads(sec_ingestion._submission_part_execution(first_budget))
    assert path.opens == 1 and hashed == [path.raw]
    assert first_budget.read_bytes == len(path.raw) + 1
    assert first["engine_identity_sha256"] == first["producer_build_sha256"] == actual_sha(path.raw).hexdigest()
    path.raw = b"pair-source-two"
    second_budget = sec_ingestion._AdmissionBudget()
    second = json.loads(sec_ingestion._submission_part_execution(second_budget))
    assert path.opens == 2 and hashed == [b"pair-source-one", b"pair-source-two"]
    assert second_budget.read_bytes == len(path.raw) + 1
    assert first != second
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get() is None


@pytest.mark.parametrize("inner_error", [False, True])
def test_nested_pin_pairs_restore_outer_verified_hashes_on_success_or_error(monkeypatch, inner_error):
    from screener import captured_assembly, sec_inventory_plan
    path, depth = _PinPairFile(), []
    def owner_hash(owner):
        return sec_ingestion._submission_part_source_hashes((("pair.py", path),),
            sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get(), owner_kind=owner,
            encode_result=False)["pair.py"]
    def engine():
        digest = owner_hash("engine")
        if depth:
            if inner_error:
                raise RuntimeError("inner pin failure")
            return digest
        outer = sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get()
        depth.append(True)
        try:
            if inner_error:
                with pytest.raises(RuntimeError, match="inner pin failure"):
                    sec_ingestion._submission_part_execution(sec_ingestion._AdmissionBudget())
            else:
                sec_ingestion._submission_part_execution(sec_ingestion._AdmissionBudget())
        finally:
            depth.pop()
        assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get() is outer
        assert owner_hash("engine") == digest
        return digest
    monkeypatch.setattr(captured_assembly, "engine_identity", engine)
    monkeypatch.setattr(sec_inventory_plan, "producer_build_sha256", lambda: owner_hash("producer"))
    sec_ingestion._submission_part_execution(sec_ingestion._AdmissionBudget())
    assert path.opens == 2
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_BUDGET.get() is None
    assert sec_ingestion._SUBMISSION_PART_FINGERPRINT_PAIR.get() is None


def test_reserved_file_growth_refuses_before_sha(monkeypatch):
    class GrowingPath:
        def stat(self):
            return SimpleNamespace(st_size=3)
        def open(self, mode):
            assert mode == "rb"
            return io.BytesIO(b"four")
    def forbidden_hash(*args, **kwargs):
        raise AssertionError("size drift must refuse before source SHA")
    monkeypatch.setattr(hashlib, "sha256", forbidden_hash)
    with pytest.raises(ValueError, match="changed size"):
        sec_ingestion._submission_part_source_hashes((("synthetic.py", GrowingPath()),),
            sec_ingestion._AdmissionBudget(), owner_kind="producer", encode_result=False)


@pytest.mark.parametrize("mismatch", ["observations", "artifacts", "context"])
def test_public_parts_reject_cross_engine_before_parameters_or_lookup(mismatch):
    engine, other = object(), object()
    jobs = SimpleNamespace(engine=engine)
    observations = SimpleNamespace(engine=other if mismatch == "observations" else engine)
    artifacts = SimpleNamespace(engine=other if mismatch == "artifacts" else engine)
    context_jobs = SimpleNamespace(engine=other if mismatch == "context" else engine)
    owner = sec_ingestion.SecResourceFetchHandler(edgar=object(), object_store=object(),
        artifacts=artifacts, observations=observations, jobs=jobs)
    context = JobContext(job=SimpleNamespace(kind="sec-submission-parts-v1"), stopping=Event(),
        _repository=context_jobs, _lease=LeaseToken(9, "synthetic-owner", 1),
        _clock=lambda: datetime.now(timezone.utc))
    with pytest.raises(ValueError, match="same-engine"):
        owner.derive_registered_auxiliaries(context)


@pytest.mark.parametrize("flipped", [True, 1.0])
def test_receipt_preflight_type_flip_refuses_before_authoritative_getter(flipped):
    now = datetime(2026, 10, 10, tzinfo=timezone.utc)
    values = dict(state="present", artifact_sha256="a" * 64,
        source_url="https://www.sec.gov/Archives/edgar/data/1/0000000001-26-000001.txt",
        etag=None, last_modified=None, source_published_at=None)
    digest = observation_sha256(metadata={"value": 1}, **values)
    identity = SourceItemIdentity("SEC", "resource", "synthetic/part", "0000000001", 3)
    delivery = SourceDeliveryRecord(7, 4, 14, 9, 1, "derived:synthetic:part", now)
    receipt = SourceAcquisitionReceipt(9, "b" * 64, "sec-submission-part:1:part", identity, digest, delivery)
    raw = receipt.to_outcome()
    budget = sec_ingestion._AdmissionBudget()
    def receipt_hint(connection, job_id, item_key, *, _raw_work_budget=None):
        assert _raw_work_budget is budget
        return {"outcome": raw}
    jobs = SimpleNamespace(engine=SimpleNamespace(connect=lambda: nullcontext(object())),
        _item_outcome_on_connection=receipt_hint)
    observation = SimpleNamespace(canonical_metadata={"value": flipped}, **values)
    def forbidden_getter(**kwargs):
        raise AssertionError("type-flipped source must refuse before authoritative lookup")
    def by_sha256(sha, *, _raw_work_budget=None):
        assert _raw_work_budget is budget
        return SimpleNamespace(observation=observation)
    observations = SimpleNamespace(by_sha256=by_sha256, acquisition_receipt=forbidden_getter)
    owner = SimpleNamespace(_jobs=jobs, _observations=observations)
    with pytest.raises(ValueError, match="preflight source hash"):
        sec_ingestion._submission_part_receipt_lookup(owner, budget,
            producer_job_id=9, receipt_key=receipt.receipt_key, request_identity_sha256=receipt.request_identity_sha256)


def _receipt_getter_fixture(flow, monkeypatch):
    from screener import source_observations as owner_module
    raw = json.loads(json.dumps(flow.context.job.parameters["acquisitions"][0]))
    receipt = SourceAcquisitionReceipt.from_outcome(raw)
    stored = flow.observations.records[receipt.observation_sha256]
    row = {**vars(stored.item), **vars(stored.observation)}
    ledger = dict(job_id=receipt.producer_job_id, item_key=sec_ingestion._SOURCE_RECEIPT_PREFIX + receipt.receipt_key,
                  status="succeeded", error_summary=None, attempts=1, outcome=raw, updated_at=NOW, finished_at=NOW)
    database_dump = json.dumps
    class Result:
        def __init__(self, value):
            self.value = value
        def mappings(self):
            return self
        def one_or_none(self):
            return self.value
        def all(self):
            return [] if self.value is None else [self.value]
    class Connection:
        def __init__(self):
            self.engine = engine
            self.connection = SimpleNamespace(dbapi_connection=SimpleNamespace(autocommit=False))
        def execute(self, statement):
            from sqlalchemy.sql import operators, visitors
            from sqlalchemy.sql.elements import BinaryExpression, BindParameter
            bound = {}
            for criterion in statement._where_criteria:
                for expression in visitors.iterate(criterion):
                    if (isinstance(expression, BinaryExpression) and expression.operator is operators.eq
                            and isinstance(expression.right, BindParameter)):
                        key = (expression.left.table.name, expression.left.name)
                        assert key not in bound or bound[key] == expression.right.value
                        bound[key] = expression.right.value
            actual = ledger if ("durable_job_item", "job_id") in bound else row
            assert all(actual[name] == value for (_, name), value in bound.items())
            projected = {}
            for column in statement.selected_columns:
                if column.key.startswith("_raw_bytes_"):
                    original = tuple(column.element.clauses)[0].clause
                    value = actual[original.key]
                    if value is None:
                        size = None
                    elif original.key in {"outcome", "canonical_metadata"}:
                        size = len(database_dump(value, ensure_ascii=False, separators=(", ", ": ")).encode())
                    else:
                        size = len(str(value).encode())
                    projected[column.key] = size
                else:
                    projected[column.key] = actual[column.key]
            return Result(projected)
    engine = SimpleNamespace(connect=lambda: nullcontext(Connection()), dialect=SimpleNamespace(name="postgresql"))
    repository = SourceObservationRepository(engine)
    monkeypatch.setattr(owner_module, "load_delivery", lambda *args, **kwargs: stored.delivery)
    return SimpleNamespace(repository=repository, raw=raw, receipt=receipt, row=row, stored=stored,
        get=lambda budget: repository.acquisition_receipt(producer_job_id=receipt.producer_job_id,
            receipt_key=receipt.receipt_key, request_identity_sha256=receipt.request_identity_sha256,
            _raw_work_budget=budget))


@pytest.mark.parametrize("source_key", ["plain-resource", "資料"])
def test_receipt_codec_uses_actual_size_and_preserves_default_bytes(source_key, submission_parts_flow):
    flow = submission_parts_flow()
    receipt = SourceAcquisitionReceipt.from_outcome(flow.context.job.parameters["acquisitions"][0])
    receipt = replace(receipt, source_item=replace(receipt.source_item, source_key=source_key))
    default = receipt.to_outcome()
    budget = sec_ingestion._AdmissionBudget()
    actual = receipt.to_outcome(_raw_work_budget=budget)
    assert actual == default
    assert SourceAcquisitionReceipt.from_outcome(actual, _raw_work_budget=sec_ingestion._AdmissionBudget()) == receipt
    size = len(json.dumps(default, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    assert 2 * size <= budget.work_bytes < 3 * 8192


def test_actual_getter_sizes_current_ledger_row_not_prior_hint(submission_parts_flow, monkeypatch):
    fixture = _receipt_getter_fixture(submission_parts_flow(), monkeypatch)
    original_size = len(json.dumps(fixture.raw, sort_keys=True, separators=(",", ":")).encode())
    fixture.raw["source_item"]["source_key"] += "x" * 500
    class Budget(sec_ingestion._AdmissionBudget):
        def receipt_codec(self, raw):
            self.actual_size = super().receipt_codec(raw)
            return self.actual_size
    budget = Budget()
    with pytest.raises(SourceObservationIdentityConflict, match="source identity differs"):
        fixture.get(budget)
    assert budget.actual_size > original_size


def test_actual_getter_default_and_budgeted_receipt_are_identical(submission_parts_flow, monkeypatch):
    fixture = _receipt_getter_fixture(submission_parts_flow(), monkeypatch)
    default = fixture.get(None)
    budget = sec_ingestion._AdmissionBudget()
    actual = fixture.get(budget)
    assert actual == default and actual.delivery == fixture.receipt.delivery
    assert budget.work_bytes > 0 and budget.read_bytes > 0


def test_actual_getter_oversized_receipt_refuses_before_json_emission(submission_parts_flow, monkeypatch):
    from screener import source_observations as owner_module
    fixture = _receipt_getter_fixture(submission_parts_flow(), monkeypatch)
    fixture.raw["source_item"]["source_key"] = "x" * 9000
    def forbidden_dump(*args, **kwargs):
        raise AssertionError("oversized actual receipt reached JSON emission")
    monkeypatch.setattr(owner_module.json, "dumps", forbidden_dump)
    with pytest.raises(sec_ingestion._AdmissionCapacity, match="encoded-byte"):
        fixture.get(sec_ingestion._AdmissionBudget())


def test_actual_getter_observation_frontier_refuses_before_hash(submission_parts_flow, monkeypatch):
    from screener import source_observations as owner_module
    fixture = _receipt_getter_fixture(submission_parts_flow(), monkeypatch)
    fixture.row["canonical_metadata"] = dict(padding="x" * 65500)
    def forbidden_hash(**kwargs):
        raise AssertionError("actual observation reached hash before envelope admission")
    monkeypatch.setattr(owner_module, "observation_sha256", forbidden_hash)
    with pytest.raises(sec_ingestion._AdmissionCapacity, match="observation encoded-byte"):
        fixture.get(sec_ingestion._AdmissionBudget())


def test_actual_getter_work_capacity_refuses_before_receipt_emission(submission_parts_flow, monkeypatch):
    from screener import source_observations as owner_module
    fixture = _receipt_getter_fixture(submission_parts_flow(), monkeypatch)
    def forbidden_dump(*args, **kwargs):
        raise AssertionError("work refusal reached JSON emission")
    monkeypatch.setattr(owner_module.json, "dumps", forbidden_dump)
    budget = sec_ingestion._AdmissionBudget(work_bytes=128 * 1024 * 1024)
    with pytest.raises(sec_ingestion._AdmissionCapacity, match="raw-work"):
        fixture.get(budget)


def test_receipt_serialization_size_drift_is_refused(submission_parts_flow, monkeypatch):
    from screener import source_observations as owner_module
    raw = submission_parts_flow().context.job.parameters["acquisitions"][0]
    original_dump = owner_module.json.dumps
    monkeypatch.setattr(owner_module.json, "dumps", lambda value, **kwargs: original_dump(value, **kwargs) + " ")
    with pytest.raises(ValueError, match="encoded size changed"):
        SourceAcquisitionReceipt.from_outcome(raw, _raw_work_budget=sec_ingestion._AdmissionBudget())


def test_receipt_noninteger_reservation_refuses_before_emission(submission_parts_flow, monkeypatch):
    from screener import source_observations as owner_module
    raw = submission_parts_flow().context.job.parameters["acquisitions"][0]
    def forbidden_dump(*args, **kwargs):
        raise AssertionError("invalid reservation reached JSON emission")
    monkeypatch.setattr(owner_module.json, "dumps", forbidden_dump)
    with pytest.raises(ValueError, match="exact byte size"):
        SourceAcquisitionReceipt.from_outcome(raw, _raw_work_budget=SimpleNamespace(receipt_codec=lambda value: True))


def test_store_reservation_matches_one_bounded_verified_read():
    budget = sec_ingestion._AdmissionBudget()
    sec_ingestion._reserve_submission_part_store(budget, 10)
    assert (budget.read_bytes, budget.work_bytes) == (12, 20)


@pytest.mark.parametrize("role", ["complete_submission", "accession_inventory", "submissions"])
def test_source_metadata_root_transfer_quota_refuses_before_root_return(role, submission_parts_flow, monkeypatch):
    flow = submission_parts_flow()
    root = flow.jobs.rows[4].checkpoint["roots"][role]
    flow_budget = sec_ingestion._AdmissionBudget()
    retained_owner = sec_ingestion._retained_root_source
    actual_read = flow.observations.by_sha256
    def retained_root(observations, digest, *, budget=None, **values):
        if digest != root:
            return retained_owner(observations, digest, budget=budget, **values)
        assert budget is flow_budget
        def oversized_root(actual_digest, *, _raw_work_budget=None):
            assert actual_digest == root and _raw_work_budget is flow_budget
            _raw_work_budget.sql_row_read((64 * 1024 * 1024,), ())
            return actual_read(actual_digest, _raw_work_budget=_raw_work_budget)
        with monkeypatch.context() as root_patch:
            root_patch.setattr(observations, "by_sha256", oversized_root)
            return retained_owner(observations, digest, budget=budget, **values)
    monkeypatch.setattr(sec_ingestion, "_retained_root_source", retained_root)
    with pytest.raises(sec_ingestion._AdmissionCapacity):
        sec_ingestion._admit_submission_part_source_metadata(flow.owner, flow.association, flow_budget)
    assert not any(event[0] in {"body", "put", "source", "artifact", "checkpoint"} for event in flow.events)