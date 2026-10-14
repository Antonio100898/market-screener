"""Exact CF targets. Positive captured IDs are synthetic, not registered sources."""
import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import pickle
from types import SimpleNamespace

import pytest

from screener import evidence
from screener.models import AdmittedPrimaryInput, CapturedSource, UnavailablePrimaryInput
from screener.sources import sec_json
from test_native_primary_inputs import CIK, fixture, mapping


def company_facts(token="2.33", *, cik=1066923, occurrence=3, unit="USD/shares", concept="EarningsPerShareDiluted"):
    entries = [{"val": 1, "start": "2020-01-01", "end": "2020-12-31",
                "accn": "0001213900-21-021762", "form": "10-K", "filed": "2021-04-15"}
               for _ in range(100)]
    entries[97]["val"] = 2.34
    entries[98] = {**entries[98], "val": 2.33, "accn": "0001213900-22-020106", "filed": "2022-04-15"}
    entries[99] = {**entries[99], "val": "EXACT_TOKEN", "accn": "0001213900-23-022083",
                   "form": "10-K/A", "filed": "2023-03-22", "frame": "CY2020"}
    raw = json.dumps({"cik": cik, "facts": {"us-gaap": {concept: {"units": {unit: entries}}}}}).replace('"EXACT_TOKEN"', token).encode()
    source = CapturedSource("company_facts", hashlib.sha256(raw).hexdigest(),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0001066923.json", 1, 2, occurrence, "EXACT_LEXICAL")
    tree = sec_json.decode_company_facts(raw, source=source)
    rows = tree["facts"]["us-gaap"][concept]["units"][unit]
    return raw, source, tree, rows[99][sec_json.ORIGIN_KEY]


def plan(*, token="2.33", **cf_kwargs):
    raw, source, tree, target = company_facts(token, **cf_kwargs)
    body, request = fixture()
    declaration = replace(mapping(body, request), target_origin=target)
    payloads = {request.source.artifact_sha256: body, source.artifact_sha256: raw}
    return declaration, payloads, tree


def admit(declaration, payloads):
    return evidence.admit_primary_inputs(CIK, (declaration,), payloads)[0]


def assert_unavailable(declaration, payloads):
    result = admit(declaration, payloads)
    assert isinstance(result, UnavailablePrimaryInput) and result.reason
    assert result.original == declaration.original and result.original is not declaration.original
    assert result.target_origin == declaration.target_origin
    assert result.target_origin is not declaration.target_origin
    assert pickle.loads(pickle.dumps(result)) == result
    return result


def test_exact_target99_is_copied_without_value_or_role_inference():
    declaration, payloads, tree = plan()
    before_tree, before_payloads = copy.deepcopy(tree), copy.deepcopy(payloads)
    result = admit(declaration, payloads)
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.target_origin.pointer.endswith("/99/val")
    assert result.mapping.target_origin.lexical_text == "2.33"
    assert result.original.value == Decimal("2.06")
    assert result.mapping.target_origin is not declaration.target_origin
    assert result.mapping.target_origin.source is not declaration.target_origin.source
    assert tree == before_tree and payloads == before_payloads
    assert pickle.loads(pickle.dumps(result)) == result and copy.deepcopy(result) == result


def test_equal98_is_not_discovered_as99():
    declaration, payloads, tree = plan()
    target = declaration.target_origin
    forged = replace(target, pointer=target.pointer.replace("/99/", "/98/"))
    assert_unavailable(replace(declaration, target_origin=forged), payloads)
    target98 = tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][98][sec_json.ORIGIN_KEY]
    assert target98.lexical_text == target.lexical_text and target98 != target
    result = admit(replace(declaration, target_origin=target98), payloads)
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.target_origin == target98 and result.mapping.target_origin != target


@pytest.mark.parametrize("field,value", [
    ("pointer", "/absent/val"), ("pointer", "/facts/us-gaap/EarningsPerShareDiluted/units/USD/shares/99/val"),
    ("lexical_text", "2.330"), ("namespace", "ifrs-full"), ("concept", "EarningsPerShareBasic"),
    ("unit", "CNY/shares"), ("start", "2019-01-01"), ("end", "2019-12-31"),
    ("accession", "0001213900-22-020106"), ("form", "10-K"), ("filed", "2023-03-23"),
    ("start", None), ("end", None), ("accession", None), ("form", None), ("filed", None),
])
def test_exact_original_context_and_lexical_guard(field, value):
    declaration, payloads, _ = plan()
    assert_unavailable(replace(declaration, target_origin=replace(declaration.target_origin, **{field: value})), payloads)


@pytest.mark.parametrize("field", ["pointer", "lexical_text", "namespace", "concept", "unit", "start", "end", "accession", "form", "filed"])
def test_mutable_origin_fields_are_unavailable_before_comparison(field):
    declaration, payloads, _ = plan()
    assert_unavailable(replace(declaration, target_origin=replace(declaration.target_origin, **{field: ["mutable"]})), payloads)


@pytest.mark.parametrize("field,value", [
    ("role", ["company_facts"]), ("artifact_sha256", ["bad"]), ("source_document", ["bad"]),
    ("fidelity", ["EXACT_LEXICAL"]), ("role", "other"), ("artifact_sha256", "0" * 64),
    ("source_document", ""), ("source_document", " \n"), ("fidelity", "MISSING"),
    ("source_item_id", True), ("source_observation_id", True), ("occurrence_id", True),
    ("source_item_id", 0), ("source_observation_id", -1), ("occurrence_id", None),
    ("occurrence_id", 0), ("occurrence_id", "3"), ("source_item_id", [1]),
])
def test_target_captured_identity_is_immutable_and_delivery_explicit(field, value):
    declaration, payloads, _ = plan()
    changed = replace(declaration.target_origin.source, **{field: value})
    assert_unavailable(replace(declaration, target_origin=replace(declaration.target_origin, source=changed)), payloads)


def test_old_none_delivery_decoder_contract_remains_unchanged():
    declaration, payloads, tree = plan(occurrence=None)
    assert declaration.target_origin.source.occurrence_id is None
    sec_json.verify_origin(declaration.target_origin, tree)
    assert_unavailable(declaration, payloads)


@pytest.mark.parametrize("kind", ["missing", "changed", "reordered", "mutable-bytes", "wrong-issuer", "missing-issuer", "bool-issuer", "zero-origin"])
def test_body_or_origin_missing_changed_foreign_is_unavailable(kind):
    declaration, payloads, _ = plan()
    key = declaration.target_origin.source.artifact_sha256
    if kind == "missing":
        del payloads[key]
    elif kind == "changed":
        payloads[key] += b" "
    elif kind == "reordered":
        tree = json.loads(payloads[key])
        rows = tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
        rows[98], rows[99] = rows[99], rows[98]
        payloads[key] = json.dumps(tree).encode()
    elif kind == "mutable-bytes":
        payloads[key] = bytearray(payloads[key])
    elif kind in {"wrong-issuer", "missing-issuer", "bool-issuer"}:
        issuer = 999999 if kind == "wrong-issuer" else None if kind == "missing-issuer" else True
        declaration, payloads, _ = plan(cik=issuer)
    else:
        declaration = replace(declaration, target_origin="missing-origin")
    result = admit(declaration, payloads)
    assert isinstance(result, UnavailablePrimaryInput)
    assert result.target_origin == declaration.target_origin


def test_fresh_byte_decode_not_a_mutated_arbitrary_tree():
    declaration, payloads, tree = plan()
    tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][99]["val"] = 999
    sec_json.verify_origin(declaration.target_origin, tree)  # membership helper alone has this ceiling
    result = admit(declaration, payloads)
    assert isinstance(result, AdmittedPrimaryInput) and result.mapping.target_origin.lexical_text == "2.33"


@pytest.mark.parametrize("kind", ["reorder", "lexical", "metadata"])
def test_rehashed_new_body_cannot_reuse_old_pointer_metadata(kind):
    declaration, payloads, _ = plan()
    source = declaration.target_origin.source
    raw = payloads[source.artifact_sha256]
    if kind == "lexical":
        raw = raw.replace(b'"val": 2.33, "start": "2020-01-01", "end": "2020-12-31", "accn": "0001213900-23-022083"',
                          b'"val": 2.330, "start": "2020-01-01", "end": "2020-12-31", "accn": "0001213900-23-022083"')
    else:
        tree = json.loads(raw)
        rows = tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"]
        if kind == "reorder":
            rows[98], rows[99] = rows[99], rows[98]
        else:
            rows[99]["filed"] = "2023-03-23"
        raw = json.dumps(tree).encode()
    assert hashlib.sha256(raw).hexdigest() != source.artifact_sha256
    changed_source = replace(source, artifact_sha256=hashlib.sha256(raw).hexdigest())
    declaration = replace(declaration, target_origin=replace(declaration.target_origin, source=changed_source))
    payloads[changed_source.artifact_sha256] = raw
    assert_unavailable(declaration, payloads)


@pytest.mark.parametrize("token", ["-0.0000", "9007199254740993.1234567890123456789", "1e-400", "-14205925"])
def test_exact_target_token_survives_without_native_amount_equality(token):
    declaration, payloads, _ = plan(token=token)
    declaration = replace(declaration, unary_factor_text="-1")
    result = admit(declaration, payloads)
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.target_origin.lexical_text == token
    assert result.original.value == Decimal("2.06") and result.mapping.unary_factor_text == "-1"


def test_escaped_concept_and_unit_pointers_use_existing_decoder():
    declaration, payloads, _ = plan(concept="X~Y/Z")
    result = admit(declaration, payloads)
    assert isinstance(result, AdmittedPrimaryInput)
    assert result.mapping.target_origin.pointer == "/facts/us-gaap/X~0Y~1Z/units/USD~1shares/99/val"


def test_duplicate_target_rejects_both_even_with_other_delivery_and_native_identity():
    first, payloads, _ = plan()
    body2, request2 = fixture("3.01")
    changed_source = replace(first.target_origin.source, occurrence_id=4)
    second = replace(mapping(body2, request2), key="second", target_origin=replace(first.target_origin, source=changed_source))
    payloads[request2.source.artifact_sha256] = body2
    results = evidence.admit_primary_inputs(CIK, (first, second), payloads)
    assert all(isinstance(r, UnavailablePrimaryInput) and "duplicate primary target" in r.reason for r in results)
    assert [r.target_origin for r in results] == [first.target_origin, second.target_origin]


def test_distinct_delivery_and_pointer_use_full_source_decode_cache(monkeypatch):
    first, payloads, tree = plan()
    body2, request2 = fixture("3.01")
    origin98 = tree["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"][98][sec_json.ORIGIN_KEY]
    second = replace(mapping(body2, request2), key="second", target_origin=replace(origin98, source=replace(origin98.source, occurrence_id=4)))
    payloads[request2.source.artifact_sha256] = body2
    calls = []
    owner = sec_json.decode_company_facts
    def traced(raw, *, source):
        calls.append(source)
        return owner(raw, source=source)
    monkeypatch.setattr(sec_json, "decode_company_facts", traced)
    results = evidence.admit_primary_inputs(CIK, (first, second), payloads)
    assert all(isinstance(r, AdmittedPrimaryInput) for r in results)
    assert [r.mapping.target_origin.source.occurrence_id for r in results] == [3, 4]
    assert len(calls) == 2  # same bytes must not substitute a different delivery's origin
    calls.clear()
    second = replace(second, target_origin=origin98)
    results = evidence.admit_primary_inputs(CIK, (first, second), payloads)
    assert all(isinstance(r, AdmittedPrimaryInput) for r in results) and len(calls) == 1


def test_failed_native_still_retains_valid_cf_target():
    declaration, payloads, _ = plan()
    del payloads[declaration.original.source.artifact_sha256]
    assert_unavailable(declaration, payloads)


@pytest.mark.parametrize("kind", ["missing-document", "missing-replacement", "both-operations"])
def test_valid_target_cannot_bypass_existing_evidence_and_operation_admission(kind):
    declaration, payloads, _ = plan()
    if kind == "missing-document":
        declaration = replace(declaration, documents=())
    else:
        _, request = fixture("3.01")
        declaration = replace(declaration, replacement=request,
                              unary_factor_text="-1" if kind == "both-operations" else None)
    assert_unavailable(declaration, payloads)


def test_native_only_and_loader_default_compatibility(monkeypatch, tmp_path):
    body, request = fixture()
    native = mapping(body, request)
    assert native.target_origin is None
    result = admit(native, {request.source.artifact_sha256: body})
    assert isinstance(result, AdmittedPrimaryInput) and result.mapping.target_origin is None
    assert evidence.admit_primary_inputs(CIK, (), {}) == ()
    monkeypatch.setattr(evidence.inline_xbrl, "load_retained_supplement", lambda *a, **k: (a[2], "missing"))
    monkeypatch.setattr(evidence.dera, "load_sidecar", lambda *a: None)
    loader = evidence.EvidenceLoader.__new__(evidence.EvidenceLoader)
    loader.edgar = SimpleNamespace(cache_dir=tmp_path)
    loader._inline_cache_dir = tmp_path
    loader._inline_retained_root = None
    monkeypatch.setattr(loader, "identity", lambda *a: ("FTFT", None))
    declaration, payloads, tree = plan()
    bundle = loader.load(CIK, "FTFT", tree, primary_mappings=(declaration,), primary_payloads=payloads)
    assert bundle.primary_inputs[0].mapping.target_origin == declaration.target_origin
    assert bundle.facts == tree and bundle.original_company_facts is None
