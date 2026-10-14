"""Missing payout units and unary transforms retain honest source evidence."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from screener import normalize, sync
from screener.models import Fact, Provenance
from tests.helpers import OE_GAAP, build, dur, inst, tagdata


def foreign_taxonomy(tag, unit, entries=None):
    tax = normalize._FiscalTaxonomy({tag: tagdata(unit, entries or [
        dur("2025-01-01", "2025-12-31", 100, form="20-F")])})
    tax.reporting_currency = "CNY"
    tax.currency_adapter = True
    return tax


@pytest.mark.parametrize("tag,units", normalize.DIVIDEND_TAGS)
def test_supported_positive_payout_without_selected_unit_is_unknown(tag, units):
    tax = foreign_taxonomy(tag, units[0])
    assert normalize._dividend(tax, date(2025, 12, 31)) == (None, None)


@pytest.mark.parametrize("unit", ["shares", "pure"])
def test_missing_unit_does_not_turn_counts_or_ratios_into_payouts(unit):
    tax = foreign_taxonomy("Dividends", unit)
    assert normalize._dividend(tax, date(2025, 12, 31)) == (False, None)


@pytest.mark.parametrize("tag", ["DividendsReceived", "DividendsPreferredStock",
    "DividendsPaidToNoncontrollingInterest", "ExpectedDividendYield"])
def test_missing_unit_preserves_nonpayout_exclusions(tag):
    assert normalize._dividend(foreign_taxonomy(tag, "USD"), date(2025, 12, 31)) == (False, None)


@pytest.mark.parametrize("start,end,filed,reference", [
    ("2025-01-01", "2025-03-31", "2025-05-01", "2025-12-31"),
    ("2024-01-01", "2024-12-31", "2025-03-01", "2026-04-01"),
    ("2025-01-01", "2025-12-31", "2025-09-01", "2025-12-31"),
])
def test_unresolved_supported_payout_preserves_cadence_and_filed_period(start, end, filed, reference):
    tax = foreign_taxonomy("Dividends", "USD", [dur(start, end, 100, filed=filed)])
    assert normalize._dividend(tax, date.fromisoformat(reference)) == (False, None)


def test_unresolved_annual_payout_uses_the_same_cadence_as_selected_evidence():
    tax = foreign_taxonomy("Dividends", "USD")
    # Annual duration plus the existing60-day lag extends beyond the broad
    #400-day window used only for unchained names.
    assert normalize._dividend(tax, date(2027, 2, 1)) == (None, None)
    assert normalize._dividend(tax, date(2027, 3, 1)) == (False, None)


def test_unresolved_aggregate_excludes_only_proven_same_currency_preferred_payout():
    tax = foreign_taxonomy("Dividends", "USD")
    tax["DividendsPreferredStock"] = tagdata("USD", [dur("2025-01-01", "2025-12-31", 100)])
    assert normalize._dividend(tax, date(2025, 12, 31)) == (False, None)
    tax["DividendsPreferredStock"] = tagdata("CNY", [dur("2025-01-01", "2025-12-31", 100)])
    assert normalize._dividend(tax, date(2025, 12, 31)) == (None, None)


def test_unresolved_aggregate_preserves_prior_payable_settlement_exclusion():
    tax = foreign_taxonomy("PaymentsOfDividends", "USD", [
        dur("2025-01-01", "2025-12-31", 37000)])
    tax["DividendsPayableCurrent"] = tagdata("USD", [
        inst("2024-12-31", 37000), inst("2025-06-30", 36375), inst("2025-12-31", 0)])
    assert normalize._dividend(tax, date(2025, 12, 31)) == (False, None)
    tax["DividendsPayableCurrent"] = tagdata("CNY", [
        inst("2024-12-31", 37000), inst("2025-06-30", 36375), inst("2025-12-31", 0)])
    assert normalize._dividend(tax, date(2025, 12, 31)) == (None, None)


def test_unknown_dividend_remains_insufficient_in_strict_and_assumed_grades():
    from screener.screens.enterprising import evaluate
    from tests.helpers import GAAP, facts_doc
    tax = foreign_taxonomy("PaymentsOfDividendsCommonStock", "CAD")
    gaap = dict(GAAP)
    gaap["PaymentsOfDividendsCommonStock"] = tax["PaymentsOfDividendsCommonStock"]
    for assumed in (False, True):
        snap = normalize.build_snapshot("TEST", "0000000001", facts_doc(gaap),
            assume_absent_zero=assumed)
        criterion = next(c for c in evaluate(snap, None).criteria if c.criterion == 5)
        assert snap.pays_dividend is None
        assert criterion.status.value == "INSUFFICIENT_DATA"


def rich_provenance():
    return Provenance("EPS", "native:EPS", 2025, "6-K", "source", date(2026, 2, 1),
        date(2025, 12, 31), date(2025, 1, 1), segments="Class=Ordinary",
        unit="AUD/shares", document="statement.htm", canonical_tag="EarningsPerShareDiluted",
        annual_accession="annual", annual_form="20-F", annual_filed=date(2026, 3, 1))


def test_receipt_transform_retains_exact_source_metadata_and_components():
    p = replace(rich_provenance(), components=(rich_provenance(),))
    fact = Fact(Decimal("2"), p)
    parts = {"annual_eps": {2025: fact}, "ttm_eps_inputs": (fact,),
             "shares": fact, "recurring_dividend_per_share": fact}
    normalize._restate_onto_receipt(parts, Decimal("40"), "cover-accession")
    for output, value in ((parts["annual_eps"][2025], Decimal("80")),
        (parts["ttm_eps_inputs"][0], Decimal("80")), (parts["shares"], Decimal(".05")),
        (parts["recurring_dividend_per_share"], Decimal("80"))):
        assert output.value == value
        assert output.provenance == replace(p, concept=output.provenance.concept)


def test_owner_capital_sources_share_complete_provenance_serializer():
    snap = build(OE_GAAP)
    p = rich_provenance()
    leaf = Fact(Decimal("100"), p)
    capital = Fact(Decimal("100"), replace(p, components=(p,)))
    oe = replace(snap.owner_earnings, invested_capital_beginning=capital,
        all_capex_floor=replace(snap.owner_earnings.all_capex_floor,
            provenance=replace(p, components=(p, p, p))))
    row = sync._owner_earnings_row(replace(snap, owner_earnings=oe))
    expected = sync._source(leaf)
    assert expected["start"] == "2025-01-01"
    assert expected["canonical_tag"] == "EarningsPerShareDiluted"
    assert row["invested_capital_evidence"]["beginning"]["sources"] == [expected]
    assert row["sources"]["reported_earnings"] == expected


def test_combined_intangibles_unary_zero_preserves_source_context():
    e = {**inst("2025-12-31", 100, form="20-F"), "segments": "Class=Ordinary",
         "_source_unit": "EUR", "_source_document": "report.htm",
         "_annual_accession": "annual", "_annual_form": "20-F", "_annual_filed": "2026-03-01"}
    goodwill, combined = normalize._combined_goodwill_and_intangibles(
        {"IntangibleAssetsNetIncludingGoodwill": tagdata("USD", [e])}, None)
    assert goodwill.value == 0 and combined.value == 100
    assert goodwill.provenance == replace(combined.provenance, concept=goodwill.provenance.concept)


def test_recurring_rate_annualization_retains_reported_unit_and_annual_relationship():
    entries = []
    for start, end in (("2025-01-01", "2025-03-31"),
                       ("2025-04-01", "2025-06-30"), ("2025-07-01", "2025-09-30")):
        entries.append({**dur(start, end, .25, form="6-K"),
            "_source_unit": "EUR/shares", "_source_document": "payout.htm",
            "_annual_accession": "annual", "_annual_form": "20-F", "_annual_filed": "2026-03-01"})
    result = normalize._recurring_dividend_per_share(
        {"CommonStockDividendsPerShareDeclared": tagdata("USD/shares", entries)}, date(2025, 9, 30))
    assert result.value == Decimal("1.00")
    assert result.provenance.unit == "EUR/shares"
    assert result.provenance.document == "payout.htm"
    assert result.provenance.annual_accession == "annual"
    assert result.provenance.period_start == date(2025, 7, 1)
