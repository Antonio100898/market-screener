"""Synthetic lexical/operation contracts; genuine source proof is recorded separately."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
import pickle
from types import SimpleNamespace

import pytest

from helpers import GAAP, OE_GAAP, dur, facts_doc, tagdata
from screener import exact_annual, normalize, sync
from screener.evidence import EvidenceBundle
from screener.models import CapturedSource
from screener.sources.sec_json import ORIGIN_KEY, decode_company_facts, decode_object


def source(raw, fidelity="EXACT_LEXICAL"):
    return CapturedSource("company_facts", hashlib.sha256(raw).hexdigest(),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000001234.json", 1, 2, 3, fidelity)


def annotated(value=None, fidelity="EXACT_LEXICAL"):
    gaap = deepcopy(OE_GAAP)
    gaap["RevenueFromContractWithCustomerExcludingAssessedTax"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 300e9, accn="k25", filed="2026-02-15")])
    gaap["GrossProfit"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 200e9, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15")])
    value = value or {**facts_doc(gaap), "cik": 1234}
    raw = json.dumps(value).encode()
    captured = source(raw, fidelity)
    return raw, captured, decode_company_facts(raw, source=captured)


@pytest.mark.parametrize("token", ["9007199254740993.1234567890123456789", "-0.0000", "1.2300", "12e+3", "1e-400"])
def test_original_numeric_lexeme_pointer_and_compatibility(token):
    raw = ('{"cik":1234,"facts":{"us-gaap":{"X~Y":{"units":{"USD/shares":['
           '{"val":' + token + ',"start":"2025-01-01","end":"2025-12-31",'
           '"accn":"a","form":"10-K","filed":"2026-02-01"}]}}}}}').encode()
    captured = source(raw)
    parsed = decode_company_facts(raw, source=captured)
    entry = parsed["facts"]["us-gaap"]["X~Y"]["units"]["USD/shares"][0]
    origin = entry[ORIGIN_KEY]
    assert origin.lexical_text == token
    assert origin.pointer == "/facts/us-gaap/X~0Y/units/USD~1shares/0/val"
    assert entry["val"] == json.loads(raw)["facts"]["us-gaap"]["X~Y"]["units"]["USD/shares"][0]["val"]
    assert type(parsed["cik"]) is int
    assert pickle.loads(pickle.dumps(origin)) == origin


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"x":1e400}', b'\xff', b'[]', b'{"_exact_sec_origin":{}}'])
def test_strict_unknown_or_forged_input_rejected(raw):
    with pytest.raises(ValueError):
        decode_object(raw)


def test_exact_source_byte_identity_and_legacy_fidelity():
    raw, captured, facts = annotated(fidelity="DECODED_DECIMAL")
    assert decode_object(b'\xef\xbb\xbf{"a":1}') == {"a": 1}
    with pytest.raises(ValueError, match="identity"):
        decode_company_facts(raw + b" ", source=captured)
    with pytest.raises(ValueError):
        decode_company_facts(raw, source=replace(captured, source_item_id=True))
    chosen = normalize._annual_series(facts["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[2025]
    assert chosen.exact_origin.source.fidelity == "DECODED_DECIMAL"


def test_actual_winner_equal_value_original_indexes_not_amount_matching():
    entries = [dur("2025-01-01", "2025-12-31", 9, accn="old", filed="2026-02-01"),
               dur("2025-01-01", "2025-12-31", 9, accn="new", filed="2026-03-01")]
    raw, captured, facts = annotated(facts_doc({"NetIncomeLoss": tagdata("USD", entries)}))
    fact = normalize._annual_series(facts["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[2025]
    assert fact.exact_origin.pointer.endswith("/1/val")
    assert fact.exact_origin.accession == "new" and fact.provenance.accession == "new"
    assert fact == replace(fact, exact_origin=None) and hash(fact) == hash(replace(fact, exact_origin=None))
    assert pickle.loads(pickle.dumps(fact)).exact_origin == fact.exact_origin
    narrowed = normalize._with_fiscal_calendar(facts["facts"]["us-gaap"])
    assert normalize._annual_series(narrowed, "NetIncomeLoss", ("USD",))[2025].exact_origin == fact.exact_origin
    assert normalize._fact("x", "x", {"_fact_override": fact}) is fact


def test_ifrs_mapping_and_basic_metadata_wrapper_retain_original():
    raw, captured, facts = annotated(facts_doc({"EarningsPerShareBasic": tagdata("USD/shares", [
        dur("2025-01-01", "2025-12-31", 9)])}))
    basic = normalize._fill_missing_years({}, facts["facts"]["us-gaap"])[2025]
    assert basic.exact_origin is not None and "basic" in basic.provenance.concept
    value = {"facts": {"ifrs-full": {"Revenue": tagdata("EUR", [dur("2025-01-01", "2025-12-31", 9)])}}}
    raw, captured, facts = annotated(value)
    mapped = normalize._ifrs_as_us_gaap(facts["facts"]["ifrs-full"])
    fact = normalize._annual_series(mapped, "Revenues", ("EUR",))[2025]
    assert fact.exact_origin.namespace == "ifrs-full" and fact.exact_origin.concept == "Revenue"
    assert fact.exact_unassessed_reason is None


def test_factor_one_receipt_and_split_decisions_do_not_certify_output_basis():
    raw, captured, facts = annotated()
    eps = normalize._annual_series(facts["facts"]["us-gaap"], "EarningsPerShareDiluted")[2025]
    split = normalize._split_adjust({2025: eps}, facts["facts"]["us-gaap"])[2025]
    assert split.value == eps.value and split.exact_origin == eps.exact_origin
    assert split.exact_unassessed_reason == "UNKNOWN_ACTION_INCORPORATION"
    parts = {"annual_eps": {2025: eps}}
    normalize._restate_onto_receipt(parts, Decimal(1), "cover")
    converted = parts["annual_eps"][2025]
    assert converted.value == eps.value and converted.exact_origin == eps.exact_origin
    assert converted.exact_unassessed_reason == "RECEIPT_BASIS_OPERATION_UNTRACED"


def test_source_context_change_cannot_borrow_reported_origin():
    raw, captured, facts = annotated()
    e = normalize._entries(facts["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[0]
    changed = {**e, "_source_accession": "other", "_annual_accession": "wrapper",
               "_annual_filed": "2026-03-01", "_annual_form": "20-F"}
    fact = normalize._fact("Net income", "NetIncomeLoss", changed)
    assert fact.exact_origin is e[ORIGIN_KEY]
    assert fact.exact_unassessed_reason == "SOURCE_CONTEXT_CHANGED"


def test_real_successful_snapshots_two_three_builds_and_six_family_translation(monkeypatch):
    raw, captured, facts = annotated()
    bundle = EvidenceBundle("0000001234", "TEST", facts, None, None, captured_sources=(captured,))
    builds = []
    build = sync.build_snapshot
    def observe(*args, **kwargs):
        snap = build(*args, **kwargs)
        builds.append((kwargs["assume_absent_zero"], snap))
        return snap
    monkeypatch.setattr(sync, "build_snapshot", observe)
    result = sync._derive_evidence_result(bundle)
    assert result.status == "ok" and len(builds) == 2
    assert result.snapshot is builds[0][1] and result.rq_result.snapshot is builds[1][1]
    prepared = exact_annual.prepare(result, bundle.captured_sources, cik=bundle.cik, original_facts=facts)
    assert len(builds) == 2
    stored = SimpleNamespace(security_id=1, snapshot_id=2, snapshot_sha256="a"*64, engine_revision=190)
    graph = prepared.bind(stored, pipeline_run_key="actual-synthetic-build", security_basis="PRIMARY_COMMON_SHARE", receipt_ratio=None)
    assert {row.metric_family for row in graph.coverage} == set(exact_annual.FAMILIES)
    assert all(row.assessment == "PARTIAL" for row in graph.coverage)
    direct = next(n for n in graph.selections if n.metric_path == "annual_net_income.2025" and n.view == "STRICT")
    assert direct.state == "REPORTED" and direct.value_decimal_text == "70000000000.0"
    eps = next(n for n in graph.selections if n.metric_path == "annual_eps.2025" and n.view == "STRICT")
    assert eps.state == "NOT_ASSESSED" and eps.value_decimal_text is None
    assert sync._derive_evidence(bundle, assume_absent_zero=True)[0] == "ok" and len(builds) == 3
    # Compare complete numeric serialization to the same legacy unannotated inputs.
    plain = EvidenceBundle(bundle.cik, bundle.ticker, json.loads(raw), None, None)
    plain_status, plain_row = sync._derive_evidence(plain)
    assert plain_status == result.status
    comparable = deepcopy(result.row)
    assert set(result.snapshot.annual_eps) == {2021, 2022, 2023, 2024, 2025}
    for year, fact in result.snapshot.annual_eps.items():
        assert fact.exact_unassessed_reason == "UNKNOWN_ACTION_INCORPORATION"
        source = comparable["annual_sources"]["annual_eps"][str(year)]
        assert source.pop("operation_unassessed_reason") == fact.exact_unassessed_reason
        assert "operation_unassessed_reason" not in plain_row["annual_sources"]["annual_eps"][str(year)]
    assert plain_row == comparable
    json.dumps(result.row)


@pytest.mark.parametrize("token", ["9007199254740993.1234567890123456789", "1e-400", "-0"])
def test_precision_loss_is_observation_evidence_not_reported_selection(token):
    raw = json.dumps(facts_doc({"NetIncomeLoss": tagdata("USD", [dur("2025-01-01", "2025-12-31", "TOKEN")])})).replace('"TOKEN"', token).encode()
    captured = source(raw)
    facts = decode_company_facts(raw, source=captured)
    fact = normalize._annual_series(facts["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[2025]
    snap = SimpleNamespace(**{family: {} for family in exact_annual.FAMILIES})
    snap.annual_net_income = {2025: fact}
    prepared = exact_annual.prepare(sync.DeriveResult("ok", {}, snap), (captured,), cik="0000001234", original_facts=facts)
    assert prepared.observations[0].source_lexical_text == token
    assert prepared.selections[0]["state"] == "NOT_ASSESSED"
    assert prepared.selections[0]["reason"] == "COMPATIBILITY_PRECISION_LOSS"


def test_pending_fallback_retains_only_the_snapshot_that_produced_each_row(monkeypatch):
    raw, captured, facts = annotated()
    build = sync.build_snapshot
    attempts, successes = [], []
    def pending_then_fallback(*args, **kwargs):
        assumed = kwargs["assume_absent_zero"]
        attempts.append(assumed)
        if "foreign_identity_filing" not in kwargs:
            raise normalize.PendingFilingFactsError(("2026-09-28", "missing-annual"))
        snap = build(*args, **kwargs)
        successes.append(snap)
        return snap
    monkeypatch.setattr(sync, "build_snapshot", pending_then_fallback)
    result = sync._derive_evidence_result(EvidenceBundle("0000001234", "TEST", facts, None, None))
    assert result.status == "pending_facts" and attempts == [False, False, True, True]
    assert result.snapshot is successes[0] and result.rq_result.snapshot is successes[1]
    assert result.row["data_pending"]["accession"] == "missing-annual"


@pytest.mark.parametrize("failure", [normalize.UnsupportedFilerError("unsupported"), RuntimeError("bad filing")])
def test_failed_current_attempt_cannot_supply_snapshot(monkeypatch, failure):
    monkeypatch.setattr(sync, "build_snapshot", lambda *a, **k: (_ for _ in ()).throw(failure))
    result = sync._derive_evidence_result(EvidenceBundle("0000001234", "TEST", {}, None, None))
    assert result.snapshot is None and result.rq_result is None
    with pytest.raises(ValueError, match="successful Snapshot"):
        exact_annual.prepare(result, (), cik="0000001234")


def test_offsetting_split_events_are_not_basis_witnesses():
    # Two independent, opposed inferred actions net to1, which cannot prove incorporation.
    entries = [dur("2017-01-01", "2017-12-31", 10, accn="first", filed="2018-02-01"),
        dur("2017-01-01", "2017-12-31", 5, accn="second", filed="2019-02-01"),
        dur("2017-01-01", "2017-12-31", 10, accn="third", filed="2021-02-01")]
    raw, captured, facts = annotated(facts_doc({"EarningsPerShareDiluted": tagdata("USD/shares", entries)}))
    gaap = facts["facts"]["us-gaap"]
    fact = normalize._fact("EPS", "EarningsPerShareDiluted", normalize._entries(gaap, "EarningsPerShareDiluted", ("USD/shares",))[0])
    assert normalize._split_factor(normalize._per_share_periods(gaap), "2018-02-01") == 1
    adjusted = normalize._split_adjust({2017: fact}, gaap)[2017]
    assert adjusted.value == fact.value and adjusted.exact_origin == fact.exact_origin
    assert adjusted.exact_unassessed_reason == "UNKNOWN_ACTION_INCORPORATION"


def test_existing_verified_already_restated_branch_is_not_an_emitted_action_trace(monkeypatch):
    cik, accession = "0001854275", "0001213900-25-059675"
    assert (cik, accession) in normalize._VERIFIED_ALREADY_SPLIT_ADJUSTED_ACCESSIONS
    # Synthetic numeric row exercises the existing verified branch; it is not filing evidence.
    raw, captured, facts = annotated(facts_doc({"EarningsPerShareDiluted": tagdata("USD/shares", [
        dur("2024-01-01", "2024-12-31", 9, accn=accession, filed="2025-07-01")])}))
    gaap = normalize._with_fiscal_calendar(facts["facts"]["us-gaap"], cik=cik)
    fact = normalize._annual_series(gaap, "EarningsPerShareDiluted")[2024]
    monkeypatch.setattr(normalize, "_split_factor", lambda *a, **k: pytest.fail("verified branch reinferred actions"))
    adjusted = normalize._split_adjust({2024: fact}, gaap)[2024]
    assert adjusted.value == fact.value and adjusted.provenance == fact.provenance
    assert adjusted.exact_origin == fact.exact_origin
    assert adjusted.exact_unassessed_reason == "UNKNOWN_ACTION_INCORPORATION"


@pytest.mark.parametrize("field,value", [("pointer", "/facts/us-gaap/NetIncomeLoss/units/USD/999/val"),
    ("unit", "EUR"), ("lexical_text", "70000000001.0"), ("start", "2025-04-01"),
    ("accession", "different-source"), ("concept", "GrossProfit")])
def test_original_pointer_context_cannot_be_reconstructed_or_tampered(field, value):
    raw, captured, facts = annotated()
    fact = normalize._annual_series(facts["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[2025]
    bad = replace(fact, exact_origin=replace(fact.exact_origin, **{field: value}))
    snapshot = SimpleNamespace(**{family: {} for family in exact_annual.FAMILIES})
    snapshot.annual_net_income = {2025: bad}
    with pytest.raises(ValueError, match="original"):
        exact_annual.prepare(sync.DeriveResult("ok", {}, snapshot), (captured,),
                             cik="0000001234", original_facts=facts)


def test_actual_inline_incorporated_binding_preserves_raw_origin_but_not_reported_output():
    from screener.sources.inline_xbrl import merge_missing_facts
    entry = dur("2025-01-01", "2025-12-31", 9, accn="original", filed="2026-02-01")
    raw, captured, facts = annotated({**facts_doc({"NetIncomeLoss": tagdata("USD", [entry])}), "cik": 1234})
    supplement = {"cik": 1234, "_inline_xbrl": {}, "facts": {"us-gaap": {"NetIncomeLoss": tagdata("USD", [
        {**entry, "accn": "current", "_source_accession": "original", "_annual_accession": "current",
         "_annual_filed": "2026-03-01", "_annual_form": "20-F"}])}}}
    merged = merge_missing_facts(facts, supplement)
    chosen = normalize._annual_series(merged["facts"]["us-gaap"], "NetIncomeLoss", ("USD",))[2025]
    assert chosen.exact_origin == facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0][ORIGIN_KEY]
    assert chosen.exact_unassessed_reason == "SOURCE_CONTEXT_CHANGED"
    snapshot = SimpleNamespace(**{family: {} for family in exact_annual.FAMILIES})
    snapshot.annual_net_income = {2025: chosen}
    prepared = exact_annual.prepare(sync.DeriveResult("ok", {}, snapshot), (captured,),
                                    cik="0000001234", original_facts=facts)
    assert prepared.selections[0]["state"] == "NOT_ASSESSED"
    assert prepared.observations[0].reference.location.pointer.endswith("/0/val")
