"""Combined long-lived-asset cash payments must reach the existing FCF owner."""
from decimal import Decimal

import pytest

from helpers import tagdata
from screener.normalize import _ifrs_as_us_gaap, _owner_earnings, _with_fiscal_calendar


COMBINED_PURCHASE = (
    "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwill"
    "InvestmentPropertyAndOtherNoncurrentAssets"
)


@pytest.mark.parametrize("purchase", [647527000000, -647527000000])
def test_combined_ifrs_cash_purchase_builds_fcf_with_its_original_scope(purchase):
    # Sony's FY2025 filed cash-flow rows, JPY (20-F, F-10/F-11).
    def annual(value):
        return {
            "val": value, "start": "2024-04-01", "end": "2025-03-31",
            "form": "20-F", "accn": "0001193125-25-143137",
            "filed": "2025-06-20", "fy": 2025, "fp": "FY",
        }

    facts = {
        "ProfitLossAttributableToOwnersOfParent": tagdata("JPY", [annual(1141600000000)]),
        "AdjustmentsForDepreciationAndAmortisationExpense": tagdata(
            "JPY", [annual(1152987000000)]),
        "CashFlowsFromUsedInOperatingActivities": tagdata("JPY", [annual(2321675000000)]),
        COMBINED_PURCHASE: tagdata("JPY", [annual(purchase)]),
    }
    gaap = _with_fiscal_calendar(_ifrs_as_us_gaap(facts))
    gaap.reporting_currency = "JPY"
    gaap.currency_adapter = True
    gaap.canonical_adapter = False

    result = _owner_earnings(gaap, {}, None)

    assert result is not None
    assert result.free_cash_flow.value == Decimal("1674148000000")
    source = result.free_cash_flow.provenance.components[1]
    assert source.tag == f"ifrs-full:{COMBINED_PURCHASE}"
    assert source.canonical_tag == "PaymentsToAcquireProductiveAssets"
    assert source.accession == "0001193125-25-143137"
    assert str(source.period_start) == "2024-04-01"
    assert str(source.period_end) == "2025-03-31"
    assert len(result.free_cash_flow.provenance.components) == 2
