"""Unavailable identities retain real diagnoses, never invented financial data."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from screener import company_availability as availability, sync
from screener.company_import import DerivationError, RetainedInputMissing
from screener.company_projection import project_companies
from screener.display_publication import typed_projection
from screener.source_observations import SourceItemIdentity, _canonical_url, observation_sha256
from test_company_import import retained, _sec, _importer


def assessment(*, code="LOCAL_INPUT_MISSING", remote_ids=()):
    return availability.load_assessment(dict(
        kind=availability.REMOTE_KIND if remote_ids else availability.LOCAL_KIND,
        issuer_source="SEC", issuer_identifier="0000001800", ticker="ABT",
        reason_code=code, reason=("Captured source reported unavailable." if remote_ids else
                                 "Retained input is missing locally; remote availability was not checked."),
        assessed_at="2026-10-13T00:00:00Z", input_artifact_sha256s=[], remote_observation_ids=list(remote_ids)))


def local_row(record=None, digest="a" * 64):
    return availability.unavailable_row(record or assessment(), digest, name="Retained identity", exchange="NYSE")


def observed_local(record, digest="a" * 64):
    item = availability.local_source_item(record)
    return dict(source_observation_id=7, source_item_id=3, source_system=item.source_system,
        issuer_source_identifier=item.issuer_source_identifier, item_kind=item.item_kind,
        source_key=item.source_key, parent_source_item_id=None, state="present",
        artifact_sha256=digest, source_url=availability.local_source_url(record), etag=None,
        last_modified=None, source_published_at=None,
        canonical_metadata=availability.local_observation_metadata(record, digest))


def test_missing_local_file_has_a_real_retained_diagnosis_and_no_derivation(retained):
    _, cache = retained
    _sec(*retained)
    (cache / "companyfacts_0000001800.json").unlink()
    importer, objects, _, companies = _importer(retained,
        lambda bundle: (_ for _ in ()).throw(AssertionError("missing input cannot derive")))
    with pytest.raises(RetainedInputMissing):
        importer.import_ticker("ABT")
    result = importer.import_ticker("ABT", preserve_unavailable=True)
    assert result.data_status == "UNAVAILABLE"
    row = companies.calls[-1]["canonical_payload"]
    record, digest = availability.validate_unavailable_row(row)
    assert record.reason_code == "LOCAL_INPUT_MISSING" and not record.remote_observation_ids
    assert "locally" in record.reason and "not checked" in record.reason
    diagnostic = next(item.artifact for item in result.artifacts if item.role == availability.ARTIFACT_ROLE)
    import json
    assert json.loads(objects.data[diagnostic.object_key]) == record.json_record()
    assert diagnostic.content_sha256 == digest
    assert companies.calls[-1]["security_basis"] == "UNASSESSED"
    assert companies.calls[-1]["receipt_ratio"] is None


@pytest.mark.parametrize("status,code", [("foreign", "NORMALIZATION_UNSUPPORTED"), ("pending_facts", "FILING_FACTS_PENDING")])
def test_controlled_normalizer_refusals_keep_actual_input_artifacts(retained, status, code):
    _sec(*retained)
    importer, _, _, companies = _importer(retained, lambda bundle: (status, None))
    result = importer.import_ticker("ABT", preserve_unavailable=True)
    record, digest = availability.validate_unavailable_row(companies.calls[-1]["canonical_payload"])
    assert result.data_status == "UNAVAILABLE" and record.reason_code == code
    assert len(record.input_artifact_sha256s) >= 2
    assert {item.artifact.content_sha256 for item in result.artifacts} == {digest, *record.input_artifact_sha256s}


def test_unexpected_engine_errors_are_not_green_unavailable_records(retained):
    _sec(*retained)
    importer, _, _, companies = _importer(retained, lambda bundle: ("error", {"error": "internal defect"}))
    with pytest.raises(DerivationError):
        importer.import_ticker("ABT", preserve_unavailable=True)
    assert not companies.calls


def test_local_assessment_ledger_uses_its_real_artifact_and_no_remote_headers(retained):
    _sec(*retained)
    _, cache = retained
    (cache / "companyfacts_0000001800.json").unlink()
    importer, _, _, _ = _importer(retained, lambda bundle: None)
    imported = importer.import_ticker("ABT", preserve_unavailable=True)
    calls = []
    class Observations:
        def record(self, lease, **values):
            calls.append(values)
            return SimpleNamespace(observation=SimpleNamespace(source_observation_id=11))
    assert importer.record_unavailability(imported, object(), Observations(), item_outcome_key="ABT") == (11,)
    value, = calls
    assert value["item"].item_kind == "inventory" and value["state"] == "present"
    assert value["source_url"].startswith("urn:")
    assert value["detected_at"].tzinfo is not None
    assert "source_published_at" not in value and "etag" not in value


@pytest.mark.parametrize("tamper", ["missing_artifact", "filing", "remote_state", "publication_date", "wrong_urn", "wrong_metadata"])
def test_local_urn_cannot_claim_a_primary_or_remote_source(tamper):
    record = assessment()
    args = dict(item=availability.local_source_item(record), state="present",
        metadata=availability.local_observation_metadata(record, "a" * 64), artifact="a" * 64,
        etag=None, last_modified=None, published_at=None)
    value = availability.local_source_url(record)
    if tamper == "missing_artifact": args["artifact"] = None
    elif tamper == "filing": args["item"] = SourceItemIdentity("SEC", "filing", "x", "0000001800")
    elif tamper == "remote_state": args["state"] = "unavailable"
    elif tamper == "publication_date": args["published_at"] = datetime.now(timezone.utc)
    elif tamper == "wrong_urn": value += "/other"
    else: args["metadata"]["pretend_remote"] = True
    with pytest.raises(ValueError): availability.admit_local_source_url(value, **args)
    with pytest.raises(ValueError): _canonical_url(value)


def test_local_assessment_digest_retains_exact_diagnosis_and_url():
    record = assessment()
    values = dict(state="present", metadata=availability.local_observation_metadata(record, "a" * 64),
        artifact_sha256="a" * 64, source_url=availability.local_source_url(record))
    digest = observation_sha256(**values)
    assert len(digest) == 64 and digest == observation_sha256(**deepcopy(values))
    changed = record.model_copy(update={"reason": "A different retained input diagnosis."})
    values["metadata"] = availability.local_observation_metadata(changed, "a" * 64)
    assert digest != observation_sha256(**values)
    with pytest.raises(ValueError):
        _canonical_url(values["source_url"])


@pytest.mark.parametrize("tamper", ["wrong_urn", "missing_artifact", "remote_state", "etag",
    "last_modified", "publication_date", "extra_metadata", "missing_kind"])
def test_local_assessment_digest_cannot_bypass_source_admission(tamper):
    record = assessment()
    values = dict(state="present", metadata=availability.local_observation_metadata(record, "a" * 64),
        artifact_sha256="a" * 64, source_url=availability.local_source_url(record))
    if tamper == "wrong_urn": values["source_url"] += "/other"
    elif tamper == "missing_artifact": values["artifact_sha256"] = None
    elif tamper == "remote_state":
        values.update(state="unavailable", artifact_sha256=None)
    elif tamper == "etag": values["etag"] = "not-an-HTTP-response"
    elif tamper == "last_modified": values["last_modified"] = "not-an-HTTP-response"
    elif tamper == "publication_date": values["source_published_at"] = datetime.now(timezone.utc)
    elif tamper == "extra_metadata": values["metadata"]["pretend_remote"] = True
    else: del values["metadata"]["kind"]
    with pytest.raises(ValueError):
        observation_sha256(**values)


def test_captured_remote_unavailability_requires_actual_issuer_state_and_ids():
    record = assessment(code="REMOTE_SOURCE_UNAVAILABLE", remote_ids=(19,))
    row = dict(source_system="SEC", issuer_source_identifier="0000001800", state="unavailable", source_observation_id=19)
    availability.validate_source_rows(record, "a" * 64, [row])
    for bad in ({**row, "state": "present"}, {**row, "state": "removed"}, {**row, "source_observation_id": 20},
                {**row, "issuer_source_identifier": "0000000001"}):
        with pytest.raises(ValueError): availability.validate_source_rows(record, "a" * 64, [bad])
    with pytest.raises(ValueError): assessment(code="REMOTE_SOURCE_UNAVAILABLE")
    with pytest.raises(ValueError): assessment(remote_ids=(19,))


def test_remote_import_reads_recorded_state_instead_of_trusting_local_missingness(retained, monkeypatch):
    from contextlib import nullcontext
    from screener.publication import PublicationRepository
    _sec(*retained)
    importer, _, _, companies = _importer(retained, lambda bundle: None)
    companies.engine = SimpleNamespace(connect=lambda: nullcontext(object()))
    actual = dict(source_system="SEC", issuer_source_identifier="0000001800",
                  state="unavailable", source_observation_id=19)
    def rows(connection, ids):
        assert ids == (19,)
        return [actual]
    monkeypatch.setattr(PublicationRepository, "_source_rows", staticmethod(rows))
    result = importer.import_source_unavailable("ABT", observation_ids=(19,), reason="Captured provider failure.")
    assert result.data_status == "UNAVAILABLE"
    record, _ = availability.validate_unavailable_row(companies.calls[-1]["canonical_payload"])
    assert record.remote_observation_ids == (19,) and record.kind == availability.REMOTE_KIND
    actual["state"] = "present"
    count = len(companies.calls)
    with pytest.raises(ValueError):
        importer.import_source_unavailable("ABT", observation_ids=(19,), reason="Cannot infer unavailable.")
    assert len(companies.calls) == count


@pytest.mark.parametrize("field,value", [("price", 0), ("shares", 1), ("annual_eps", {"2025": 0}),
    ("pe", 5), ("owner_earnings", {}), ("n_pass", False)])
def test_unavailable_rows_refuse_figures_even_zero_or_hidden_ratios(field, value):
    row = local_row()
    row[field] = value
    with pytest.raises(ValueError): availability.validate_unavailable_row(row)
    with pytest.raises(ValueError): project_companies([row])


def test_actual_pricing_owner_cannot_enrich_an_unavailable_diagnosis():
    row = local_row()
    original = deepcopy(row)
    assert sync.apply_price(row, 100) == original
    assert row == original
    row["shares"] = 0
    with pytest.raises(ValueError): sync.apply_price(row, 100)


def test_shared_projection_keeps_all_identities_and_null_ratios():
    row = local_row()
    projection, = project_companies([row])
    result = typed_projection({**row, **projection}, ordinal=0, security_id=1, input_bundle_sha256="b" * 64)
    assert result["enriched_payload"]["data_availability"] == row["data_availability"]
    assert result["criteria_passed"] == 0 and result["unjudged"]
    assert result["return_quality"] == 0  # existing disclosed missing-data score, not a financial figure
    assert all(result[key] is None for key in ("market_cap", "price", "pe", "pe3", "dividend_yield"))


def test_large_assessment_collection_refuses_before_serialization(monkeypatch):
    value = assessment().json_record()
    value["input_artifact_sha256s"] = ["a" * 64] * 8193
    monkeypatch.setattr(availability.UnavailabilityAssessment, "model_dump_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must refuse before allocation")))
    with pytest.raises(ValueError): availability.load_assessment(value)


def test_unavailable_snapshot_binding_does_not_accept_a_different_evidence_set():
    row = local_row()
    kwargs = dict(issuer_source="SEC", issuer_identifier="0000001800", ticker="ABT")
    artifact = dict(content_sha256="a" * 64, role=availability.ARTIFACT_ROLE)
    availability.validate_snapshot(row, [artifact], **kwargs)
    with pytest.raises(ValueError):
        availability.validate_snapshot(row, [{**artifact, "content_sha256": "b" * 64}], **kwargs)
    with pytest.raises(ValueError):
        availability.validate_snapshot(row, [artifact, dict(content_sha256="b" * 64, role="unbound")], **kwargs)


def test_urn_acquisition_receipt_is_refused_before_any_database_access():
    from screener.source_observations import SourceObservationRepository
    from screener.durable_jobs import LeaseToken
    from test_source_observations import NoDatabaseEngine
    record = assessment()
    repo = SourceObservationRepository(NoDatabaseEngine())
    with pytest.raises(ValueError, match="acquisition"):
        repo.record_with_acquisition_receipt(LeaseToken(1, "owner", 1),
            item=availability.local_source_item(record), item_outcome_key="ABT", state="present",
            metadata=availability.local_observation_metadata(record, "a" * 64),
            source_url=availability.local_source_url(record), artifact_sha256="a" * 64,
            receipt_key="not-a-request", request_identity_sha256="b" * 64)
