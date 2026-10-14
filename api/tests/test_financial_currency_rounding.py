from decimal import Decimal

import pytest

from screener import normalize, sync
from helpers import dur, facts_doc, inst, tagdata


def taxonomy(data, currency, *, canonical=False):
    taxo = normalize._with_fiscal_calendar(data)
    taxo.reporting_currency = currency
    taxo.currency_adapter = True
    taxo.canonical_adapter = canonical
    return taxo


@pytest.mark.parametrize("currency", ["CAD", "JPY", "EUR", "USD"])
@pytest.mark.parametrize("per_share", [False, True])
def test_reporting_unit_precedes_convenience_unit_and_retains_provenance(currency, per_share):
    suffix = "/shares" if per_share else ""
    requested, actual = "USD" + suffix, currency + suffix
    entry = dur("2025-01-01", "2025-12-31", "2.125") if per_share else inst("2025-12-31", 13_016_000_000)
    units = {requested: [{**entry, "val": 9_490_000_000}], actual: [entry]}
    taxo = taxonomy({"Value": {"units": units}}, currency)
    selected = normalize._entries(taxo, "Value", (requested,))
    assert selected[0]["val"] == entry["val"]
    assert normalize._fact("Value", "Value", selected[0]).provenance.unit == actual


@pytest.mark.parametrize("unit", ["USD", "USD/shares"])
def test_missing_reporting_unit_withholds_convenience_value(unit):
    taxo = taxonomy({"Value": tagdata(unit, [inst("2025-12-31", 10)])}, "CAD")
    assert normalize._entries(taxo, "Value", (unit,)) == []


def test_domestic_nonmonetary_and_future_period_selection():
    row = inst("2025-12-31", 10)
    taxo = {"Value": {"units": {"USD": [row, {**row, "end": "2030-01-01"}], "CAD": [{**row, "val": 20}]}}}
    selected = normalize._entries(taxo, "Value", ("USD",))
    assert len(selected) == 1 and selected[0]["val"] == 10
    marked = taxonomy({"Value": tagdata("shares", [row])}, "CAD")
    assert normalize._entries(marked, "Value", ("shares",))[0]["val"] == 10


@pytest.mark.parametrize("currency", ["USD", "JPY"])
def test_canonical_monetary_selection_requires_adapter_evidence(currency):
    row = inst("2025-12-31", 10)
    taxo = taxonomy({"Value": {"units": {currency: [row, {**row, "val": 20, "_canonical_adapter": True}],
                                          "USD": [row] if currency != "USD" else [row, {**row, "val": 20, "_canonical_adapter": True}]}}}, currency, canonical=True)
    selected = normalize._entries(taxo, "Value", ("USD",))
    assert len(selected) == 1 and selected[0]["val"] == 20


def test_ifrs_and_narrowed_annual_goodwill_keep_currency_context():
    taxo = normalize._ifrs_as_us_gaap({"Goodwill": {"units": {"CAD": [inst("2025-12-31", 13_016_000_000, form="40-F")],
                                                           "USD": [inst("2025-12-31", 9_490_000_000, form="40-F")]}}})
    taxo = taxonomy(taxo, "CAD")
    goodwill, _ = normalize._annual_goodwill_and_intangibles(taxo, {2025: "2025-12-31"}, 10)
    assert goodwill[2025].value == Decimal("13016000000")
    assert goodwill[2025].provenance.unit == "CAD"
    assert goodwill[2025].provenance.tag == "ifrs-full:Goodwill"


@pytest.mark.parametrize("currency", ["USD", "JPY"])
def test_narrowed_canonical_goodwill_keeps_adapter_gate(currency):
    entry = inst("2025-12-31", 20, form="10-K")
    taxo = taxonomy({"Goodwill": tagdata(currency, [
        {**entry, "val": 99}, {**entry, "_canonical_adapter": True}])}, currency, canonical=True)
    goodwill, _ = normalize._annual_goodwill_and_intangibles(taxo, {2025: "2025-12-31"}, 10)
    assert goodwill[2025].value == 20
    assert goodwill[2025].provenance.unit == currency


def ratio_facts(income, prior_equity=21_111_822):
    entries = [] if prior_equity is None else [inst("2024-12-31", prior_equity + 5_000_000, form="10-K")]
    return facts_doc({
        "Assets": tagdata("USD", [*entries, inst("2025-12-31", 35_469_527, form="10-K")]),
        "Liabilities": tagdata("USD", [inst("2024-12-31", 5_000_000, form="10-K"), inst("2025-12-31", 5_000_000, form="10-K")]),
        "NetIncomeLoss": tagdata("USD", [dur("2025-01-01", "2025-12-31", income)]),
        "Revenues": tagdata("USD", [dur("2025-01-01", "2025-12-31", "25790674.5")]),
        "GrossProfit": tagdata("USD", [dur("2025-01-01", "2025-12-31", abs(income))]),
    })


@pytest.mark.parametrize("income,expected", [(Decimal("-45142"), -0.18), (Decimal("45142"), 0.18),
                                            (Decimal("-45125"), -0.17), (Decimal("45125"), 0.17)])
def test_summary_roe_and_gross_round_once_while_history_stays_four_decimals(income, expected):
    status, row = sync._derive("0000000001", "TEST", ratio_facts(income))
    assert status == "ok"
    exact = float(income / Decimal("25790674.5") * 100)
    assert row["profitability"]["on_equity"] == expected
    assert row["profitability"]["gross"] == abs(expected)
    assert row["annual_ratios"][2025]["return_on_equity"] == round(exact, 4)
    assert row["annual_ratios"][2025]["gross_margin"] == round(abs(exact), 4)
    assert row["annual_ratios"][2025]["end"] == "2025-12-31"


@pytest.mark.parametrize("prior_equity", [None, -30_469_527])
def test_missing_or_nonpositive_average_equity_stays_unavailable(prior_equity):
    status, row = sync._derive("0000000001", "TEST", ratio_facts(Decimal("-45142"), prior_equity))
    assert status == "ok"
    assert row["profitability"]["on_equity"] is None
    assert "return_on_equity" not in row["annual_ratios"][2025]
