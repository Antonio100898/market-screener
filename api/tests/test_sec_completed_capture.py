"""Scalar completed-capture recovery from known retained rows and bytes; no I/O."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

from screener.artifacts import EvidenceArtifact
from screener.object_store import ObjectVerificationError
from screener.sec_ingestion import (SecResourceFetchHandler, SecCandidateStageHandler,
    SecResourcePending, _root_resources, _resource_metadata, _resource_identity)
from screener.source_validity import SourceDeliveryRecord
from test_sec_ingestion import _job, NOW
from test_sec_stage_association import inputs, ROLES


class Forbidden:
    def __getattr__(self, name):
        raise AssertionError("readonly recovery crossed into " + name)


class Fixture:
    def __init__(self, kind="sec-resource-fetch-v2"):
        job, checkpoint = inputs()
        self.job = replace(job, kind=kind, checkpoint=checkpoint, status="succeeded", ownership_generation=2)
        self.identity = dict(job_id=7, occurrence_id=8, schedule_key="sec-resource:original", scheduled_for=NOW)
        self.events, self.job_reads, self.drift = [], 0, None
        p = job.parameters
        self.parent = NS(item=NS(source_item_id=10, source_system="SEC", item_kind="filing",
            source_key=p["accession"], issuer_source_identifier=p["cik"]),
            observation=NS(source_observation_id=9, observation_sha256="9"*64, state="present",
                canonical_metadata=dict(accession=p["accession"], cik=p["cik"], form=p["form"],
                    archive_filename=p["filename"], filing_date="2026-09-28")))
        self.raw = dict(complete_submission=b"retained raw submission",
            accession_inventory=json.dumps({"directory":{"name":"/Archives/edgar/data/1234/000000123426000001",
                                                         "item":[{"name":"annual.htm"}]}}).encode(),
            submissions=json.dumps({"cik":1234,"filings":{"recent":{"accessionNumber":[p["accession"]],
                                                                        "form":["10-K"]}}}).encode(),
            company_facts=json.dumps({"cik":1234,"facts":{"us-gaap":{"NetIncomeLoss":{"units":{"USD":[
                {"accn":p["accession"],"form":"10-K","val":5}]}}}}}).encode())
        self.rows, self.deliveries, self.artifact_rows, self.body_rows = {}, {}, {}, {}
        for resource in _root_resources(accession=p["accession"], cik=p["cik"], filename=p["filename"]):
            role = resource.role
            supplied = checkpoint["root_deliveries"][role]
            item = NS(**vars(_resource_identity(resource, cik=p["cik"], parent=self.parent)),
                      source_item_id=supplied["source_item_id"])
            metadata = _resource_metadata(resource=resource, role=role, accession=p["accession"],
                cik=p["cik"], form=p["form"], filename=p["filename"], parent=self.parent)
            metadata.update(capture_revision=2, capture_fidelity="RAW_HTTP_BYTES")
            self.rows[checkpoint["roots"][role]] = NS(item=item, observation=NS(
                source_observation_id=supplied["source_observation_id"], source_item_id=item.source_item_id,
                observation_sha256=checkpoint["roots"][role], state="present", canonical_metadata=metadata,
                artifact_sha256=None, source_url="https://retained.example/"+role,
                etag=None, last_modified=None, detecting_job_id=99))
            values = dict(supplied)
            values["checked_at"] = datetime.fromisoformat(values["checked_at"]).astimezone(timezone.utc)
            self.deliveries[values["occurrence_id"]] = SourceDeliveryRecord(**values)
            self.set_body(role, self.raw[role])
        self.handler = SecResourceFetchHandler(edgar=Forbidden(), object_store=self,
            artifacts=self, observations=self, jobs=self)

    def set_body(self, role, raw):
        digest = hashlib.sha256(raw).hexdigest()
        self.row(role).observation.artifact_sha256 = digest
        self.artifact_rows[digest] = EvidenceArtifact(digest, "raw/sha256/"+digest, len(raw),
                                                    "application/first-registration", NOW, NOW)
        self.body_rows[digest] = raw

    def row(self, role):
        return self.rows[self.job.checkpoint["roots"][role]]

    def scheduled_job(self, **identity):
        assert identity == self.identity
        self.job_reads += 1
        return deepcopy(self.drift if self.job_reads > 1 and self.drift is not None else self.job)

    def sec_filing(self, observation_id):
        assert observation_id == 9
        self.events.append("parent")
        return self.parent

    def by_sha256(self, digest):
        self.events.append("source")
        if digest not in self.rows:
            raise ValueError("source observation SHA-256 is missing or ambiguous")
        return self.rows[digest]

    def delivery(self, occurrence_id, **identity):
        self.events.append("delivery")
        value = self.deliveries[occurrence_id]
        assert value.source_item_id == identity["source_item_id"]
        assert value.source_observation_id == identity["source_observation_id"]
        return value

    def get(self, digest):
        self.events.append("artifact")
        if digest not in self.artifact_rows:
            raise ValueError("verified evidence artifact does not exist")
        return self.artifact_rows[digest]

    def read_verified(self, key, digest, size):
        self.events.append("body")
        raw = self.body_rows[digest]
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
            raise ObjectVerificationError("known retained body mismatch")
        return raw

    def recover(self, **changes):
        return self.handler.recover_completed_capture(**{**self.identity, **changes})


@pytest.mark.parametrize("kind", ["sec-resource-fetch", "sec-resource-fetch-v2"])
def test_actual_owner_rows_verify_both_kinds_and_preserve_original_strings(kind):
    fixture = Fixture(kind)
    before = fixture.handler.candidate_stage_request(fixture.job, fixture.job.checkpoint)
    result = fixture.recover()
    assert result["status"] == "VERIFIED" and result["bytes_verified"]
    assert fixture.events.count("body") == 4 and fixture.job_reads == 2
    assert result["checkpoint"] == fixture.job.checkpoint
    assert result["filing"]["index_artifact_verified"] is False
    for ref in result["roots"]:
        assert ref["delivery"]["ownership_generation"] == 1
        assert ref["delivery"]["job_id"] == 7
        assert ref["delivery"]["checked_at"].endswith("+03:00")
        assert ref["bytes_verified"] and ref["root_structure_validated"] == (ref["role"] != "complete_submission")
        assert ref["artifact"]["registration_media_type"] == "application/first-registration"
        assert ref["original_http_content_type_captured"] is False
    assert before == fixture.handler.candidate_stage_request(fixture.job, result["checkpoint"])
    result["checkpoint"]["roots"]["company_facts"] = "f"*64
    result["roots"][0]["metadata"]["role"] = "forged"
    assert fixture.recover()["status"] == "VERIFIED"


class IntSubclass(int):
    pass


@pytest.mark.parametrize("field,value", [(field, value) for field in ("job_id", "occurrence_id")
    for value in (True, 1.0, IntSubclass(1), 0, -1, 2**63)] + [
        ("schedule_key", "\0key"), ("schedule_key", " key"), ("schedule_key", "x"*1025),
        ("scheduled_for", NOW.replace(tzinfo=None))], ids=lambda value: type(value).__name__)
def test_scalar_refusal_before_any_owner(field, value):
    handler = SecResourceFetchHandler(edgar=Forbidden(), object_store=Forbidden(), artifacts=Forbidden(),
                                     observations=Forbidden(), jobs=Forbidden())
    with pytest.raises(ValueError):
        handler.recover_completed_capture(**{**Fixture().identity, field:value})


@pytest.mark.parametrize("status", ["pending", "running", "retry_wait", "failed"])
def test_operational_status_has_no_source_reads_and_generation_zero_is_valid(status):
    fixture = Fixture()
    fixture.job = replace(fixture.job, status=status, ownership_generation=0,
                          checkpoint=None, error_summary="unsupported financial words are diagnostics")
    result = fixture.recover()
    assert result["status"] == ("FAILED" if status == "failed" else "NOT_COMPLETED")
    assert result["job"]["status"] == status and not result["bytes_verified"]
    assert fixture.events == [] and fixture.job_reads == 2


@pytest.mark.parametrize("kind", ["sec-resource-fetch", "sec-resource-fetch-v2"])
def test_legacy_capture1_is_honest_gap_and_v2_contradiction_refuses(kind):
    fixture = Fixture(kind)
    checkpoint = dict(fixture.job.checkpoint)
    del checkpoint["capture_revision"], checkpoint["root_deliveries"]
    fixture.job = replace(fixture.job, checkpoint=checkpoint)
    if kind.endswith("v2"):
        with pytest.raises(ValueError): fixture.recover()
    else:
        assert fixture.recover()["status"] == "ASSOCIATION_UNAVAILABLE"
    assert fixture.events == []


@pytest.mark.parametrize("change", ["metadata_revision", "metadata_parent_float", "metadata_parent_bool",
    "wrong_parent_sha", "wrong_key", "wrong_kind", "foreign_job", "different_instant", "digest"])
def test_context_contradictions_refuse_before_body(change):
    fixture = Fixture()
    row = fixture.row("accession_inventory")
    if change == "metadata_revision": row.observation.canonical_metadata["capture_revision"] = 2.0
    elif change == "metadata_parent_float": row.observation.canonical_metadata["parent_observation_id"] = 9.0
    elif change == "metadata_parent_bool": row.observation.canonical_metadata["parent_source_item_id"] = True
    elif change == "wrong_parent_sha": row.observation.canonical_metadata["parent_observation_sha256"] = "f"*64
    elif change == "wrong_key": row.item.source_key = "borrowed"
    elif change == "wrong_kind": row.item.item_kind = "aggregate"
    elif change == "digest": row.observation.observation_sha256 = "f"*64
    else:
        occurrence = fixture.job.checkpoint["root_deliveries"]["accession_inventory"]["occurrence_id"]
        delivery = fixture.deliveries[occurrence]
        fixture.deliveries[occurrence] = replace(delivery, **(
            {"job_id":99} if change == "foreign_job" else {"checked_at":delivery.checked_at+timedelta(seconds=1)}))
    with pytest.raises(ValueError): fixture.recover()
    assert "body" not in fixture.events


def test_missing_original_linkage_gap_does_not_mask_contradiction():
    fixture = Fixture()
    del fixture.row("complete_submission").observation.canonical_metadata["parent_observation_id"]
    assert fixture.recover()["status"] == "ASSOCIATION_UNAVAILABLE"
    assert "body" not in fixture.events
    fixture.row("accession_inventory").observation.canonical_metadata["parent_observation_id"] = 99
    with pytest.raises(ValueError): fixture.recover()


@pytest.mark.parametrize("role", ROLES)
def test_each_registered_body_plus_one_returns_compact_gap_before_first_read(role):
    fixture = Fixture()
    digest = fixture.row(role).observation.artifact_sha256
    limit = (32 if role == "company_facts" else 8)*1024*1024
    fixture.artifact_rows[digest] = replace(fixture.artifact_rows[digest], byte_size=limit+1)
    result = fixture.recover()
    assert result["status"] == "CAPACITY_UNAVAILABLE"
    assert result["gap"]["required_bytes"] == limit+1 and result["gap"]["limit_bytes"] == limit
    assert "checkpoint" not in result and len(json.dumps(result)) < 1048576
    assert "body" not in fixture.events and fixture.events.count("delivery") == 4


def test_whole_role_boundaries_maximum56_mib_verify_without_retained_payloads():
    fixture = Fixture()
    for role in ROLES:
        limit = (32 if role == "company_facts" else 8)*1024*1024
        fixture.set_body(role, fixture.raw[role] + b" "*(limit-len(fixture.raw[role])))
    result = fixture.recover()
    assert result["status"] == "VERIFIED"
    assert sum(ref["artifact"]["byte_size"] for ref in result["roots"]) == 56*1024*1024
    assert len(json.dumps(result)) < 1048576


def test_reference_overflow_returns_compact_scalars_before_body():
    fixture = Fixture()
    for role in ROLES:
        fixture.row(role).observation.canonical_metadata.update({"extra"+str(i):"x"*65536 for i in range(8)})
    result = fixture.recover()
    assert result["status"] == "CAPACITY_UNAVAILABLE" and result["gap"]["phase"] == "result_references"
    assert result["gap"]["required_is_lower_bound"]
    assert "checkpoint" not in result and "metadata" not in json.dumps(result)
    assert "body" not in fixture.events


@pytest.mark.parametrize("missing", ["source", "artifact", "body"])
def test_owner_missing_errors_propagate_without_typed_absence(missing):
    fixture = Fixture()
    row = fixture.row("company_facts")
    if missing == "source": del fixture.rows[row.observation.observation_sha256]
    elif missing == "artifact": del fixture.artifact_rows[row.observation.artifact_sha256]
    else: del fixture.body_rows[row.observation.artifact_sha256]
    with pytest.raises(KeyError if missing == "body" else ValueError): fixture.recover()


def test_corrupt_body_propagates_actual_verification_error():
    fixture = Fixture()
    fixture.body_rows[fixture.row("company_facts").observation.artifact_sha256] = b"corrupt"
    with pytest.raises(ObjectVerificationError): fixture.recover()


def test_retained_pending_structure_is_bounded_reference_only_gap():
    fixture = Fixture()
    fixture.set_body("company_facts", b'{"cik":1234,"facts":{}}')
    result = fixture.recover()
    assert result["status"] == "ROOT_STRUCTURE_UNAVAILABLE" and result["gap"]["role"] == "company_facts"
    assert result["gap"]["reason"] == "SEC Company Facts does not yet identify the filing accession"
    assert not result["all_applicable_root_structures_validated"] and not result["bytes_verified"]
    assert result["roots"][-1]["bytes_verified"] and not result["roots"][-1]["root_structure_validated"]


@pytest.mark.parametrize("change", ["parameters", "checkpoint", "status", "priority"])
def test_final_scalar_relation_drift_refuses_all_claims(change):
    fixture = Fixture()
    values = {"parameters":{**fixture.job.parameters,"observation_id":10}} if change == "parameters" else (
        {"checkpoint":{**fixture.job.checkpoint,"filing_observation_id":10}} if change == "checkpoint" else (
        {"status":"running"} if change == "status" else {"priority":0}))
    fixture.drift = replace(fixture.job, **values)
    with pytest.raises(ValueError, match="changed during"): fixture.recover()


def test_old_loader_preserves_default_revision_compare_read_order_and_payload_cache():
    fixture = Fixture()
    for role in ROLES:
        fixture.row(role).observation.canonical_metadata["capture_revision"] = 2.0
        occurrence = fixture.job.checkpoint["root_deliveries"][role]["occurrence_id"]
        delivery = fixture.deliveries[occurrence]
        fixture.deliveries[occurrence] = replace(delivery,
            checked_at=datetime.fromisoformat(fixture.job.checkpoint["root_deliveries"][role]["checked_at"]))
    stage = SecCandidateStageHandler(edgar=Forbidden(), object_store=fixture, artifacts=fixture,
                                     observations=fixture, companies=Forbidden())
    payloads, artifacts, sources = stage._load_roots(fixture.job.checkpoint["roots"], parent=fixture.parent,
        accession=fixture.job.parameters["accession"], cik=fixture.job.parameters["cik"],
        deliveries=fixture.job.checkpoint["root_deliveries"])
    assert fixture.events == [event for _ in ROLES for event in ("source","artifact","body","delivery")]
    assert set(payloads) == set(ROLES)-{"complete_submission"} and len(artifacts) == len(sources) == 4
    cache = dict(fixture.body_rows)
    fixture.events.clear()
    assert stage._load_roots(fixture.job.checkpoint["roots"], parent=fixture.parent,
        accession=fixture.job.parameters["accession"], cik=fixture.job.parameters["cik"],
        deliveries=fixture.job.checkpoint["root_deliveries"], verified_payloads=cache)[0] == payloads
    assert "body" not in fixture.events
    with pytest.raises(ValueError): fixture.recover()


@pytest.mark.parametrize("error", [ValueError("retained wrong structure"), RuntimeError("object SDK error")], ids=["structure","object"])
def test_untyped_errors_propagate_same_object(error, monkeypatch):
    fixture = Fixture()
    if isinstance(error, ValueError):
        fixture.set_body("company_facts", b'{"cik":99,"facts":{}}')
        with pytest.raises(ValueError, match="another issuer"): fixture.recover()
    else:
        def fail(*args): raise error
        monkeypatch.setattr(fixture,"read_verified",fail)
        with pytest.raises(RuntimeError) as caught: fixture.recover()
        assert caught.value is error


def test_pending_subclass_is_not_converted_and_top_structure_truth_is_explicit(monkeypatch):
    import screener.sec_ingestion as owner
    class OtherPending(SecResourcePending): pass
    error = OtherPending("other owner pending")
    fixture = Fixture()
    original = owner._root_resources
    def resources(**kwargs):
        result = list(original(**kwargs))
        def fail(*args): raise error
        result[-1] = replace(result[-1],validator=fail)
        return tuple(result)
    monkeypatch.setattr(owner,"_root_resources",resources)
    with pytest.raises(OtherPending) as caught: fixture.recover()
    assert caught.value is error
    monkeypatch.setattr(owner,"_root_resources",original)
    result = fixture.recover()
    assert result["all_applicable_root_structures_validated"]
    assert not result["roots"][0]["root_structure_validated"]


def test_operational_return_also_rechecks_final_status():
    fixture = Fixture()
    fixture.job = replace(fixture.job,status="pending",ownership_generation=0,checkpoint=None)
    fixture.drift = replace(fixture.job,status="running",ownership_generation=1)
    with pytest.raises(ValueError,match="changed during"):fixture.recover()


def test_unknown_kind_refuses_even_when_failed():
    fixture = Fixture()
    fixture.job = replace(fixture.job,kind="other",status="failed")
    with pytest.raises(ValueError):fixture.recover()
    assert fixture.events == []
