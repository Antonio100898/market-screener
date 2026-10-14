"""Independent raw-filing checks must not trust rebuilt canonical values."""
import copy
import ast
import json
import os
from pathlib import Path
import zipfile

import pytest

from screener import audit


MEMBER = "XBRL/PublicDoc/jpcrp030000-asr-001_E00009-000_2026-06-30_01_2026-09-24.xbrl"
SOURCE = {"tag": "jppfs_cor:Assets", "canonical_tag": "Assets", "document": "S100Z3O7",
          "accn": "S100Z3O7", "form": "JP-AR", "filed": "2026-09-24",
          "end": "2026-06-30", "unit": "JPY"}


def retained_edinet(tmp_path, *, entity="E00009-000", scheme="http://disclosure.edinet-fsa.go.jp",
                    currency_uri="http://www.xbrl.org/2003/iso4217", end="2026-06-30",
                    start="2025-07-01", dimensions="", duplicate="", cash="-1521000000"):
    # Portable synthetic bytes use literal native S100Z3O7 values and contexts.
    body = f'''<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:jppfs_cor="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
      xmlns:iso4217="{currency_uri}" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
      <xbrli:context id="instant"><xbrli:entity><xbrli:identifier scheme="{scheme}">{entity}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>{end}</xbrli:instant></xbrli:period>{dimensions}</xbrli:context>
      <xbrli:context id="annual"><xbrli:entity><xbrli:identifier scheme="{scheme}">{entity}</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate></xbrli:period>{dimensions}</xbrli:context>
      <xbrli:unit id="JPY"><xbrli:measure>iso4217:JPY</xbrli:measure></xbrli:unit>
      <jppfs_cor:Assets contextRef="instant" unitRef="JPY" decimals="-3">28461000000</jppfs_cor:Assets>
      <jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF contextRef="annual" unitRef="JPY" decimals="-3">{cash}</jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF>
      <jppfs_cor:NetCashProvidedByUsedInOperatingActivities contextRef="annual" unitRef="JPY" decimals="-3">-198197000</jppfs_cor:NetCashProvidedByUsedInOperatingActivities>
      {duplicate}</xbrli:xbrl>'''
    native = tmp_path / "edinet"
    native.mkdir()
    with zipfile.ZipFile(native / "S100Z3O7.zip", "w") as archive:
        archive.writestr(MEMBER, body)
    # Deliberately stale/colluding canonical cache; this is not the audit oracle.
    (tmp_path / "companyfacts_E00009.json").write_text(json.dumps({"facts": {"canonical": {
        "Assets": {"units": {"JPY": [{"val": 999, "accn": "S100Z3O7", "end": "2026-06-30",
          "_source_document": "S100Z3O7", "_source_tag": "Assets"}]}}
    }}}))


def row():
    return {"cik": "E00009", "ticker": "1381.T", "criteria": [],
            "total_assets": 28461000000, "sources": {"total_assets": dict(SOURCE)}}


def payment_source(**changes):
    return {**SOURCE, "tag": "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
            "canonical_tag": "PaymentsToAcquirePropertyPlantAndEquipment", "start": "2025-07-01",
            "concept": "PaymentsToAcquirePropertyPlantAndEquipment (cash outflow magnitude; source signed negative)",
            **changes}


def test_public_audit_uses_native_bytes_not_stale_canonical_and_never_fetches_sec(tmp_path):
    retained_edinet(tmp_path)
    class NoNetwork:
        def _get_text(self, url):
            pytest.fail("EDINET audit must not request SEC statements")
    result = audit.audit([row()], tmp_path, quiet=True, edgar=NoNetwork())
    assert result["totals"]["sourced_ok"] == 1
    assert result["totals"]["filing_ok"] == 1
    assert not result["problems"]
    witness = result["edinet_native_sources"][0]
    assert witness["member"] == MEMBER
    assert witness["occurrences"][0]["lexical"] == "28461000000"
    assert len(witness["archive_sha256"]) == len(witness["member_sha256"]) == 64
    colluding = row()
    colluding["total_assets"] = 999
    assert audit.audit([colluding], tmp_path, quiet=True)["totals"]["sourced_bad"] == 1


@pytest.mark.parametrize("changes", [
    {"entity": "E99999-000"}, {"scheme": "http://www.sec.gov/CIK"},
    {"currency_uri": "urn:not-iso4217"}, {"end": "2025-06-30"},
    {"dimensions": '<xbrli:scenario><xbrldi:explicitMember dimension="jppfs_cor:ConsolidatedOrNonConsolidatedAxis">jppfs_cor:NonConsolidatedMember</xbrldi:explicitMember></xbrli:scenario>'},
    {"duplicate": '<jppfs_cor:Assets contextRef="instant" unitRef="JPY">123</jppfs_cor:Assets>'},
])
def test_native_scope_or_ambiguous_value_cannot_pass_from_cached_json(tmp_path, changes):
    retained_edinet(tmp_path, **changes)
    result = audit.audit([row()], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 0
    assert result["totals"]["unchecked"] >= 1


def test_payment_magnitude_is_explicit_and_negative_ocf_keeps_its_sign(tmp_path):
    retained_edinet(tmp_path)
    facts = audit._facts("E00009", tmp_path)
    source = payment_source()
    assert audit._source_values(facts, source) == [1521000000]
    assert audit._source_values(facts, {**source, "concept": ""}) == [-1521000000]
    assert audit._source_values(facts, {**source, "canonical_tag": "Assets"}) == []
    ocf = {**SOURCE, "tag": "jppfs_cor:NetCashProvidedByUsedInOperatingActivities",
           "canonical_tag": "NetCashProvidedByUsedInOperatingActivities", "start": "2025-07-01"}
    assert audit._source_values(facts, ocf) == [-198197000]
    assert audit._source_values(facts, {**ocf, "concept": source["concept"]}) == []
    assert audit._source_values(facts, {**source, "start": "2025-01-01"}) == []
    assert audit._source_values(facts, {**source, "unit": "EUR"}) == []
    assert audit._source_values(facts, {**source, "source_file": "XBRL/PublicDoc/wrong.xbrl"}) == []


def test_new_cash_component_is_checked_even_without_rendered_filing_mode(tmp_path):
    retained_edinet(tmp_path)
    company = row()
    company["owner_earnings"] = {"components": [["- total capital expenditure", -1521000000]],
        "sources": {"total_capex": payment_source()}}
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 2
    company["owner_earnings"]["components"][0][1] = -999
    assert audit.audit([company], tmp_path, quiet=True)["totals"]["sourced_bad"] == 1


def test_annual_cash_bridge_full_source_is_checked_and_legacy_tuple_is_unchecked(tmp_path):
    retained_edinet(tmp_path)
    company = row()
    source = payment_source()
    point = [1521000000, *[source.get(key) for key in ("tag", "form", "accn", "end", "unit", "document")], source]
    company["owner_earnings"] = {"annual_per_share": {"2026": {
        "cash_flow_bridge": {"total_capital_expenditure": point}}}}
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 2
    point.pop()
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 1
    assert any("legacy cash point" in line[-1] for _, line in result["uncheckable"])


def test_missing_native_zip_cannot_fall_back_to_cached_canonical_values(tmp_path):
    retained_edinet(tmp_path)
    (tmp_path / "edinet" / "S100Z3O7.zip").unlink()
    company = row()
    company["total_assets"] = 999
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 0
    assert result["totals"]["unchecked"] >= 1


@pytest.mark.parametrize("kind", ["compressed", "expanded"])
def test_native_input_frontier_refuses_before_xml_parse(tmp_path, monkeypatch, kind):
    retained_edinet(tmp_path)
    path = tmp_path / "edinet" / "S100Z3O7.zip"
    if kind == "compressed":
        path.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    else:
        with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("XBRL/PublicDoc/second.xbrl", b" " * (16 * 1024 * 1024))
    monkeypatch.setattr(audit.inline_xbrl, "_xml", lambda *args: pytest.fail("size refusal must precede XML parsing"))
    result = audit.audit([row()], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 0
    assert result["totals"]["unchecked"] >= 1
    assert any("16 MiB" in item.get("reason", "") for item in result["edinet_native_sources"])


def test_public_audit_retains_unknown_filing_leaf_without_marking_it_pass(tmp_path):
    retained_edinet(tmp_path)
    company = row()
    company.update(annual_eps={"2025": 1}, annual_ratios={"2025": {"end": "2025-06-30"}})
    result = audit.audit([company], tmp_path, quiet=True, edgar=object())
    assert result["totals"]["filing_unknown"] == 1
    assert result["totals"]["filing_ok"] == 1
    assert result["totals"]["unchecked"] == 0
    assert result["uncheckable"] == [("1381.T", ("FILING?", "FY2025 eps", 1, None,
        "annual value lacks its own exact serialized source"))]
    assert not result["problems"]


def test_actual_caller_boundary_allows_missing_declared_input_not_unprotected_present(tmp_path):
    caller = Path(__file__).parents[2] / "local/japanese-retained-native-audit2-2026-10-13.py"
    tree = ast.parse(caller.read_bytes())
    node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "boundary")
    cache = tmp_path / "cache"
    cache.mkdir()
    missing = cache / "missing.json"
    other = cache / "other.json"
    other.write_text("{}")
    scope = {"Path": Path, "os": os, "OWNED": tmp_path / "output", "cache": cache,
             "protected": set(), "absent": [missing], "readonly_uri": "owned-readonly"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(caller), "exec"), scope)
    boundary = scope["boundary"]
    boundary("open", (str(missing), "r", 0))
    with pytest.raises(FileNotFoundError):
        missing.read_bytes()
    with pytest.raises(RuntimeError, match="unbracketed"):
        boundary("open", (str(other), "r", 0))
    with pytest.raises(RuntimeError, match="write outside"):
        boundary("open", (str(missing), "w", os.O_WRONLY))
    missing.write_text("{}")
    with pytest.raises(RuntimeError, match="declared-absent"):
        boundary("open", (str(missing), "r", 0))


def test_native_cache_retains_only_the_current_document(tmp_path):
    retained_edinet(tmp_path)
    path = tmp_path / "edinet" / "S100Z3O7.zip"
    (tmp_path / "edinet" / "S100Z3O8.zip").write_bytes(path.read_bytes())
    facts = audit._facts("E00009", tmp_path)
    assert audit._source_values(facts, SOURCE) == [28461000000]
    second = {**SOURCE, "document": "S100Z3O8", "accn": "S100Z3O8"}
    assert audit._source_values(facts, second) == [28461000000]
    assert set(facts["_audit_edinet"]["documents"]) == {"S100Z3O8"}


def prior_fixture(tmp_path):
    identity = {"accn": "0001193125-21-195631", "form": "20-F", "filed": "2021-06-22",
                "start": "2020-04-01", "end": "2021-03-31"}
    # Reported source values are fixture amounts, not an asserted Sony financial result.
    tags = {"NetCashProvidedByUsedInOperatingActivities": 100,
            "PaymentsToAcquirePropertyPlantAndEquipment": -40,
            "EarningsPerShareDiluted": 936.9}
    facts = {"facts": {"us-gaap": {tag: {"units": {"JPY/shares" if "PerShare" in tag else "JPY": [
        {**identity, "val": value}]}} for tag, value in tags.items()}}}
    (tmp_path / "companyfacts_0000313838.json").write_text(json.dumps(facts))
    sources = [{**identity, "tag": "us-gaap:" + tag} for tag in tags]
    cash, capex, eps = sources
    group = {"basis": "US-GAAP", "current_basis": "IFRS", "currency": "JPY",
        "share_basis": "Source-reported; later split and receipt adjustments are not assessed.",
        "series": {"eps": {"2021": {"value": 936.9, "source": eps}},
                   "operating_cash_flow": {"2021": {"value": 100, "source": cash}},
                   "capital_expenditure": {"2021": {"value": -40, "source": capex}},
                   "free_cash_flow": {"2021": {"value": 60, "source": {
                       **identity, "tag": cash["tag"] + " - " + capex["tag"],
                       "components": [cash, capex]}}}}}
    return {"cik": "0000313838", "ticker": "SONY", "criteria": [],
            "annual_eps": {"2025": 1}, "prior_accounting_history": [group]}


def test_public_prior_history_checks_reported_sources_and_source_parent_fcf(tmp_path):
    company = prior_fixture(tmp_path)
    untouched_current = copy.deepcopy(company["annual_eps"])
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["sourced_ok"] == 3
    assert result["totals"]["derived_ok"] == 1
    assert not result["problems"]
    assert company["annual_eps"] == untouched_current
    company["prior_accounting_history"][0]["series"]["eps"]["2021"]["value"] = 823.77
    assert audit.audit([company], tmp_path, quiet=True)["totals"]["sourced_bad"] == 1


@pytest.mark.parametrize("key,value", [("accn", "wrong"), ("filed", "2022-06-28"),
                                     ("form", "10-K"), ("start", "2020-01-01"), ("unit", "USD/shares")])
def test_prior_source_provenance_change_does_not_pass(tmp_path, key, value):
    company = prior_fixture(tmp_path)
    source = company["prior_accounting_history"][0]["series"]["eps"]["2021"]["source"]
    source[key] = value
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["unchecked"] >= 1
    assert result["totals"]["sourced_ok"] == 2


def test_prior_fcf_wrong_math_and_period_are_separate_failures(tmp_path):
    company = prior_fixture(tmp_path)
    series = company["prior_accounting_history"][0]["series"]
    series["free_cash_flow"]["2021"]["value"] = 140
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["derived_bad"] == 1
    series["capital_expenditure"]["2021"]["source"]["start"] = "2020-01-01"
    result = audit.audit([company], tmp_path, quiet=True)
    assert result["totals"]["derived_ok"] == 0
    assert result["totals"]["unchecked"] >= 2
