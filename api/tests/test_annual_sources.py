"""Per-year source identity is independent of latest source and display balances."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

from screener import audit, evidence, sync
from screener.models import Fact, Provenance
from screener.sources import edinet_mapper
from tests.helpers import GAAP, facts_doc
from tests.test_edinet_mapper import _s100z3o7_cashflow_rows
from tests.test_audit import physical_views
from tests.test_audit_edinet_prior_history import retained_edinet, SOURCE


def test_selected_eps_years_emit_their_actual_fact_source_without_changing_values():
    result = sync._derive_evidence_result(evidence.EvidenceBundle(
        "0000000001", "TEST", facts_doc(GAAP), None, None))
    assert result.status == "ok"
    row, snapshot = result.row, result.snapshot
    for year, fact in snapshot.annual_eps.items():
        assert row["annual_eps"][str(year)] == float(fact.value)
        source = row["annual_sources"]["annual_eps"][str(year)]
        assert source["accn"] == fact.provenance.accession
        assert source["tag"] == fact.provenance.tag
        assert source["start"] == fact.provenance.period_start.isoformat()
        assert source["end"] == fact.provenance.period_end.isoformat()
        assert source["filed"] == fact.provenance.filed.isoformat()
        assert source.get("unit") == fact.provenance.unit
    assert row["annual_sources"]["annual_eps"]["2021"]["accn"] == "k21"
    assert row["annual_sources"]["annual_eps"]["2023"]["accn"] == "k24"
    assert row["annual_sources"]["annual_eps"]["2021"] != row["sources"]["eps"]


def test_retained_jgaap_income_duration_uses_each_actual_year_not_latest_balance(monkeypatch):
    record, raw = _s100z3o7_cashflow_rows()
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, document: raw)
    facts = edinet_mapper.build_edinet_companyfacts(record, b"source-backed fixture", ticker="1381.T")
    result = sync._derive_evidence_result(evidence.EvidenceBundle("E00009", "1381.T", facts, None, None))
    assert result.status == "ok"
    row = result.row
    assert row["annual_net_income"] == {"2025": 1720000000.0, "2026": 2925000000.0}
    sources = row["annual_sources"]["annual_net_income"]
    assert sources["2025"] == {
        "tag": "jppfs_cor:ProfitLossAttributableToOwnersOfParent", "form": "JP-AR", "accn": "S100Z3O7",
        "filed": "2026-09-24", "start": "2024-07-01", "end": "2025-06-30", "unit": "JPY",
        "document": "S100Z3O7", "canonical_tag": "NetIncomeLoss", "concept": "NetIncomeLoss",
    }
    assert sources["2026"]["start"] == "2025-07-01" and sources["2026"]["end"] == "2026-06-30"
    assert sources["2025"]["end"] != row["balance_sheet_date"]
    assert "annual_eps" not in row["annual_sources"]


def test_unproved_operation_and_components_stay_visible_only_for_emitted_years():
    base = sync._derive_evidence_result(evidence.EvidenceBundle(
        "0000000001", "TEST", facts_doc(GAAP), None, None)).snapshot
    provenance = Provenance("Reported shares", "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
        2025, "10-K", "annual25", date(2026, 2, 15), period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31), unit="shares")
    original = Fact(Decimal("20"), provenance)
    adjusted = Fact(Decimal("10"), replace(provenance,
        concept="Reported shares (per depositary receipt: x2 ordinary shares, cover of cover25)"),
        operation_inputs=(original,), exact_unassessed_reason="RECEIPT_BASIS_OPERATION_UNTRACED")
    components = (replace(provenance, concept="Income", tag="us-gaap:NetIncomeLoss", unit="USD"),
                  replace(provenance, concept="Preferred dividends", tag="us-gaap:PreferredStockDividends", unit="USD"))
    derived = Fact(Decimal("2"), replace(provenance, tag="derived EPS", components=components),
                   exact_unassessed_reason="DERIVED_EPS_INPUTS_NOT_ASSESSED")
    snapshot = replace(base, annual_share_counts={2025: adjusted}, annual_eps={2025: derived})
    row = {"annual_eps": {"2025": 2.0}, "annual_weighted_shares": {"2025": 10.0}}
    unchanged = {field: dict(values) for field, values in row.items()}
    result = sync._annual_sources_row(snapshot, row)
    assert row == unchanged
    assert result["annual_weighted_shares"]["2025"]["operation_unassessed_reason"] == "RECEIPT_BASIS_OPERATION_UNTRACED"
    assert "per depositary receipt" in result["annual_weighted_shares"]["2025"]["concept"]
    assert result["annual_eps"]["2025"]["operation_unassessed_reason"] == "DERIVED_EPS_INPUTS_NOT_ASSESSED"
    assert [item["tag"] for item in result["annual_eps"]["2025"]["components"]] == [p.tag for p in components]
    assert "annual_net_income" not in result
    assert sync._annual_sources_row(snapshot, {}) == {}
    without_unit = replace(derived, provenance=replace(derived.provenance, unit=None))
    absent_unit = sync._annual_sources_row(replace(snapshot, annual_eps={2025: without_unit}), row)
    assert "unit" not in absent_unit["annual_eps"]["2025"]


def test_income_audit_opens_each_selected_year_even_when_latest_has_no_older_column(monkeypatch):
    def selected(year):
        return {"tag": "us-gaap:GrossProfit", "form": "10-K", "accn": f"annual-{year}",
                "filed": f"{year + 1}-02-15", "start": f"{year}-01-01", "end": f"{year}-12-31", "unit": "USD"}
    row = {"cik": "1", "ticker": "TEST", "annual_gross_profit": {"2024": 80, "2025": 100},
           "annual_sources": {"annual_gross_profit": {"2024": selected(2024), "2025": selected(2025)}},
           "sources": {"eps": selected(2025), "gross_profit": selected(2025)},
           "annual_ratios": {"2024": {"end": "2025-12-31"}, "2025": {"end": "2025-12-31"}}}
    def read(_row, _edgar, _kind, source):
        year = int(source["end"][:4])
        value = Decimal("80") if year == 2024 else Decimal("100")
        return physical_views([], {"us-gaap_GrossProfit": [value]}, [date(year, 12, 31)])
    monkeypatch.setattr(audit, "_read_statement", read)
    assert audit.against_income(row, object()) == [
        ("FILING-OK", "FY2024 gross_profit", 80, 80, None),
        ("FILING-OK", "FY2025 gross_profit", 100, 100, None)]


def test_exact_annual_sources_do_not_fall_back_or_approve_unproved_operations(monkeypatch):
    source = {"tag": "us-gaap:EarningsPerShareDiluted", "form": "10-K", "accn": "annual",
              "start": "2025-01-01", "end": "2025-12-31", "unit": "USD/shares",
              "operation_unassessed_reason": "SPLIT_OPERATION_UNTRACED"}
    row = {"cik": "1", "ticker": "TEST", "annual_eps": {"2024": 1, "2025": 2},
           "annual_sources": {"annual_eps": {"2025": source}}, "sources": {"eps": source}}
    monkeypatch.setattr(audit, "_read_statement", lambda *_: (_ for _ in ()).throw(AssertionError("no proved source to read")))
    assert audit.against_income(row, object()) == [
        ("FILING?", "FY2024 eps", 1, None, "no exact selected-year source context in the serialized series"),
        ("FILING?", "FY2025 eps", 2, None, "SPLIT_OPERATION_UNTRACED")]


def test_failed_selected_year_read_remains_visible(monkeypatch):
    source = {"tag": "us-gaap:GrossProfit", "form": "10-K", "accn": "annual-2024",
              "start": "2024-01-01", "end": "2024-12-31", "unit": "USD"}
    row = {"cik": "1", "ticker": "TEST", "annual_gross_profit": {"2024": 80},
           "annual_sources": {"annual_gross_profit": {"2024": source}}}
    def read(*_):
        raise OSError("retained statement absent")
    monkeypatch.setattr(audit, "_read_statement", read)
    result = audit.against_income(row, object())
    assert len(result) == 1 and result[0][:4] == ("FILING?", "FY2024 gross_profit", 80, None)
    assert "retained statement absent" in result[0][4]


def test_native_annual_income_matches_exact_duration_not_balance_or_latest_source(tmp_path):
    retained_edinet(tmp_path, duplicate='''
      <xbrli:context id="prior"><xbrli:entity><xbrli:identifier scheme="http://disclosure.edinet-fsa.go.jp">E00009-000</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2024-07-01</xbrli:startDate><xbrli:endDate>2025-06-30</xbrli:endDate></xbrli:period></xbrli:context>
      <jppfs_cor:ProfitLossAttributableToOwnersOfParent contextRef="prior" unitRef="JPY">1720000000</jppfs_cor:ProfitLossAttributableToOwnersOfParent>
      <jppfs_cor:ProfitLossAttributableToOwnersOfParent contextRef="annual" unitRef="JPY">2925000000</jppfs_cor:ProfitLossAttributableToOwnersOfParent>''')
    source = {**SOURCE, "tag": "jppfs_cor:ProfitLossAttributableToOwnersOfParent", "canonical_tag": "NetIncomeLoss"}
    row = {"cik": "E00009", "ticker": "1381.T", "criteria": [],
           "annual_net_income": {"2025": 1720000000, "2026": 2925000000},
           "sources": {"net_income": source}, "annual_ratios": {},
           "annual_sources": {"annual_net_income": {
               "2025": {**source, "start": "2024-07-01", "end": "2025-06-30"},
               "2026": {**source, "start": "2025-07-01"}}}}
    facts = audit._facts("E00009", tmp_path)
    assert [item[:4] for item in audit._edinet_filing_checks(row, facts)] == [
        ("FILING-OK", "FY2025 net_income", 1720000000, 1720000000),
        ("FILING-OK", "FY2026 net_income", 2925000000, 2925000000)]
    row["annual_net_income"]["2025"] = 2925000000
    assert list(audit._edinet_filing_checks(row, facts))[0][:4] == (
        "FILING", "FY2025 net_income", 2925000000, 1720000000)
    row["annual_sources"]["annual_net_income"]["2025"]["operation_unassessed_reason"] = "UNTRACED"
    assert list(audit._edinet_filing_checks(row, facts))[0][0] == "FILING?"
