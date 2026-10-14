"""Parent/common equity through retained source mapping and actual consumers."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
import io
import zipfile

import pytest

from screener import normalize, sync, profiles
from screener.models import Fact, Quote, Status
from screener.screens.enterprising import _c7_tangible_asset_valuation
from screener.sources.edinet_mapper import build_edinet_companyfacts
from screener.sources.edinet import EdinetError, issuer_class_evidence


def _archive(*, oci="5", class_label="普通株式", class_value="100", duplicate="",
             oci_context="current", unit="JPY", earnings=False):
    header = "当連結会計年度末株式数（株）"
    note = (f"<table><tr><td>種類</td><td>{header}</td></tr>"
            f"<tr><td>発行済株式</td><td></td></tr>"
            f"<tr><td>普通株式</td><td>100</td></tr>"
            + (f"<tr><td>{class_label}</td><td>{class_value}</td></tr>" if class_label != "普通株式" else "")
            + f"<tr><td>合計</td><td>{100 + int(class_value) if class_label != '普通株式' and class_value.isdigit() else 100}</td></tr></table>")
    from xml.sax.saxutils import escape
    facts = '<j:Assets contextRef="current" unitRef="JPY">200</j:Assets><j:Liabilities contextRef="current" unitRef="JPY">20</j:Liabilities>'
    facts += '<j:ShareholdersEquity contextRef="current" unitRef="JPY">100</j:ShareholdersEquity>'
    if oci is not None:
        facts += f'<j:ValuationAndTranslationAdjustments contextRef="{oci_context}" unitRef="{unit}">{oci}</j:ValuationAndTranslationAdjustments>'
    facts += duplicate
    if earnings:
        facts += ('<j:NetSales contextRef="duration" unitRef="JPY">1000000</j:NetSales>'
                  '<j:ProfitLossAttributableToOwnersOfParent contextRef="duration" unitRef="JPY">10</j:ProfitLossAttributableToOwnersOfParent>')
    xml = f'''<x:xbrl xmlns:x="http://www.xbrl.org/2003/instance"
      xmlns:j="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor"
      xmlns:c="http://disclosure.edinet-fsa.go.jp/taxonomy/jpcrp/2025-11-01/jpcrp_cor">
      <x:context id="current"><x:entity><x:identifier scheme="http://disclosure.edinet-fsa.go.jp">E03475-000</x:identifier></x:entity><x:period><x:instant>2026-06-30</x:instant></x:period></x:context>
      <x:context id="prior"><x:entity><x:identifier scheme="http://disclosure.edinet-fsa.go.jp">E03475-000</x:identifier></x:entity><x:period><x:instant>2025-06-30</x:instant></x:period></x:context>
      <x:context id="duration"><x:entity><x:identifier scheme="http://disclosure.edinet-fsa.go.jp">E03475-000</x:identifier></x:entity><x:period><x:startDate>2025-07-01</x:startDate><x:endDate>2026-06-30</x:endDate></x:period></x:context>
      <x:unit id="JPY"><x:measure>JPY</x:measure></x:unit><x:unit id="USD"><x:measure>USD</x:measure></x:unit>
      {facts}<c:NotesRegardingIssuedSharesAndTreasurySharesTextBlock contextRef="duration">{escape(note)}</c:NotesRegardingIssuedSharesAndTreasurySharesTextBlock></x:xbrl>'''
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as bundle:
        bundle.writestr("XBRL/PublicDoc/annual.xbrl", xml)
    return out.getvalue()


def _bundle(archive, *, entity="E03475", document="S100TEST", security="30280"):
    return build_edinet_companyfacts({"docID": document, "edinetCode": entity,
        "secCode": security, "docTypeCode": "120", "xbrlFlag": "1",
        "submitDateTime": "2026-09-24 09:00", "periodStart": "2025-07-01",
        "periodEnd": "2026-06-30"}, archive, ticker=f"{security[:-1]}.T")


def test_parent_pair_and_shared_current_consumers_preserve_reported_liabilities():
    snap = normalize.build_snapshot("3028.T", "E03475", _bundle(_archive()))
    assert snap.parent_equity.value == snap.common_equity.value == Decimal(105)
    assert snap.total_liabilities.value == Decimal(20)
    assert snap.preferred_stock is None
    assert len(snap.common_equity.operation_inputs) == 2
    assert [fact.value for fact in snap.common_equity.operation_inputs] == [Decimal(100), Decimal(5)]
    assert sync._common_equity(snap) == Decimal(105)
    source = sync._source(snap.common_equity)
    assert "canonical_tag" not in source
    assert source["equity_class_evidence"][0]["as_of"] == "2026-06-30"
    assert source["components"][1]["native_xml_location"]["context_ref"] == "current"
    # Explicit consumer inputs; no source extractor infers these from income/EPS.
    p = snap.total_assets.provenance
    snap = replace(snap, shares_outstanding=Fact(Decimal(10), p),
                   goodwill=Fact(Decimal(0), p), intangibles=Fact(Decimal(0), p))
    quote = Quote(Decimal(1), datetime(2026, 9, 24, tzinfo=timezone.utc), "test")
    result = _c7_tangible_asset_valuation(snap, quote)
    assert result.status == Status.PASS
    assert result.inputs[0] is snap.common_equity
    assert sync._bvps(snap) == sync._tbvps(snap) == 10.5
    assert sync._ncavps(snap) is None  # Current assets were never supplied.


@pytest.mark.parametrize("options", [
    {"oci": None}, {"oci_context": "prior"}, {"unit": "USD"},
    {"duplicate": '<j:ShareholdersEquity contextRef="current" unitRef="JPY">99</j:ShareholdersEquity>'},
])
def test_incomplete_or_incompatible_pair_cannot_fall_back_to_assets_less_liabilities(options):
    snap = normalize.build_snapshot("3028.T", "E03475", _bundle(_archive(**options)))
    assert snap.common_equity is None
    assert sync._common_equity(snap) is None
    assert snap.total_liabilities.value == Decimal(20)


@pytest.mark.parametrize("oci", ["0", "-5"])
def test_explicit_zero_and_negative_oci_are_reported_components(oci):
    snap = normalize.build_snapshot("3028.T", "E03475", _bundle(_archive(oci=oci)))
    assert snap.common_equity.value == Decimal(100) + Decimal(oci)


@pytest.mark.parametrize("oci,book,on_book", [
    ("-100", 0, None), ("-105", -5, None), ("-90", 10, 100.0),
])
def test_measured_common_book_amount_preserves_zero_negative_and_positive(oci, book, on_book):
    snap = normalize.build_snapshot("3028.T", "E03475", _bundle(_archive(oci=oci, earnings=True)))
    assert snap.common_equity.value == Decimal(book)
    assert snap.annual_net_income[2026].value == Decimal(10)
    result = sync._profitability(snap)
    assert result["book_value"] == book
    assert result["on_book"] == on_book
    assert result["on_equity"] is None  # No qualified opening common amount.
    legacy = replace(snap, common_equity_required=False, common_equity=None)
    old_result = sync._profitability(legacy)
    assert old_result["book_value"] == 180.0
    assert old_result["on_book"] == 5.56


def test_withheld_issuer_class_blocks_acquisition_book_fallback():
    bundle = _bundle(_archive(class_label="Ａ種優先株式", class_value="2"))
    snap = normalize.build_snapshot("3028.T", "E03475", bundle)
    assert snap.parent_equity.value == Decimal(105)
    assert snap.common_equity is None and snap.common_equity_required
    taxonomy = normalize._with_fiscal_calendar(bundle["facts"]["canonical"])
    taxonomy.reporting_currency = "JPY"
    taxonomy.currency_adapter = taxonomy.canonical_adapter = True
    quality, _ = sync._asset_quality(snap, taxonomy)
    row = {"total_assets": 200, "total_liabilities": 20, "asset_quality": quality,
           "goodwill": 100, "intangibles": 100}
    # Explicit positive consumer inputs make the old A−L fallback emit its claim.
    assert profiles.acquisition_book_note(row) is None
    assert "common_equity" in quality and quality["common_equity"] is None


@pytest.mark.parametrize("label,value", [("Ａ種優先株式", "2"), ("Ａ種優先株式", "－"), ("未分類株式", "0")])
def test_unknown_or_positive_nonordinary_class_withholds_common_without_fake_zero(label, value):
    snap = normalize.build_snapshot("3028.T", "E03475", _bundle(_archive(class_label=label, class_value=value)))
    assert snap.parent_equity.value == Decimal(105)
    assert snap.common_equity is None
    assert snap.preferred_stock is None


@pytest.mark.parametrize("mutation", ["duplicate-context", "duplicate-member", "doctype"])
def test_ambiguous_original_source_is_refused_before_class_qualification(mutation):
    source = _archive()
    with zipfile.ZipFile(io.BytesIO(source)) as original:
        xml = original.read("XBRL/PublicDoc/annual.xbrl")
    if mutation == "duplicate-context":
        import re
        context = re.search(br'<x:context id="current">.*?</x:context>', xml).group()
        xml = xml.replace(b"</x:xbrl>", context + b"</x:xbrl>")
    elif mutation == "doctype":
        xml = b'<!DOCTYPE xbrl [<!ENTITY injected "ordinary">]>' + xml
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("XBRL/PublicDoc/annual.xbrl", xml)
        if mutation == "duplicate-member":
            with pytest.warns(UserWarning, match="Duplicate name"):
                archive.writestr("XBRL/PublicDoc/annual.xbrl", xml)
    with pytest.raises(EdinetError):
        issuer_class_evidence(out.getvalue(), "S100TEST", "E03475")


def test_original_source_ceiling_is_checked_before_expansion(monkeypatch):
    from screener.sources import edinet
    source = _archive()
    monkeypatch.setattr(edinet, "_XBRL_INPUT_LIMIT", len(source) - 1)
    with pytest.raises(EdinetError, match="source ceiling"):
        issuer_class_evidence(source, "S100TEST", "E03475")


def test_other_issuer_class_table_does_not_qualify_common():
    assert issuer_class_evidence(_archive(), "S100TEST", "E00001") == []


@pytest.mark.parametrize("document,entity,security,parent", [
    ("S100Z2YO", "E03475", "30280", "126359000000"),
    ("S100Z32V", "E01991", "69200", "246828000000"),
    ("S100Z3CP", "E30880", "33000", "9742482000"),
    ("S100Z3CG", "E32988", "65470", "12668000000"),
])
def test_retained_original_parent_and_class_controls(document, entity, security, parent):
    path = Path.home() / ".cache/graham-screener/edinet" / f"{document}.zip"
    if not path.is_file():
        pytest.skip("retained original EDINET control unavailable")
    bundle = _bundle(path.read_bytes(), entity=entity, document=document, security=security)
    snap = normalize.build_snapshot(f"{security[:-1]}.T", entity, bundle)
    assert snap.parent_equity.value == snap.common_equity.value == Decimal(parent)
    assert snap.preferred_stock is None
    taxonomy = normalize._with_fiscal_calendar(bundle["facts"]["canonical"])
    taxonomy.canonical_adapter = taxonomy.currency_adapter = taxonomy.jgaap_equity_required = True
    taxonomy.reporting_currency = "JPY"
    prior_parent, prior_common, reason = normalize._jgaap_equity(taxonomy, date(2025, 6, 30))
    assert prior_parent is not None
    if document == "S100Z3CG":
        assert prior_common is None
        assert "preferred monetary claim" in reason
        facts = {}
        ratios = normalize.annual_ratios(taxonomy, snap.annual_net_income, snap.annual_revenue,
                                        snap.annual_operating_income, common_equity_facts=facts)
        assert 2025 not in facts and 2026 in facts
        assert "return_on_equity" not in ratios.get(2026, {})
    else:
        assert prior_common is not None and reason is None
