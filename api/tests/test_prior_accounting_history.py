"""Prior standards are display-only; current calculation contracts stay separate."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest

from helpers import dur, inst, tagdata
from screener import evidence, normalize, sync
from test_normalize_notes import _foreign_ifrs


def prior_us(currency="JPY"):
    def annual(year, value):
        return dur(f"{year}-01-01", f"{year}-12-31", value, form="20-F",
                   accn=f"old{year}", filed="2026-02-15" if year == 2025 else "2020-03-01")
    return {
        "Assets": tagdata(currency, [inst("2025-12-31", 1000, form="20-F",
                                         accn="old2025", filed="2026-02-15"),
                                     inst("2019-12-31", 900, form="20-F",
                                          accn="old2019", filed="2020-03-01")]),
        "EarningsPerShareDiluted": tagdata(currency + "/shares", [annual(2019, 10), annual(2025, 100)]),
        "NetIncomeLoss": tagdata(currency, [annual(2019, 100), annual(2025, 200)]),
        "NetCashProvidedByUsedInOperatingActivities": tagdata(currency, [annual(2019, 150), annual(2025, 300)]),
        "DepreciationAndAmortization": tagdata(currency, [annual(2019, 20), annual(2025, 30)]),
        "PaymentsToAcquirePropertyPlantAndEquipment": tagdata(currency, [annual(2019, 50), annual(2025, 80)]),
    }


def fixture():
    return {"facts": {"ifrs-full": _foreign_ifrs(), "us-gaap": prior_us()}}


RECEIPT = {"symbol": "TEST", "title": "Ordinary Shares", "ratio": None, "accn": "ifrs25"}


def test_prior_overlap_does_not_enter_current_snapshot_or_serialized_metrics():
    facts = fixture()
    current = {"facts": {"ifrs-full": deepcopy(facts["facts"]["ifrs-full"])}}
    before = normalize.build_snapshot("TEST", "0000000001", current, receipt=RECEIPT)
    after = normalize.build_snapshot("TEST", "0000000001", facts, receipt=RECEIPT)
    assert after == before
    assert after.annual_eps[2025].value == Decimal("2")
    group, = after.prior_accounting_history
    assert group["series"]["eps"][2025].value == 100
    assert group["series"]["eps"][2019].value == 10
    assert group["basis"] == "US-GAAP" and group["current_basis"] == "IFRS"
    assert group["currency"] == "JPY" and "not assessed" in group["share_basis"]
    old_row = sync._derive_evidence_result(
        evidence.EvidenceBundle("0000000001", "TEST", current, None, RECEIPT)).row
    new_row = sync._derive_evidence_result(
        evidence.EvidenceBundle("0000000001", "TEST", facts, None, RECEIPT)).row
    assert "prior_accounting_history" not in old_row
    history = new_row.pop("prior_accounting_history")
    assert new_row == old_row
    assert history[0]["series"]["eps"]["2019"]["source"]["tag"] == "us-gaap:EarningsPerShareDiluted"
    assert history[0]["series"]["eps"]["2019"]["source"]["accn"] == "old2019"


def test_prior_selection_preserves_reported_values_without_split_or_receipt_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Historical display must not guess a current share adjustment")
    monkeypatch.setattr(normalize, "_split_factor", forbidden)
    original = fixture()
    unchanged = deepcopy(original)
    group, = normalize._prior_accounting_history(original["facts"], "ifrs-full", "0000000001")
    assert original == unchanged
    assert group["series"]["eps"][2019].value == 10
    fcf = group["series"]["free_cash_flow"][2019]
    assert fcf.value == 100
    assert len(fcf.operation_inputs) == 2
    assert {fact.provenance.unit for fact in fcf.operation_inputs} == {"JPY"}
    serialized = sync._prior_accounting_history_row(SimpleNamespace(prior_accounting_history=(group,)))
    sources = serialized[0]["series"]["free_cash_flow"]["2019"]["source"]["components"]
    assert {source["unit"] for source in sources} == {"JPY"}


def test_prior_fcf_requires_matching_periods_and_excludes_translation_units():
    facts = fixture()
    facts["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["USD/shares"] = [
        {**facts["facts"]["us-gaap"]["EarningsPerShareDiluted"]["units"]["JPY/shares"][0], "val": 999}]
    facts["facts"]["us-gaap"]["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["JPY"][0]["start"] = "2019-01-02"
    group, = normalize._prior_accounting_history(facts["facts"], "ifrs-full", "0000000001")
    assert group["currency"] == "JPY" and group["series"]["eps"][2019].value == 10
    assert 2019 not in group["series"]["free_cash_flow"]
    assert group["series"]["free_cash_flow"][2025].value == 220


@pytest.mark.parametrize("problem", ["no_prior_balance", "same_filing_date", "quarter_only"])
def test_inactive_taxonomy_without_prior_annual_basis_does_not_create_history(problem):
    facts = fixture()
    prior = facts["facts"]["us-gaap"]
    if problem == "no_prior_balance":
        prior.pop("Assets")
    else:
        for data in prior.values():
            for entries in data["units"].values():
                for entry in entries:
                    if problem == "same_filing_date":
                        entry["filed"] = "2026-03-25"
                    else:
                        entry["form"] = "6-K"
    assert normalize._prior_accounting_history(facts["facts"], "ifrs-full", "0000000001") == ()
    assert normalize._prior_accounting_history(facts["facts"], "canonical", "E00009") == ()


def test_reverse_accounting_transition_uses_original_ifrs_provenance():
    prior = _foreign_ifrs(currency="EUR", filed="2026-02-15", accn="prior-ifrs")
    current = prior_us("USD")
    for data in current.values():
        for entries in data["units"].values():
            for entry in entries:
                if entry["end"] == "2025-12-31":
                    entry["filed"] = "2026-03-25"
                    entry["form"] = "10-K"
    group, = normalize._prior_accounting_history(
        {"ifrs-full": prior, "us-gaap": current}, "us-gaap", "0000000001")
    assert group["basis"] == "IFRS" and group["current_basis"] == "US-GAAP"
    assert group["currency"] == "EUR"
    assert group["series"]["eps"][2025].provenance.tag == "ifrs-full:DilutedEarningsLossPerShare"
