"""An independent audit re-proves class and capital sources from original bytes."""
import copy
import io
import json
from pathlib import Path
import zipfile

import pytest

from screener import audit, evidence, sync
from tests.test_jgaap_parent_equity import _archive, _bundle


def source_case(tmp_path, *, preferred=False, flows=False, stale=False, oci="5", serialized=True):
    archive = _archive(oci=oci, **({"class_label": "Ａ種優先株式", "class_value": "2"} if preferred else {}))
    with zipfile.ZipFile(io.BytesIO(archive)) as old:
        xml = old.read("XBRL/PublicDoc/annual.xbrl")
    xml = xml.replace(b'xmlns:j=', b'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:j=')
    xml = xml.replace(b'<x:measure>JPY</x:measure>', b'<x:measure>iso4217:JPY</x:measure>')
    xml = xml.replace(b'<x:measure>USD</x:measure>', b'<x:measure>iso4217:USD</x:measure>')
    if flows:
        flow_context = "duration"
        if stale:
            flow_context = "old-income"
            xml = xml.replace(b'</x:xbrl>', b'<x:context id="old-income"><x:entity><x:identifier scheme="http://disclosure.edinet-fsa.go.jp">E03475-000</x:identifier></x:entity>'
                              b'<x:period><x:startDate>2022-07-01</x:startDate><x:endDate>2023-06-30</x:endDate></x:period></x:context></x:xbrl>')
        xml = xml.replace(b'</x:xbrl>', (f'<j:ProfitLossAttributableToOwnersOfParent contextRef="{flow_context}" unitRef="JPY">21</j:ProfitLossAttributableToOwnersOfParent>'
                          f'<j:NetSales contextRef="{flow_context}" unitRef="JPY">2000000</j:NetSales></x:xbrl>').encode())
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as source:
        source.writestr("XBRL/PublicDoc/annual.xbrl", xml)
    archive = out.getvalue()
    return retained_row(tmp_path, archive, serialized=serialized)


def retained_row(tmp_path, archive, *, entity="E03475", document="S100TEST", security="30280", serialized=True):
    facts = _bundle(archive, entity=entity, document=document, security=security)
    row = sync._derive_evidence_result(evidence.EvidenceBundle(entity, security[:-1] + ".T", facts, None, None)).row
    (tmp_path / "edinet").mkdir(exist_ok=True)
    (tmp_path / "edinet" / (document + ".zip")).write_bytes(archive)
    (tmp_path / ("companyfacts_" + entity + ".json")).write_text(json.dumps(facts))
    return (json.loads(json.dumps(row)) if serialized else row), audit._facts(entity, tmp_path)


def test_original_capital_plus_oci_and_class_table_prove_equity_not_assets_less_liabilities(tmp_path):
    row, facts = source_case(tmp_path)
    source = row["equity_evidence"]["current"]
    assert source["value"] == 105
    assert audit._equity_source_value(facts, source, common=True) == (105, None)
    # No mapper/canonical value acts as the independent oracle.
    facts["facts"] = {"canonical": {"StockholdersEquity": {"units": {"JPY": [{"val": 180}]}}}}
    assert audit._equity_source_value(facts, source, common=True) == (105, None)
    row.update(shares=10, bvps=10.5, tbvps=10.5, goodwill=0, intangibles=0, debt=21, debt_to_equity=.2)
    checks = {name: (shown, expected) for name, shown, expected, *_ in audit._identities(row)}
    assert checks["bvps"] == (10.5, 10.5)
    assert checks["debt_to_equity"] == (.2, .2)
    assert row["total_liabilities"] == 20


@pytest.mark.parametrize("fault", ["member", "ordinal", "context", "header", "class", "archive", "entity", "endpoint", "omitted-class"])
def test_declared_ordinary_only_witness_is_not_its_own_oracle(tmp_path, fault):
    row, facts = source_case(tmp_path)
    item = copy.deepcopy(row["equity_evidence"]["current"])
    witness = item["source"]["equity_class_evidence"][0]
    if fault == "member":
        witness["member_sha256"] = "0" * 64
    elif fault in {"ordinal", "context"}:
        witness["location"]["element_ordinal" if fault == "ordinal" else "context_ref"] = 0 if fault == "ordinal" else "wrong"
    elif fault == "header":
        witness["header_lexical"] = "unverified header"
    elif fault == "class":
        witness["classes"][0][0] = "ordinary asserted"
    elif fault == "archive":
        item["source"]["components"][0]["source_artifact_sha256"] = "0" * 64
    elif fault == "entity":
        for part in item["source"]["components"]:
            part["source_entity"] = "E99999-000"
    elif fault == "endpoint":
        witness["as_of"] = "2025-06-30"
    else:
        witness["location"]["class_rows"] = []
    value, reason = audit._equity_source_value(facts, item, common=True)
    assert value is None and reason


def test_preferred_endpoint_cannot_become_common_by_forging_the_outcome(tmp_path):
    row, facts = source_case(tmp_path, preferred=True)
    assert row["equity_evidence"]["current"] is None
    parent = copy.deepcopy(row["equity_evidence"]["parent"])
    witness = copy.deepcopy(parent["source"]["components"][0]["equity_class_evidence"][0])
    witness["outcome"] = "ORDINARY_ONLY"
    parent["source"]["equity_class_evidence"] = [witness]
    assert audit._equity_source_value(facts, parent, common=False) == (105, None)
    assert audit._equity_source_value(facts, parent, common=True)[0] is None
    row.update(bvps=18, shares=10)
    assert any(kind == "DERIVED" and field == "bvps" for kind, field, *_ in audit._equity_checks(row, facts))


def test_missing_opening_preferred_claim_does_not_prove_average_equity(tmp_path):
    row, facts = source_case(tmp_path)
    row["annual_ratios"] = {"2026": {"end": "2026-06-30", "common_equity": 105, "average_common_equity": 100}}
    row["equity_evidence"]["annual"] = {"2026": row["equity_evidence"]["current"]}
    assert any(kind == "UNCHECKED" and field == "FY2026 average_common_equity"
               for kind, field, *_ in audit._equity_checks(row, facts))


def test_wrong_equity_sum_is_not_passed_as_a_reported_leaf(tmp_path):
    row, facts = source_case(tmp_path)
    row["equity_evidence"]["current"]["value"] = 180
    assert any(kind == "DERIVED" and field == "equity_evidence.current" and expected == 105
               for kind, field, shown, expected, *_ in audit._equity_checks(row, facts))


@pytest.mark.parametrize("slot", ["current", "annual"])
def test_real_equity_source_cannot_be_relabelled_into_another_consumer_period(tmp_path, slot):
    row, facts = source_case(tmp_path)
    actual = copy.deepcopy(row["equity_evidence"]["current"])
    assert audit._equity_source_value(facts, actual, common=True) == (105, None)
    if slot == "current":
        row["balance_sheet_date"] = "2027-06-30"
        field = "equity_evidence.current"
    else:
        row["equity_evidence"]["annual"] = {"2025": actual}
        row["annual_ratios"] = {"2025": {"end": "2025-06-30", "common_equity": 105}}
        field = "equity_evidence.annual 2025"
    checks = list(audit._equity_checks(row, facts))
    assert any(kind == "DERIVED" and name == field and "consumer endpoint" in why
               for kind, name, shown, expected, why in checks)
    assert not any(kind == "DERIVED-OK" and name == field for kind, name, *_ in checks)
    public = audit.audit([row], tmp_path, quiet=True)
    assert any(line[1] == field for _, lines in public["problems"] for line in lines)


def test_equity_audit_reports_missing_consumer_period_and_current_formula(tmp_path):
    row, facts = source_case(tmp_path)
    row.pop("balance_sheet_date")
    assert any(kind == "UNCHECKED" and field == "equity_evidence.current"
               for kind, field, *_ in audit._equity_checks(row, facts))
    row.update(shares=10, bvps=10.5)
    assert next(line[3] for line in audit._identities(row) if line[0] == "bvps") == "source-qualified current common equity / shares"


@pytest.mark.parametrize("field,value", [("book_value", 180), ("on_book", 40), ("on_book", None)])
def test_current_book_profitability_value_presence_and_return_are_checked(tmp_path, field, value):
    row, facts = source_case(tmp_path, flows=True)
    assert row["profitability"]["book_value"] == 105
    assert row["profitability"]["on_book"] == 20
    assert not any(kind == "DERIVED" for kind, *_ in audit._equity_checks(row, facts))
    row["profitability"][field] = value
    assert any(kind == "DERIVED" and name == "profitability." + field
               for kind, name, *_ in audit._equity_checks(row, facts))


def test_stale_income_cannot_produce_current_return_on_book(tmp_path):
    row, facts = source_case(tmp_path, flows=True, stale=True)
    assert row["profitability"]["on_book"] is None
    assert not any(kind == "DERIVED" for kind, *_ in audit._equity_checks(row, facts))
    row["profitability"]["on_book"] = 20
    assert any(kind == "DERIVED" and name == "profitability.on_book" and expected is None
               for kind, name, shown, expected, *_ in audit._equity_checks(row, facts))


@pytest.mark.parametrize("field", ["book_value", "on_book"])
def test_unavailable_common_cannot_show_book_profitability(tmp_path, field):
    row, facts = source_case(tmp_path, flows=True, preferred=True)
    assert row["equity_evidence"]["current"] is None
    assert row["profitability"][field] is None
    row["profitability"][field] = 105
    assert any(kind == "DERIVED" and name == "profitability." + field
               for kind, name, *_ in audit._equity_checks(row, facts))


def test_unavailable_required_common_retains_explicit_null_to_block_profile_fallback(tmp_path):
    row, facts = source_case(tmp_path, preferred=True)
    assert "common_equity" in row["asset_quality"] and row["asset_quality"]["common_equity"] is None
    assert not any(kind == "DERIVED" for kind, *_ in audit._equity_checks(row, facts))
    del row["asset_quality"]["common_equity"]
    assert any(kind == "DERIVED" and field == "asset_quality.common_equity"
               for kind, field, *_ in audit._equity_checks(row, facts))


def test_book_profitability_year_must_be_the_selected_qualifying_year(tmp_path):
    row, facts = source_case(tmp_path, flows=True)
    row["profitability"]["fiscal_year"] -= 1
    assert any(kind == "DERIVED" and name == "profitability.fiscal_year"
               for kind, name, *_ in audit._equity_checks(row, facts))


@pytest.mark.parametrize("oci,book", [("-100", 0), ("-101", -1)])
def test_measured_zero_or_negative_common_book_remains_an_amount_without_a_return(tmp_path, oci, book):
    row, facts = source_case(tmp_path, flows=True, oci=oci)
    assert row["equity_evidence"]["current"]["value"] == book
    assert row["profitability"]["book_value"] == book
    assert row["profitability"]["on_book"] is None
    assert not any(kind == "DERIVED" for kind, *_ in audit._equity_checks(row, facts))
    row["profitability"]["book_value"] = None
    assert any(kind == "DERIVED" and name == "profitability.book_value"
               for kind, name, *_ in audit._equity_checks(row, facts))


@pytest.mark.parametrize("field", ["bvps", "tbvps", "debt_to_equity"])
def test_available_current_book_consumers_cannot_disappear(tmp_path, field):
    row, facts = source_case(tmp_path)
    row.update(shares=10, goodwill=0, intangibles=0, debt=21, bvps=10.5, tbvps=10.5, debt_to_equity=.2)
    row["asset_quality"].update(goodwill_to_common_equity=0, intangibles_to_common_equity=0,
                                goodwill_and_intangibles_to_common_equity=0)
    assert not any(kind == "DERIVED" for kind, *_ in audit._equity_checks(row, facts))
    row[field] = None
    assert any(kind == "DERIVED" and name == field for kind, name, *_ in audit._equity_checks(row, facts))


def test_colluding_historical_book_and_price_ratio_are_not_complete_dependency_proof(tmp_path):
    row, facts = source_case(tmp_path, flows=True)
    cell = row["annual_ratios"]["2026"]
    cell.update(bvps=100, tbvps=50, price=10, pb=.1, ptbv=.2)
    arithmetic = [check for check in audit._derived_series(row) if check[0] in {
        "annual_ratios.2026.pb", "annual_ratios.2026.ptbv"}]
    assert arithmetic and all(audit._close(shown, expected) for _, shown, expected, *_ in arithmetic)
    proof = list(audit._equity_checks(row, facts))
    for metric in ("bvps", "tbvps", "pb", "ptbv"):
        assert any(kind == "UNCHECKED" and name == "annual_ratios.2026." + metric
                   for kind, name, *_ in proof)


def test_public_audit_accepts_actual_in_memory_and_serialized_year_maps_equally(tmp_path):
    row, facts = source_case(tmp_path, flows=True, serialized=False)
    assert 2026 in row["annual_ratios"] and "2026" in row["annual_net_income"]
    assert row["annual_ratios"][2026]["return_on_book"] == 20
    serialized = json.loads(json.dumps(row))
    before = copy.deepcopy(row)
    memory_result = audit.audit([row], tmp_path, quiet=True)
    json_result = audit.audit([serialized], tmp_path, quiet=True)
    assert row == before
    assert memory_result == json_result
    assert memory_result["problems"] == []
    assert any(kind == "DERIVED-OK" and field == "FY2026 return_on_book" and expected == 20
               for kind, field, shown, expected, *_ in audit._equity_checks(row, facts))
    row["annual_ratios"][2026]["return_on_book"] = 40
    wrong_serialized = json.loads(json.dumps(row))
    for variant in (row, wrong_serialized):
        checked = audit.audit([variant], tmp_path, quiet=True)
        assert any(field == "FY2026 return_on_book" and shown == 40 and expected == 20
                   for _, lines in checked["problems"] for kind, field, shown, expected, *_ in lines)


@pytest.mark.parametrize("document,entity,security,expected", [
    ("S100Z2YO", "E03475", "30280", 126359000000),
    ("S100Z32V", "E01991", "69200", 246828000000),
    ("S100Z3CP", "E30880", "33000", 9742482000),
    ("S100Z3CG", "E32988", "65470", 12668000000),
])
def test_original_four_issuer_class_and_equity_sources(tmp_path, document, entity, security, expected):
    path = Path.home() / ".cache/graham-screener/edinet" / (document + ".zip")
    if not path.is_file():
        pytest.skip("original retained EDINET source unavailable")
    row, facts = retained_row(tmp_path, path.read_bytes(), entity=entity, document=document, security=security)
    assert audit._equity_source_value(facts, row["equity_evidence"]["current"], common=True) == (expected, None)
    checked = audit.audit([row], tmp_path, quiet=True)
    assert checked["problems"] == []
    assert not any("equity" in line[1] for _, line in checked["uncheckable"])
    if entity == "E32988":
        assert "2025" not in row["equity_evidence"]["annual"]
        assert (row.get("annual_ratios") or {}).get("2026", {}).get("average_common_equity") is None
