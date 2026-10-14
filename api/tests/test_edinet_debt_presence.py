"""Known disclosed borrowing cannot become a false zero-debt assumption."""
from copy import deepcopy
from datetime import date
from decimal import Decimal
import json
import hashlib
import io
import re
import zipfile
from pathlib import Path

import pytest

from screener import evidence, normalize, sync
from screener.sources import edinet_mapper
from test_jgaap_parent_equity import _archive, _bundle


_INPUTS = Path(__file__).resolve().parents[2] / "local/edinet-raw-export-observation-38442625807-2026-10-13/files"


def actual_alpen():
    if not (_INPUTS / "edinet/S100WQEC.zip").is_file():
        pytest.skip("approved Alpen originals unavailable")
    reference = (_INPUTS / "companyfacts_E03475.json").read_bytes()
    assert hashlib.sha256(reference).hexdigest() == "adaf0a301ac001c0d09c7fcde21704f651750d63cfa6102c86955995c3eca4ac"
    cached = json.loads(reference)
    expected = {"S100Z2YO": "269373de7d467a2babdef75279b9e50c11d1bf55332d00de30acae8a089fc221",
                "S100WQEC": "845a625d950b8d0798299ed07a73f2bbf0ac2e5b4ef3baa4f1761ab6dbdb927a"}
    def load(document):
        raw = (_INPUTS / "edinet" / f"{document}.zip").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected[document]
        return raw
    return edinet_mapper.rebuild_companyfacts(cached, load)


def test_actual_alpen_disclosed_loans_do_not_become_current_or_historical_zero():
    facts = actual_alpen()
    result = sync._derive_evidence_result(evidence.EvidenceBundle("E03475", "3028.T", facts, None, None))
    strict = result.row
    overlay = strict["return_quality_assumption"]
    assert strict["debt"] is None and strict["short_term_debt"] is None
    assert overlay["debt_to_equity"] is None
    assert "debt" not in overlay["applied"] and "short_term_debt" not in overlay["applied"]
    assert "short_term_debt" not in normalize._historical_absent_zero_candidates(
        facts["facts"], date(2026, 6, 30))
    assert "short_term_debt" not in normalize._historical_absent_zero_candidates(
        facts["facts"], date(2025, 6, 30))
    assert ((result.rq_result.row.get("operating_returns") or {}).get("nopat_roic")) is None


@pytest.mark.parametrize("number", ["1000000", "1000000.000", "1E999999999", Decimal("1000000"), 1000000])
def test_exact_finite_decimal_debt_evidence_is_material(number):
    entry = dict(val=number, accn="source", form="JP-AR", filed="2026-09-24", end="2026-06-30")
    facts = {"jppfs_cor": {"ShortTermLoansPayable": {"units": {"JPY": [entry]}}}}
    assert "debt" not in normalize._absent_zero_candidates(facts)
    assert "short_term_debt" not in normalize._historical_absent_zero_candidates(facts, date(2026, 6, 30))


@pytest.mark.parametrize("number", [None, True, False, "garbage", "NaN", "Infinity", float("nan")])
def test_unreadable_disclosed_amount_never_establishes_absence(number):
    entry = dict(val=number, form="JP-AR", filed="2026-09-24", end="2026-06-30")
    facts = {"jppfs_cor": {"ShortTermLoansPayable": {"units": {"JPY": [entry]}}}}
    assert "debt" not in normalize._absent_zero_candidates(facts)
    assert "short_term_debt" not in normalize._historical_absent_zero_candidates(facts, date(2026, 6, 30))


def test_explicit_finite_zero_keeps_existing_absence_policy():
    entry = dict(val="0", form="JP-AR", filed="2026-09-24", end="2026-06-30")
    facts = {"jppfs_cor": {"ShortTermLoansPayable": {"units": {"JPY": [entry]}}}}
    assert "debt" in normalize._absent_zero_candidates(facts)
    assert "short_term_debt" in normalize._historical_absent_zero_candidates(facts, date(2026, 6, 30))


def test_current_loan_vocabulary_does_not_turn_payments_or_assets_into_debt():
    assert normalize.debt_evidence_roles("CurrentPortionOfLongTermLoansPayable") == {"debt", "short_term_debt"}
    assert normalize.debt_evidence_roles("LongTermLoansPayable") == {"debt"}
    assert not normalize.debt_evidence_roles("PaymentsOfShortTermLoansPayable")
    assert not normalize.debt_evidence_roles("ShortTermDebtSecuritiesInvestment")


def test_presence_survives_retained_two_report_merge_and_reimport():
    facts = actual_alpen()
    source = facts["facts"]["jppfs_cor"]["ShortTermLoansPayable"]["units"]["JPY"]
    current = [item for item in source if item["accn"] == "S100Z2YO" and item["end"] == "2026-06-30"]
    assert [item["val"] for item in current] == ["4500000000"]
    assert current[0]["_source_entity"] == "E03475-000"
    assert current[0]["_source_native_xml"]["context_ref"] == "CurrentYearInstant"
    assert current[0]["_source_tag"] == "ShortTermLoansPayable"
    incoming = deepcopy(facts)
    incoming["_adapter"]["reports"] = [report for report in incoming["_adapter"]["reports"] if report["document"] == "S100Z2YO"]
    for taxonomy in incoming["facts"].values():
        for data in taxonomy.values():
            for unit, entries in data["units"].items():
                data["units"][unit] = [item for item in entries if item["accn"] == "S100Z2YO"]
    merged = edinet_mapper.merge_companyfacts(deepcopy(facts), incoming)
    assert merged["facts"]["jppfs_cor"]["ShortTermLoansPayable"]["units"]["JPY"] == source


def debt_archive(*, amount="1000000", fault=None):
    with zipfile.ZipFile(io.BytesIO(_archive())) as source:
        xml = source.read("XBRL/PublicDoc/annual.xbrl").decode()
    xml = xml.replace('xmlns:j=', 'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:j=')
    xml = xml.replace('<x:measure>JPY</x:measure>', '<x:measure>iso4217:JPY</x:measure>')
    xml = xml.replace('<x:measure>USD</x:measure>', '<x:measure>iso4217:USD</x:measure>')
    context = re.search(r'<x:context id="current">.*?</x:context>', xml, re.S).group()
    context = context.replace('id="current"', 'id="debt"')
    if fault == "entity":
        context = context.replace("E03475-000", "E99999-000")
    elif fault == "dimension":
        context = context.replace('</x:context>', '<x:scenario><d:explicitMember xmlns:d="http://xbrl.org/2006/xbrldi" dimension="j:RegionAxis">j:OtherMember</d:explicitMember></x:scenario></x:context>')
    elif fault == "period":
        context = context.replace("2026-06-30", "2027-06-30")
    elif fault == "invalid-date":
        context = context.replace("2026-06-30", "2026-06-31")
    if fault == "non-iso-measure":
        xml = xml.replace('http://www.xbrl.org/2003/iso4217', 'https://example.invalid/currency')
    unit = "USD" if fault == "currency" else "JPY"
    nil = ' xmlns:i="http://www.w3.org/2001/XMLSchema-instance" i:nil="true"' if amount is None else ""
    loan = f'<j:ShortTermLoansPayable contextRef="debt" unitRef="{unit}"{nil}>{amount or ""}</j:ShortTermLoansPayable>'
    xml = xml.replace('</x:xbrl>', context + loan + '</x:xbrl>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        target.writestr("XBRL/PublicDoc/annual.xbrl", xml)
    return out.getvalue()


@pytest.mark.parametrize("fault", ["entity", "currency", "dimension", "period", "non-iso-measure"])
def test_native_presence_excludes_wrong_statement_scope(fault):
    facts = _bundle(debt_archive(fault=fault))
    assert "jppfs_cor" not in facts["facts"]
    assert "ShortTermLoansPayable" not in facts["facts"]["canonical"]


def test_invalid_native_instant_date_is_a_visible_source_failure():
    with pytest.raises(ValueError):
        _bundle(debt_archive(fault="invalid-date"))


@pytest.mark.parametrize("amount", [None, "NaN", "not-a-number"])
def test_native_unreadable_debt_occurrence_keeps_exact_source_and_blocks_zero(amount):
    facts = _bundle(debt_archive(amount=amount))
    entry = facts["facts"]["jppfs_cor"]["ShortTermLoansPayable"]["units"]["JPY"][0]
    assert entry["val"] is None
    assert entry["_source_lexical"] == (amount or "")
    assert entry["_source_native_xml"]["context_ref"] == "debt"
    assert "debt" not in normalize._absent_zero_candidates(facts["facts"])
    assert "short_term_debt" not in normalize._historical_absent_zero_candidates(facts["facts"], date(2026, 6, 30))
    assert "ShortTermBorrowings" not in facts["facts"]["canonical"]


def test_old_filing_does_not_block_a_genuinely_silent_current_window():
    old = dict(val="1000000", form="JP-AR", filed="2025-09-24", end="2025-06-30")
    current = dict(val="2000000", form="JP-AR", filed="2026-09-24", end="2026-06-30")
    facts = {"jppfs_cor": {"ShortTermLoansPayable": {"units": {"JPY": [old]}},
                            "Assets": {"units": {"JPY": [current]}}}}
    assert "debt" in normalize._absent_zero_candidates(facts)
    assert "short_term_debt" in normalize._historical_absent_zero_candidates(facts, date(2026, 6, 30))
