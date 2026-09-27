"""What the company owns and owes at one moment, and everything standing
between the assets and the common shareholder."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from screener.normalize import (UnsupportedFilerError,
                                _current_operating_lease_liability,
                                _fy_label, build_snapshot)
from helpers import *  # noqa: F403 — the shared fixtures, by design
from helpers import EPS, GAAP, build, dur, facts_doc, inst, tagdata, texts
from helpers import _dimensioned, _reported, _shares, _years, _yr  # underscored, so `import *` skips them


def test_balance_sheet_uses_latest_period_end():
    s = build()
    assert float(s.current_assets.value) == 300e9
    assert s.current_assets.provenance.period_end == date(2026, 3, 31)
    assert s.balance_sheet_date == date(2026, 3, 31)
    # goodwill only reported annually -> its own latest instant
    assert s.goodwill.provenance.period_end == date(2025, 12, 31)


def test_bank_short_term_funding_uses_rollup_without_double_counting():
    from screener.normalize import _short_term_debt

    def point(value, end="2026-06-30"):
        return tagdata("USD", [inst(end, value, accn="bank-q2", filed="2026-08-01")])

    gaap = {
        "FederalFundsPurchasedAndSecuritiesSoldUnderAgreementsToRepurchase": point(310091000),
        "FederalFundsPurchased": point(281600000),
        "SecuritiesSoldUnderAgreementsToRepurchase": point(28491000),
    }
    funding = _short_term_debt(gaap, None)
    assert funding is not None
    assert funding.value == Decimal(310091000)
    assert funding.provenance.accession == "bank-q2"
    gaap["OtherShortTermBorrowings"] = point(9000)
    assert _short_term_debt(gaap, None).value == Decimal(310100000)
    gaap["ShortTermBorrowings"] = point(400000000)
    assert _short_term_debt(gaap, None).value == Decimal(400000000)
    gaap["DebtCurrent"] = point(500000000)
    assert _short_term_debt(gaap, None).value == Decimal(500000000)


def test_later_exact_scale_comparative_cannot_corrupt_balance_history():
    from screener.normalize import (
        _annual_balances, _latest_instant, _taxonomy_at_end,
    )

    gaap = {"Assets": tagdata("USD", [
        inst("2022-09-30", 23_000_000, form="10-K",
             accn="assets-22", filed="2023-02-01"),
        inst("2023-09-30", 19_500_000, form="10-K",
             accn="assets-23-original", filed="2024-02-01"),
        inst("2023-09-30", 19_500_000_000, form="10-K",
             accn="0001213900-25-013985", filed="2025-02-01"),
        inst("2024-09-30", 17_000_000, form="10-K",
             accn="assets-24", filed="2026-02-01"),
    ])}

    annual = _annual_balances(gaap, ("Assets",))
    exact = _taxonomy_at_end(
        gaap, date(2023, 9, 30), annual_only=True)
    selected = _latest_instant(
        exact, "Assets", ("Assets",), not_before=date(2023, 9, 30))

    for fact in (annual[2023], selected):
        assert fact.value == Decimal("19500000")
        assert fact.provenance.accession == "assets-23-original"
        assert "adjacent annual balances" in fact.provenance.concept


def test_missing_stays_missing():
    s = build()
    assert s.preferred_stock is None
    assert s.long_term_debt is None


def test_stale_instant_fact_treated_as_missing():
    # filer stopped reporting Goodwill years ago -> the old fact must not resurface
    gaap = dict(GAAP)
    gaap["Goodwill"] = tagdata("USD", [
        inst("2017-12-30", 5.9e9, form="10-Q", accn="q417", filed="2018-02-02"),
    ])
    assert build(gaap).goodwill is None


def test_liabilities_derived_from_equity_identity():
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 600e9, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 400e9
    assert "derived" in s.total_liabilities.provenance.concept


def test_liabilities_not_derived_across_mismatched_dates():
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquity"] = tagdata("USD", [inst("2025-12-31", 600e9, form="10-K", accn="k25", filed="2026-02-15")])
    assert build(gaap).total_liabilities is None


def test_exact_total_equity_identity_replaces_an_abandoned_liability_total():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 450e9, form="10-K", accn="k25",
             filed="2026-02-15")])
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [
        inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = (
        tagdata("USD", [inst("2026-03-31", 600e9, accn="q126")]))

    s = build(gaap)
    assert float(s.total_liabilities.value) == 400e9
    assert s.total_liabilities.provenance.period_end == date(2026, 3, 31)
    assert "exact same-filing accounting identity" in s.total_liabilities.provenance.concept


def test_parent_only_or_mixed_accession_identity_cannot_rescue_a_stale_liability_total():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 450e9, form="10-K", accn="k25",
             filed="2026-02-15")])
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [
        inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquity"] = tagdata("USD", [
        inst("2026-03-31", 600e9, accn="q126")])
    assert build(gaap).total_liabilities is None

    gaap.pop("StockholdersEquity")
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = (
        tagdata("USD", [inst("2026-03-31", 600e9, accn="different-q126")]))
    assert build(gaap).total_liabilities is None


def test_unresolved_total_balance_mismatch_withholds_the_older_side():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 450e9, form="10-K", accn="k25",
             filed="2026-02-15")])
    s = build(gaap)
    assert float(s.total_assets.value) == 1000e9
    assert s.total_liabilities is None

    gaap = dict(GAAP)
    gaap["Assets"] = tagdata("USD", [
        inst("2025-12-31", 1000e9, form="10-K", accn="k25",
             filed="2026-02-15")])
    s = build(gaap)
    assert s.total_assets is None
    assert float(s.total_liabilities.value) == 400e9

    gaap.pop("AssetsCurrent")
    s = build(gaap)
    assert s.total_assets is None
    assert s.balance_sheet_date == date(2025, 12, 31)


def test_withheld_current_asset_does_not_disable_owner_earnings_scale_guard():
    gaap = dict(OE_GAAP)
    gaap["Assets"] = tagdata("USD", [
        inst("2025-12-31", 5_912, form="10-K", accn="k25",
             filed="2026-02-15")])

    s = build(gaap)

    assert s.total_assets is None  # the newer liability cannot be paired with it
    assert s.owner_earnings is None  # the implausible flow/asset scale still blocks it


def test_verified_brkr_liabilities_use_current_printed_statement_rows():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 3731.1e6, form="10-K", accn="k25",
             filed="2026-02-27"),
    ])
    gaap["LiabilitiesCurrent"] = tagdata("USD", [
        inst("2026-03-31", 1204.7e6, accn="q126")])
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [
        inst("2026-03-31", 1814.7e6, accn="q126")])
    gaap["OtherLiabilitiesNoncurrent"] = tagdata("USD", [
        inst("2026-03-31", 597.7e6, accn="q126")])

    # This reconstruction is filing-backed and issuer-specific. A generic filer
    # does not treat three details as exhaustive and does not pair the abandoned
    # direct total with the newer assets either.
    assert build(gaap).total_liabilities is None

    s = build_snapshot("BRKR", "0001109354", facts_doc(gaap))
    assert float(s.total_liabilities.value) == 3617.1e6
    assert s.total_liabilities.provenance.period_end == date(2026, 3, 31)
    assert "LiabilitiesCurrent" in s.total_liabilities.provenance.tag
    assert "OtherLiabilitiesNoncurrent" in s.total_liabilities.provenance.tag


def test_verified_jbht_liabilities_use_all_current_printed_statement_rows():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 4362070e3, form="10-K", accn="k25",
             filed="2026-02-24")])
    for tag, value in {
        "LiabilitiesCurrent": 1444663e3,
        "LongTermDebtNoncurrent": 1145337e3,
        "SelfInsuranceReserveNoncurrent": 487457e3,
        "OtherLiabilitiesNoncurrent": 298697e3,
        "DeferredIncomeTaxLiabilitiesNet": 911509e3,
    }.items():
        gaap[tag] = tagdata("USD", [
            inst("2026-06-30", value, accn="q226", filed="2026-07-24")])

    # The five rows are known to be exhaustive only for this filing pattern. A
    # generic filer with the same tags gets neither a guessed sum nor a stale
    # cross-period total.
    assert build(gaap).total_liabilities is None

    s = build_snapshot("JBHT", "0000728535", facts_doc(gaap))
    assert float(s.total_liabilities.value) == 4287663e3
    assert s.total_liabilities.provenance.period_end == date(2026, 6, 30)
    assert len(s.total_liabilities.provenance.components) == 5
    assert "SelfInsuranceReserveNoncurrent" in s.total_liabilities.provenance.tag
    assert "DeferredIncomeTaxLiabilitiesNet" in s.total_liabilities.provenance.tag


def test_current_and_noncurrent_liability_classes_replace_an_abandoned_total():
    gaap = dict(GAAP)
    gaap["Liabilities"] = tagdata("USD", [
        inst("2025-12-31", 5617601e3, form="10-K", accn="k25",
             filed="2026-02-23")])
    gaap["LiabilitiesCurrent"] = tagdata("USD", [
        inst("2026-03-31", 588670e3, accn="q126")])
    gaap["LiabilitiesNoncurrent"] = tagdata("USD", [
        inst("2026-03-31", 5157083e3, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 5745753e3
    assert s.total_liabilities.provenance.period_end == date(2026, 3, 31)
    assert "LiabilitiesCurrent" in s.total_liabilities.provenance.tag
    assert "LiabilitiesNoncurrent" in s.total_liabilities.provenance.tag

    # At one date the direct rollup remains authoritative; components can be a
    # filing fragment even when their nominal element names look exhaustive.
    gaap["Liabilities"] = tagdata("USD", [
        inst("2026-03-31", 6e9, accn="q126")])
    assert float(build(gaap).total_liabilities.value) == 6e9


def test_intangibles_summed_from_finite_and_indefinite():
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 20e9, accn="q126")])
    gaap["IndefiniteLivedIntangibleAssetsExcludingGoodwill"] = tagdata("USD", [inst("2026-03-31", 10e9, accn="q126")])
    s = build(gaap)
    assert float(s.intangibles.value) == 30e9
    assert "FiniteLived" in s.intangibles.provenance.tag
    assert "IndefiniteLived" in s.intangibles.provenance.tag


def test_other_intangible_assets_net_is_last_resort():
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["OtherIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 12e9, accn="q126")])
    s = build(gaap)
    assert float(s.intangibles.value) == 12e9
    assert "OtherIntangibleAssetsNet" in s.intangibles.provenance.tag
    # the specific tags win over the ambiguous residual line
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 20e9, accn="q126")])
    assert float(build(gaap).intangibles.value) == 20e9


def test_combined_goodwill_intangibles_line_fills_both_slots():
    gaap = {k: v for k, v in GAAP.items()
            if k not in ("Goodwill", "IntangibleAssetsNetExcludingGoodwill")}
    gaap["IntangibleAssetsNetIncludingGoodwill"] = tagdata("USD", [inst("2026-03-31", 80e9, accn="q126")])
    s = build(gaap)
    assert float(s.intangibles.value) == 80e9
    assert float(s.goodwill.value) == 0  # contained in the combined line, not missing
    assert "IntangibleAssetsNetIncludingGoodwill" in s.goodwill.provenance.tag


def test_combined_line_never_used_when_goodwill_tagged_separately():
    # goodwill + combined would double-count the goodwill inside the combined line
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["IntangibleAssetsNetIncludingGoodwill"] = tagdata("USD", [inst("2026-03-31", 80e9, accn="q126")])
    s = build(gaap)
    assert float(s.goodwill.value) == 50e9
    assert s.intangibles is None


def test_sum_includes_components_with_different_period_ends():
    # dropping the older-but-fresh component would understate debt -> false PASS risk
    gaap = dict(GAAP)
    gaap["LongTermDebtCurrent"] = tagdata("USD", [
        inst("2025-12-31", 11e9, form="10-K", accn="k25", filed="2026-02-15")])
    gaap["CommercialPaper"] = tagdata("USD", [inst("2026-03-31", 2e9, accn="q126")])
    s = build(gaap)
    assert float(s.short_term_debt.value) == 13e9
    assert s.short_term_debt.provenance.period_end == date(2026, 3, 31)


def test_staleness_guard_armed_without_assets_tag():
    # filer never tags Assets: LiabilitiesAndStockholdersEquity (== total assets by
    # identity) must anchor the guard so ancient facts cannot resurface
    gaap = {k: v for k, v in GAAP.items() if k != "Assets"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["Goodwill"] = tagdata("USD", [inst("2017-12-30", 5.9e9, form="10-Q", accn="q417", filed="2018-02-02")])
    s = build(gaap)
    assert float(s.total_assets.value) == 1000e9
    assert s.goodwill is None  # 2017 fact stays dead


def test_long_term_debt_total_tag_not_double_counted_in_short_bucket():
    gaap = dict(GAAP)
    gaap["LongTermDebt"] = tagdata("USD", [inst("2026-03-31", 100e9, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 11e9, accn="q126")])
    gaap["CommercialPaper"] = tagdata("USD", [inst("2026-03-31", 2e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 100e9  # includes current maturities already
    assert float(s.short_term_debt.value) == 2e9  # only genuine short-term borrowings


def test_foreign_statement_sums_disjoint_borrowings_and_funding_notes_lx_style():
    gaap = dict(GAAP)
    gaap["LongTermDebt"] = tagdata("USD", [
        inst("2025-12-31", 80.939e6, form="20-F", accn="f25", filed="2026-04-29")])
    gaap["LongTermNotesPayable"] = tagdata("USD", [
        inst("2025-12-31", 121.633e6, form="20-F", accn="f25", filed="2026-04-29")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [
        inst("2025-12-31", 10e6, form="20-F", accn="f25", filed="2026-04-29")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 202.572e6
    assert float(s.short_term_debt.value) == 10e6
    assert len(s.long_term_debt.provenance.components) == 2


def test_foreign_combined_debt_total_does_not_absorb_its_note_component_pmec_style():
    gaap = dict(GAAP)
    gaap["LongTermDebt"] = tagdata("USD", [
        inst("2025-12-31", 12.812e6, form="20-F", accn="f25", filed="2026-04-29")])
    gaap["LongTermNotesPayable"] = tagdata("USD", [
        inst("2025-12-31", 4.331e6, form="20-F", accn="f25", filed="2026-04-29")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [
        inst("2025-12-31", 8.481e6, form="20-F", accn="f25", filed="2026-04-29")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 12.812e6
    assert s.short_term_debt is None


def test_nci_extracted_when_liabilities_are_direct():
    gaap = dict(GAAP)
    gaap["MinorityInterest"] = tagdata("USD", [inst("2026-03-31", 6.6e9, accn="q126")])
    s = build(gaap)
    assert float(s.noncontrolling_interest.value) == 6.6e9


def test_nci_skipped_when_liabilities_derived_via_parent_equity():
    # L = L&SE - parent-only StockholdersEquity already leaves NCI inside liabilities;
    # deducting MinorityInterest again would double-count
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 600e9, accn="q126")])
    gaap["MinorityInterest"] = tagdata("USD", [inst("2026-03-31", 6.6e9, accn="q126")])
    s = build(gaap)
    assert s.noncontrolling_interest is None


def test_debt_capital_lease_obligation_tags():
    gaap = dict(GAAP)
    gaap["LongTermDebtAndCapitalLeaseObligations"] = tagdata("USD", [inst("2026-03-31", 40e9, accn="q126")])
    gaap["DebtCurrent"] = tagdata("USD", [inst("2026-03-31", 5e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 40e9
    assert float(s.short_term_debt.value) == 5e9


def test_bank_without_classified_balance_sheet():
    gaap = {k: v for k, v in GAAP.items() if k not in ("AssetsCurrent", "LiabilitiesCurrent")}
    s = build(gaap)
    assert s.current_assets is None
    assert s.current_liabilities is None
    assert s.total_assets is not None


def test_preferred_liquidation_preference_wins_over_par():
    gaap = dict(GAAP)
    gaap["PreferredStockValue"] = tagdata("USD", [inst("2026-03-31", 1e6, accn="q126")])
    gaap["PreferredStockLiquidationPreferenceValue"] = tagdata("USD", [inst("2026-03-31", 21.2e9, accn="q126")])
    s = build(gaap)
    assert float(s.preferred_stock.value) == 21.2e9
    assert "LiquidationPreference" in s.preferred_stock.provenance.tag


def test_debt_components_summed_dds_style():
    # no primary long-term tag: "other" LTD + subordinated debentures must sum
    gaap = dict(GAAP)
    gaap["OtherLongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 225.7e6, accn="q126")])
    gaap["JuniorSubordinatedDebentureOwedToUnconsolidatedSubsidiaryTrustNoncurrent"] = tagdata(
        "USD", [inst("2026-03-31", 200e6, accn="q126")])
    gaap["UnsecuredDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 96e6, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 425.7e6
    assert "sum of components" in s.long_term_debt.provenance.concept
    assert float(s.short_term_debt.value) == 96e6


def test_short_debt_sums_current_portion_and_commercial_paper():
    # AAPL-style: term debt current + commercial paper are separate lines
    gaap = dict(GAAP)
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 11e9, accn="q126")])
    gaap["CommercialPaper"] = tagdata("USD", [inst("2026-03-31", 2e9, accn="q126")])
    s = build(gaap)
    assert float(s.short_term_debt.value) == 13e9


def test_total_debt_rollup_tag_extracted():
    gaap = dict(GAAP)
    gaap["DebtAndCapitalLeaseObligations"] = tagdata("USD", [inst("2026-03-31", 1.94e9, accn="q126")])
    assert float(build(gaap).total_debt.value) == 1.94e9


def test_assume_absent_zero_requires_opt_in_and_clean_current_filing_window():
    gaap = {k: v for k, v in GAAP.items() if k not in ("Goodwill",)}
    # default: strict, nothing assumed
    assert build_snapshot("TEST", "0000000001", facts_doc(gaap)).assumed_zero == frozenset()
    # opt-in: debt + goodwill have zero evidence in the current filing window
    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert s.assumed_zero == {"debt", "goodwill"}


def test_asset_side_debt_securities_are_not_debt_evidence():
    # investments in debt securities and undrawn revolver capacity are not liabilities
    gaap = dict(GAAP)
    gaap["AvailableForSaleSecuritiesDebtSecurities"] = tagdata("USD", [inst("2026-03-31", 7.6e9, accn="q126")])
    gaap["LineOfCreditFacilityMaximumBorrowingCapacity"] = tagdata("USD", [inst("2026-03-31", 800e6, accn="q126")])
    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert "debt" in s.assumed_zero


def test_owner_earnings_and_invested_capital():
    s = build(OE_GAAP)
    oe = s.owner_earnings
    # all-capex floor: 70 reported earnings + 12 D&A - 12 total capex
    assert float(oe.all_capex_floor.value) == 70e9
    assert float(oe.maintenance_estimate.value) == 70e9
    assert float(oe.free_cash_flow.value) == 63e9
    # assets 1000 - cash 40 - non-interest-bearing current liabilities 150
    assert float(oe.invested_capital) == 810e9
    assert round(float(oe.all_capex_return), 4) == round(70 / 810 * 100, 4)
    assert round(float(oe.maintenance_estimate_return), 4) == round(70 / 810 * 100, 4)


@pytest.mark.parametrize("tag", (
    "PaymentsToAcquireOilAndGasProperty",
    "PaymentsToAcquireOilAndGasPropertyAndEquipment",
    "PaymentsToAcquireOilAndGasEquipment",
))
def test_owner_earnings_treats_cash_oil_and_gas_asset_purchases_as_capex(tag):
    gaap = dict(OE_GAAP)
    gaap.pop("PaymentsToAcquirePropertyPlantAndEquipment")
    gaap[tag] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15")])

    fcf = build(gaap).owner_earnings.free_cash_flow

    assert fcf.value == Decimal("63000000000")
    assert fcf.provenance.components[1].canonical_tag == tag


def test_finkle_three_way_fcf_reconciliation_matches_or_exposes_the_gap():
    gaap = dict(OE_GAAP)
    gaap["Revenues"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 200e9, accn="k25", filed="2026-02-15")])
    gaap["IncomeTaxesPaidNet"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 25e9, accn="k25", filed="2026-02-15")])
    # With no change in operating capital: 87 CFO - 12 capex = 100 operating
    # income - 25 cash tax = 200 revenue - 100 operating costs - 25 cash tax.
    gaap["NetCashProvidedByUsedInOperatingActivities"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 87e9, accn="k25", filed="2026-02-15")])

    from screener.sync import _owner_earnings_row
    payload = _owner_earnings_row(build(gaap))
    reconciliation = payload["fcf_reconciliation"]

    assert reconciliation["status"] == "MATCH"
    assert reconciliation["net_investment_in_operating_capital"] == 0
    assert [method["value"] for method in reconciliation["methods"].values()] == [
        75e9, 75e9, 75e9]
    assert all(sum(value for _, value in method["components"]) == method["value"]
               for method in reconciliation["methods"].values())

    gaap["NetCashProvidedByUsedInOperatingActivities"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 75e9, accn="k25", filed="2026-02-15")])
    reconciliation = _owner_earnings_row(build(gaap))["fcf_reconciliation"]
    assert reconciliation["status"] == "MISMATCH"
    assert reconciliation["spread"] == 12e9

    gaap.pop("IncomeTaxesPaidNet")
    reconciliation = _owner_earnings_row(build(gaap))["fcf_reconciliation"]
    assert reconciliation["status"] == "INCOMPLETE"
    assert "same-period cash taxes paid" in reconciliation["missing"]


def test_owner_earnings_uses_common_income_and_deducts_a_filed_preferred_claim():
    preferred = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 5e9, accn="k25", filed="2026-02-15")])
    direct = dict(OE_GAAP)
    direct["NetIncomeLossAvailableToCommonStockholdersBasic"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 60e9, accn="k25", filed="2026-02-15")])
    direct["DividendsPreferredStock"] = preferred
    oe = build(direct).owner_earnings
    assert float(oe.maintenance_estimate.value) == 60e9
    assert oe.all_capex_floor.provenance.components[0].tag.endswith(
        "NetIncomeLossAvailableToCommonStockholdersBasic")

    fallback = dict(OE_GAAP)
    fallback["DividendsPreferredStock"] = preferred
    oe = build(fallback).owner_earnings
    assert float(oe.maintenance_estimate.value) == 65e9
    earnings = oe.all_capex_floor.provenance.components[0]
    assert earnings.tag.endswith("NetIncomeLoss - us-gaap:DividendsPreferredStock")
    assert len(earnings.components) == 2


def test_owner_returns_use_exact_average_beginning_and_ending_capital():
    gaap = dict(OE_GAAP)
    gaap["Assets"] = tagdata("USD", [
        inst("2024-12-31", 800e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 1200e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 1200e9, accn="q126"),
    ])
    gaap["LiabilitiesCurrent"] = tagdata("USD", [
        inst("2024-12-31", 100e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 200e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 200e9, accn="q126"),
    ])
    gaap["CashAndCashEquivalentsAtCarryingValue"] = tagdata("USD", [
        inst("2024-12-31", 50e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 100e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 100e9, accn="q126"),
    ])
    oe = build(gaap).owner_earnings
    assert float(oe.invested_capital_beginning.value) == 650e9
    assert float(oe.invested_capital_ending.value) == 900e9
    assert float(oe.invested_capital) == 775e9
    assert float(oe.capital_including_cash_beginning.value) == 700e9
    assert float(oe.capital_including_cash_ending.value) == 1000e9
    assert float(oe.capital_including_cash) == 850e9
    assert float(oe.all_capex_return) == pytest.approx(70 / 775 * 100)
    assert float(oe.all_capex_return_including_cash) == pytest.approx(70 / 850 * 100)


def test_owner_return_reads_current_generation_held_to_maturity_balance():
    """Vertiv's audited short-term-investment row uses the post-CECL tag.

    Both the explicit prior-year dash and the current carrying amount survive
    Company Facts.  They are exact balance-sheet evidence, not absent-zero
    assumptions, and must be deducted from their respective capital endpoints.
    """
    gaap = dict(OE_GAAP)
    gaap.pop("ShortTermInvestments")
    tag = (
        "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent")
    accession = "0001674101-26-000008"
    gaap[tag] = tagdata("USD", [
        inst("2024-12-31", 0, form="10-K", accn=accession,
             filed="2026-02-13"),
        inst("2025-12-31", 99.5e9, form="10-K", accn=accession,
             filed="2026-02-13"),
    ])

    oe = build_snapshot("VRT", "0001674101", facts_doc(gaap)).owner_earnings

    assert oe.invested_capital_beginning.value == Decimal("810000000000")
    assert oe.invested_capital_ending.value == Decimal("710500000000")
    assert oe.invested_capital == Decimal("760250000000")
    assert tag in oe.invested_capital_ending.provenance.tag


def test_modern_held_to_maturity_cash_equivalent_is_not_double_counted():
    """Westlake's HTM note amount is already inside cash equivalents.

    The element alone therefore cannot prove a separate short-term-investment
    balance. Without a verified statement context, missing stays missing and the
    cash-excluded return is withheld instead of deducting the same cash twice.
    """
    gaap = dict(OE_GAAP)
    gaap.pop("ShortTermInvestments")
    tag = (
        "DebtSecuritiesHeldToMaturityAmortizedCostAfterAllowanceForCreditLossCurrent")
    accession = "0001262823-26-000016"
    gaap[tag] = tagdata("USD", [
        inst("2024-12-31", 1009e9, form="10-K", accn=accession,
             filed="2026-02-18"),
        inst("2025-12-31", 0, form="10-K", accn=accession,
             filed="2026-02-18"),
    ])

    oe = build_snapshot("WLK", "0001262823", facts_doc(gaap)).owner_earnings

    assert oe.invested_capital is None
    assert oe.all_capex_return is None
    assert any("short-term-investment fact" in caveat for caveat in oe.caveats)


def test_owner_return_is_withheld_without_both_exact_balance_sheets_or_current_debt():
    missing_beginning = dict(OE_GAAP)
    missing_beginning["Assets"] = tagdata("USD", [
        inst("2025-12-31", 1000e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 1000e9, accn="q126"),
    ])
    oe = build(missing_beginning).owner_earnings
    assert oe.invested_capital is None and oe.all_capex_return is None
    assert oe.capital_including_cash is None

    no_current_debt_evidence = {k: v for k, v in OE_GAAP.items() if k != "DebtCurrent"}
    snapshot = build(no_current_debt_evidence)
    oe = snapshot.owner_earnings
    assert oe.invested_capital is None and oe.all_capex_return is None
    assert any("short-term-debt fact" in caveat for caveat in oe.caveats)
    # Discovery estimates may bound optional deductions, never debt.
    assert snapshot.conservative_operating_returns[2025].nopat_roic is None


def test_explicit_absent_debt_opt_in_reaches_owner_capital_and_dashboard_fields():
    """Filing silence is usable only after the whole-history evidence gate passes.

    The same explicit decision must reach both the Graham debt test and the
    short-term-debt input used to separate interest-bearing from operating current
    liabilities; otherwise the UI says debt-free while its capital returns stay blank.
    """
    from screener.sync import _derive

    gaap = {k: v for k, v in OE_GAAP.items() if k != "DebtCurrent"}
    strict = build_snapshot("TEST", "0000000001", facts_doc(gaap))
    assert strict.assumed_zero == frozenset()
    assert strict.owner_earnings.capital_including_cash is None

    assumed = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert "debt" in assumed.assumed_zero
    assert float(assumed.owner_earnings.capital_including_cash) == 850e9
    assert float(assumed.owner_earnings.invested_capital) == 810e9
    assert any("short-term debt was assumed to be 0" in caveat
               for caveat in assumed.owner_earnings.caveats)

    status, row = _derive(
        "0000000001", "TEST", facts_doc(gaap), assume_absent_zero=True)
    assert status == "ok"
    assert row["assumptions"] == ["debt"]
    assert row["debt"] == row["total_debt"] == 0
    assert row["long_term_debt"] == row["short_term_debt"] == 0
    assert row["debt_to_equity"] == 0
    assert {criterion["n"]: criterion for criterion in row["criteria"]}[3]["note"].startswith(
        "assumed 0")


def test_explicit_short_debt_zero_keeps_filed_long_debt_in_combined_ratio():
    """EPAM-shaped: noncurrent debt is filed, while the current bucket is silent."""
    from screener.sync import _derive

    gaap = {k: v for k, v in OE_GAAP.items() if k != "DebtCurrent"}
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [
        inst("2024-12-31", 25e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 25e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 25e9, accn="q126"),
    ])

    assumed = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert assumed.assumed_zero == {"short_term_debt"}
    assert assumed.long_term_debt.value == 25e9
    assert assumed.short_term_debt is None
    assert assumed.owner_earnings.capital_including_cash is not None

    status, row = _derive(
        "0000000001", "TEST", facts_doc(gaap), assume_absent_zero=True)
    assert status == "ok"
    assert row["long_term_debt"] == row["debt"] == 25e9
    assert row["short_term_debt"] == 0
    assert row["total_debt"] is None  # no filing-backed rollup was invented
    assert row["debt_to_equity"] == pytest.approx(25 / 600, abs=0.0001)
    criterion = {item["n"]: item for item in row["criteria"]}[3]
    assert criterion["status"] in {"PASS", "FAIL"}
    assert "assumed 0 for short-term debt" in criterion["note"]


def test_operating_capital_reads_current_debt_even_when_long_debt_includes_it():
    """NIKE's LongTermDebt includes current maturities. That suppresses the
    current component in total-debt reconciliation, but NIBCL still needs it."""
    gaap = {k: v for k, v in OE_GAAP.items() if k != "DebtCurrent"}
    gaap["LongTermDebt"] = tagdata("USD", [
        inst("2024-12-31", 105e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 110e9, form="10-K", accn="k25", filed="2026-02-15"),
    ])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [
        inst("2024-12-31", 5e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 10e9, form="10-K", accn="k25", filed="2026-02-15"),
    ])

    oe = build(gaap).owner_earnings

    # assets - (current liabilities - current debt) - cash - investments
    assert float(oe.invested_capital_beginning.value) == 815e9
    assert float(oe.invested_capital_ending.value) == 820e9
    assert float(oe.invested_capital) == 817.5e9
    assert not any("short-term-debt fact" in caveat for caveat in oe.caveats)


@pytest.mark.parametrize("missing_tag", [
    "CashAndCashEquivalentsAtCarryingValue",
    "ShortTermInvestments",
])
def test_cash_excluded_return_is_withheld_when_liquidity_evidence_is_missing(missing_tag):
    gaap = {k: v for k, v in OE_GAAP.items() if k != missing_tag}
    oe = build(gaap).owner_earnings
    assert oe.invested_capital is None
    assert oe.all_capex_return is None
    assert oe.capital_including_cash is not None
    assert oe.all_capex_return_including_cash is not None
    assert any("cash-excluded invested capital is withheld" in caveat
               for caveat in oe.caveats)


def test_combined_cash_rollup_needs_the_restricted_portion_for_excluded_capital():
    gaap = {k: v for k, v in OE_GAAP.items()
            if k != "CashAndCashEquivalentsAtCarryingValue"}
    gaap["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"] = tagdata("USD", [
        inst("2024-12-31", 50e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 50e9, form="10-K", accn="k25", filed="2026-02-15"),
    ])
    oe = build(gaap).owner_earnings
    assert oe.invested_capital is None and oe.all_capex_return is None
    assert oe.capital_including_cash is not None
    assert any("includes restricted cash" in caveat for caveat in oe.caveats)


def test_owner_cash_diagnostics_remain_separate_and_period_aligned():
    gaap = dict(OE_GAAP)
    gaap["NetCashProvidedByUsedInOperatingActivities"] = tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 70e9, accn="k23", filed="2024-02-15"),
        dur("2024-01-01", "2024-12-31", 72e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 75e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15")])
    gaap["Revenues"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15")])
    gaap["ShareBasedCompensation"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 5e9, accn="k25", filed="2026-02-15")])
    gaap["PaymentsToAcquireBusinessesNetOfCashAcquired"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15")])
    gaap["PaymentsForRepurchaseOfCommonStock"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 7e9, accn="k25", filed="2026-02-15")])
    gaap["PaymentsToDevelopSoftware"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 2e9, accn="k25", filed="2026-02-15")])
    gaap["PaymentsToAcquireIntangibleAssets"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 3e9, accn="k25", filed="2026-02-15")])
    gaap["IncreaseDecreaseInOperatingCapital"] = tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 1e9, accn="k23", filed="2024-02-15"),
        dur("2024-01-01", "2024-12-31", 2e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 4e9, accn="k25", filed="2026-02-15"),
    ])
    oe = build(gaap).owner_earnings
    assert float(oe.free_cash_flow.value) == 63e9
    assert float(oe.free_cash_flow_after_stock_compensation.value) == 58e9
    assert float(oe.free_cash_flow_after_acquisitions.value) == 53e9
    assert float(oe.expanded_free_cash_flow.value) == 58e9
    assert float(oe.capitalized_intangible_investment.value) == 5e9
    assert float(oe.operating_cash_flow_before_working_capital.value) == 79e9
    assert float(oe.average_working_capital_cash_effect_3y) == pytest.approx(-7e9 / 3)
    assert float(oe.stock_compensation_to_revenue) == 5
    assert float(oe.stock_compensation_to_free_cash_flow) == pytest.approx(5 / 63 * 100)
    assert float(oe.acquisitions_to_free_cash_flow) == pytest.approx(10 / 63 * 100)
    assert oe.acquisition_years_10 == 1
    assert float(oe.acquisitions_to_capex_10) == pytest.approx(10 / 12 * 100)
    assert float(oe.annual[2025].expanded_free_cash_flow_per_share.value) == 5.8

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(build(gaap))["annual_per_share"]["2025"][
        "cash_flow_bridge"]
    assert bridge["net_income"][0] == 70e9
    assert bridge["depreciation_and_amortisation"][0] == 12e9
    assert bridge["stock_compensation"][0] == 5e9
    assert bridge["other_operating_cash_flow_adjustments"] == -8e9
    assert bridge["working_capital_cash_effect"][0] == -4e9
    assert bridge["operating_cash_flow"][0] == 75e9
    assert bridge["total_capital_expenditure"][0] == 12e9
    assert bridge["free_cash_flow"] == 63e9
    assert bridge["share_repurchases"][0] == 7e9
    assert bridge["share_repurchases"][1:] == [
        "us-gaap:PaymentsForRepurchaseOfCommonStock",
        "10-K", "k25", "2025-12-31",
    ]
    assert bridge["operating_cash_flow"][1:] == [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "10-K", "k25", "2025-12-31",
    ]


def test_annual_cash_bridge_residual_absorbs_unseparated_stock_compensation():
    gaap = dict(OE_GAAP)
    gaap["IncreaseDecreaseInOperatingCapital"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 4e9,
            accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9,
            accn="k25", filed="2026-02-15"),
    ])

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(build(gaap))["annual_per_share"]["2025"][
        "cash_flow_bridge"]

    assert "stock_compensation" not in bridge
    # 75 OCF - 70 net income - 12 D&A - (-4 WC) = -3 other. Nothing
    # missing is set to zero; it remains inside the labelled residual.
    assert bridge["other_operating_cash_flow_adjustments"] == -3e9


def test_working_capital_cash_effect_reconstructs_complete_jnj_statement_family():
    gaap = dict(OE_GAAP)

    def component(tag, value):
        gaap[tag] = tagdata("USD", [
            dur("2025-01-01", "2025-12-31", value,
                accn="0000200406-26-000016", filed="2026-02-11"),
        ])

    # JNJ's filed XBRL values describe increases/decreases in the balances;
    # the 10-K cash-flow statement displays the corresponding signed cash
    # effects: (1,781), (1,450), 2,377, (6,167), and (5,697) million.
    component("IncreaseDecreaseInAccountsReceivable", 1_781e6)
    component("IncreaseDecreaseInInventories", 1_450e6)
    component("IncreaseDecreaseInAccountsPayableAndAccruedLiabilities", 2_377e6)
    component("IncreaseDecreaseInOtherOperatingAssets", 6_167e6)
    component("IncreaseDecreaseInOtherOperatingLiabilities", -5_697e6)
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9,
            accn="0000200406-26-000016", filed="2026-02-11"),
    ])

    snapshot = build_snapshot("JNJ", "0000200406", facts_doc(gaap))
    fact = snapshot.owner_earnings.annual[2025].working_capital_cash_effect
    assert fact is not None
    assert "complete reported component family" in fact.provenance.concept

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(snapshot)["annual_per_share"]["2025"][
        "cash_flow_bridge"]

    assert bridge["working_capital_cash_effect"][0] == -12_718e6
    assert "IncreaseDecreaseInAccountsReceivable" in (
        bridge["working_capital_cash_effect"][1])
    assert bridge["other_operating_cash_flow_adjustments"] == pytest.approx(
        75e9 - 70e9 - 12e9 - (-12_718e6))


def test_working_capital_cash_effect_uses_complete_amat_statement_family():
    from screener.normalize import _annual_working_capital_cash_effect

    gaap = {}
    accession = "0001628280-25-056742"
    periods = (
        ("2022-10-31", "2023-10-29"),
        ("2023-10-30", "2024-10-27"),
        ("2024-10-28", "2025-10-26"),
    )
    values = {
        "IncreaseDecreaseInAccountsReceivable": (-903e6, 69e6, -49e6),
        "IncreaseDecreaseInInventories": (-207e6, -304e6, 494e6),
        "IncreaseDecreaseInOtherOperatingAssets": (48e6, -287e6, 119e6),
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": (
            -138e6, 281e6, 307e6),
        "IncreaseDecreaseInContractWithCustomerLiability": (-167e6, -126e6, -283e6),
        "IncreaseDecreaseInAccruedIncomeTaxesPayable": (-20e6, 389e6, 250e6),
        "IncreaseDecreaseInOtherOperatingLiabilities": (38e6, 51e6, 90e6),
        # Printed above the working-capital section as a non-cash tax adjustment.
        "IncreaseDecreaseInDeferredIncomeTaxes": (-24e6, 633e6, -639e6),
    }
    for tag, annual_values in values.items():
        gaap[tag] = tagdata("USD", [
            dur(start, end, value, accn=accession, filed="2025-12-12")
            for (start, end), value in zip(periods, annual_values)
        ])

    series = _annual_working_capital_cash_effect(gaap, "0000006951")

    assert {year: fact.value for year, fact in series.items()} == {
        2023: Decimal("775000000"),
        2024: Decimal("1117000000"),
        2025: Decimal("-200000000"),
    }
    assert len(series[2025].provenance.components) == 7
    assert all(
        part.tag != "us-gaap:IncreaseDecreaseInDeferredIncomeTaxes"
        for part in series[2025].provenance.components
    )


@pytest.mark.parametrize(
    ("cik", "accession", "end", "cash_rows", "permitted_extra"),
    [
        ("0001874178", "0001874178-26-000008", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -112_000_000,
            "IncreaseDecreaseInInventories": 522_000_000,
            "IncreaseDecreaseInOtherOperatingAssets": 9_000_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 571_000_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -53_000_000,
            "IncreaseDecreaseInContractWithCustomerLiability": 503_000_000,
        }, None),
        ("0000082020", "0001104659-26-020480", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -3_951_000,
            "IncreaseDecreaseInInventories": -3_222_000,
            "IncreaseDecreaseInOtherOperatingAssets": 402_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 4_537_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -101_000,
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": -268_000,
        }, None),
        ("0001314727", "0001314727-25-000090", "2025-09-27", {
            "IncreaseDecreaseInAccountsReceivable": -21_873_000,
            "IncreaseDecreaseInInventories": 51_729_000,
            "IncreaseDecreaseInOtherOperatingAssets": 10_483_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": -14_439_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -459_000,
            "IncreaseDecreaseInEmployeeRelatedLiabilities": 5_232_000,
            "IncreaseDecreaseInContractWithCustomerLiability": -2_737_000,
        }, None),
        ("0001944048", "0001944048-26-000030", "2025-12-28", {
            "IncreaseDecreaseInAccountsReceivable": -112_000_000,
            "IncreaseDecreaseInInventories": -12_000_000,
            "IncreaseDecreaseInOtherOperatingAssets": 122_000_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 41_000_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -3_000_000,
            "IncreaseDecreaseInEmployeeRelatedLiabilities": 43_000_000,
            "IncreaseDecreaseInAccruedTaxesPayable": -27_000_000,
        }, None),
        ("0000015615", "0000015615-26-000020", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -140_809_000,
            "IncreaseDecreaseInInventories": -2_709_000,
            "IncreaseDecreaseInOtherOperatingAssets": -65_312_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 250_533_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": 2_661_000,
            "IncreaseDecreaseInContractWithCustomerAsset": -446_613_000,
            "IncreaseDecreaseInContractWithCustomerLiability": -7_305_000,
        }, None),
        ("0000906553", "0001437749-26-004908", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": 47_974_000,
            "IncreaseDecreaseInInventories": 1_046_000,
            "IncreaseDecreaseInOtherOperatingAssets": 1_043_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 373_730_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -1_904_000,
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": 11_701_000,
            "IncreaseDecreaseInIncomeTaxesReceivable": 8_068_000,
            "IncreaseDecreaseInOperatingLeaseLiability": -91_594_000,
        }, "IncreaseDecreaseInDeferredIncomeTaxes"),
        ("0001035983", "0001104659-26-017530", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -594_298_000,
            "IncreaseDecreaseInInventories": -24_411_000,
            "IncreaseDecreaseInOtherOperatingAssets": -386_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": -276_051_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": 28_699_000,
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": -91_359_000,
            "IncreaseDecreaseInContractWithCustomerAsset": -19_945_000,
            "IncreaseDecreaseInContractWithCustomerLiability": 910_084_000,
        }, None),
        ("0001534675", "0001493152-26-008465", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -25_348_000,
            "IncreaseDecreaseInInventories": -45_083_000,
            "IncreaseDecreaseInOtherOperatingAssets": -5_877_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 8_124_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -92_000,
            "IncreaseDecreaseInPrepaidExpense": -4_223_000,
            "IncreaseDecreaseInCommodityContractAssetsAndLiabilities": 31_362_000,
            "IncreaseDecreaseInAccruedIncomeTaxesPayable": -3_805_000,
            "IncreaseDecreaseInEmployeeRelatedLiabilities": 1_884_000,
            "IncreaseDecreaseInDueToRelatedParties": 683_000,
        }, None),
        ("0001639825", "0001639825-26-000038", "2026-06-30", {
            "IncreaseDecreaseInAccountsReceivable": 18_600_000,
            "IncreaseDecreaseInInventories": 81_300_000,
            "IncreaseDecreaseInOtherOperatingAssets": -4_700_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": -71_100_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -5_500_000,
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": 34_200_000,
            "IncreaseDecreaseInContractWithCustomerLiability": -11_000_000,
            "IncreaseDecreaseInOperatingLeaseLiability": -77_300_000,
        }, None),
        ("0001819574", "0001628280-26-042242", "2026-03-31", {
            "IncreaseDecreaseInAccountsReceivable": -2_940_000,
            "IncreaseDecreaseInInventories": 11_110_000,
            "IncreaseDecreaseInOtherOperatingAssets": -629_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": -13_389_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -2_246_000,
            "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets": -1_577_000,
            "IncreaseDecreaseInContractWithCustomerLiability": 977_000,
            "IncreaseDecreaseInOperatingLeaseLiability": -5_717_000,
        }, None),
        ("0000278165", "0001493152-26-016891", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": 7_909_000,
            "IncreaseDecreaseInInventories": 3_501_000,
            "IncreaseDecreaseInOtherOperatingAssets": 142_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": -2_838_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": -1_428_000,
            "IncreaseDecreaseInPrepaidExpense": 119_000,
            "IncreaseDecreaseInDeferredIncomeTaxes": 450_000,
            "IncreaseDecreaseInAccruedTaxesPayable": -1_370_000,
            "IncreaseDecreaseInOperatingLeaseLiability": -200_000,
        }, None),
        ("0001590364", "0001628280-26-012940", "2025-12-31", {
            "IncreaseDecreaseInAccountsReceivable": -42_425_000,
            "IncreaseDecreaseInInventories": -645_464_000,
            "IncreaseDecreaseInOtherOperatingAssets": -136_784_000,
            "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 122_779_000,
            "IncreaseDecreaseInOtherOperatingLiabilities": 356_000,
            "IncreaseDecreaseInDueToRelatedParties": -960_000,
        }, None),
    ],
    ids=("RIVN", "USLM", "SONO", "KVUE", "MTZ", "BYD", "FIX", "TGLS",
         "PTON", "BARK", "OMQS", "FTAI"),
)
def test_verified_working_capital_families_match_published_statement(
    cik, accession, end, cash_rows, permitted_extra,
):
    from screener.normalize import _annual_working_capital_cash_effect

    asset_movements = {
        "IncreaseDecreaseInAccountsReceivable",
        "IncreaseDecreaseInInventories",
        "IncreaseDecreaseInOtherOperatingAssets",
        "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
        "IncreaseDecreaseInPrepaidExpense",
        "IncreaseDecreaseInDeferredIncomeTaxes",
        "IncreaseDecreaseInIncomeTaxesReceivable",
        "IncreaseDecreaseInContractWithCustomerAsset",
        "IncreaseDecreaseInCommodityContractAssetsAndLiabilities",
    }
    start = (date.fromisoformat(end) - timedelta(days=364)).isoformat()
    gaap = {
        tag: tagdata("USD", [dur(
            start, end, -cash_effect if tag in asset_movements else cash_effect,
            accn=accession, filed="2026-09-01",
        )])
        for tag, cash_effect in cash_rows.items()
    }
    if permitted_extra:
        gaap[permitted_extra] = tagdata("USD", [dur(
            start, end, 123, accn=accession, filed="2026-09-01",
        )])

    fact = _annual_working_capital_cash_effect(gaap, cik)[int(end[:4])]

    assert fact.value == Decimal(sum(cash_rows.values()))
    assert len(fact.provenance.components) == len(cash_rows)


def test_operating_capital_balance_change_is_inverted_in_both_directions():
    """The standard rollup is a balance movement, not a signed cash effect.

    Coca-Cola reports a positive balance movement and a negative cash effect;
    HNI and Vertiv supply the opposite-direction control.  Pin both signs so a
    future cleanup cannot accidentally restore the raw Company Facts value.
    """
    from screener.normalize import _annual_working_capital_cash_effect

    gaap = {
        "IncreaseDecreaseInOperatingCapital": tagdata("USD", [
            dur("2024-01-01", "2024-12-31", 7_208e6,
                accn="ko25", filed="2026-02-20"),
            dur("2025-01-01", "2025-12-31", -339_300_000,
                accn="vrt25", filed="2026-02-13"),
        ]),
    }

    series = _annual_working_capital_cash_effect(gaap)

    assert series[2024].value == Decimal("-7208000000")
    assert series[2025].value == Decimal("339300000")
    assert series[2025].provenance.tag == (
        "-(us-gaap:IncreaseDecreaseInOperatingCapital)")


def test_working_capital_effect_rejects_a_repeated_later_scale_error():
    from screener.normalize import _annual_working_capital_cash_effect

    tag = "IncreaseDecreaseInOperatingCapital"
    gaap = {tag: tagdata("USD", [
        dur("2020-01-01", "2020-12-31", 20,
            accn="wc20", filed="2021-02-01"),
        dur("2021-01-01", "2021-12-31", 30,
            accn="wc21-a", filed="2022-02-01"),
        dur("2021-01-01", "2021-12-31", 30,
            accn="wc21-b", filed="2023-02-01"),
        dur("2021-01-01", "2021-12-31", 30_000,
            accn="0001213900-25-013985", filed="2024-02-01"),
        dur("2022-01-01", "2022-12-31", -1_000,
            accn="wc22", filed="2025-02-01"),
    ])}

    series = _annual_working_capital_cash_effect(gaap)

    assert series[2021].value == Decimal("-30")
    assert series[2021].provenance.components[0].accession == "wc21-b"


def test_working_capital_component_family_refuses_incomplete_or_overlapping_rows():
    base = dict(OE_GAAP)
    base["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9,
            accn="k25", filed="2026-02-15"),
    ])

    def add(gaap, tag, value):
        gaap[tag] = tagdata("USD", [
            dur("2025-01-01", "2025-12-31", value,
                accn="k25", filed="2026-02-15"),
        ])

    components = {
        "IncreaseDecreaseInAccountsReceivable": 1e9,
        "IncreaseDecreaseInInventories": 2e9,
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 3e9,
        "IncreaseDecreaseInOtherOperatingAssets": 4e9,
        "IncreaseDecreaseInOtherOperatingLiabilities": 5e9,
    }
    incomplete = dict(base)
    for tag, value in list(components.items())[:-1]:
        add(incomplete, tag, value)
    overlapping = dict(base)
    for tag, value in components.items():
        add(overlapping, tag, value)
    add(overlapping, "IncreaseDecreaseInDeferredRevenue", 6e9)

    for gaap in (incomplete, overlapping):
        item = build_snapshot(
            "JNJ", "0000200406", facts_doc(gaap)).owner_earnings.annual[2025]
        assert item.working_capital_cash_effect is None


def test_working_capital_component_family_refuses_unverified_ennis_pattern():
    gaap = dict(OE_GAAP)
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9,
            accn="0001193125-26-213764", filed="2026-05-08"),
    ])
    for tag, value in {
        "IncreaseDecreaseInAccountsReceivable": -1_088e3,
        "IncreaseDecreaseInInventories": 12_848e3,
        "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities": 1_123e3,
        "IncreaseDecreaseInOtherOperatingAssets": -78e3,
        "IncreaseDecreaseInOtherOperatingLiabilities": 88e3,
    }.items():
        gaap[tag] = tagdata("USD", [
            dur("2025-01-01", "2025-12-31", value,
                accn="0001193125-26-213764", filed="2026-05-08"),
        ])

    item = build_snapshot(
        "EBF", "0000033002", facts_doc(gaap)).owner_earnings.annual[2025]
    # The rendered statement also has a separately printed $72k prepaid/tax
    # row that Company Facts omits, proving that five standard tags alone do
    # not establish completeness.
    assert item.working_capital_cash_effect is None


def test_buybacks_remain_visible_when_fcf_cannot_be_calculated():
    gaap = dict(OE_GAAP)
    gaap.pop("NetCashProvidedByUsedInOperatingActivities", None)
    gaap["PaymentsForRepurchaseOfCommonStock"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 7e9, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15")])

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(build(gaap))["annual_per_share"]["2025"][
        "cash_flow_bridge"]

    assert "operating_cash_flow" not in bridge
    assert "free_cash_flow" not in bridge
    assert bridge["share_repurchases"][0] == 7e9


def test_annual_cash_bridge_residual_absorbs_unseparated_working_capital():
    gaap = dict(OE_GAAP)
    gaap["ShareBasedCompensation"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 5e9,
            accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9,
            accn="k25", filed="2026-02-15"),
    ])

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(build(gaap))["annual_per_share"]["2025"][
        "cash_flow_bridge"]

    assert "working_capital_cash_effect" not in bridge
    # 75 OCF - 70 net income - 12 D&A - 5 SBC = -12 other, including
    # the working-capital effect that the filer did not publish as a rollup.
    assert bridge["other_operating_cash_flow_adjustments"] == -12e9


def test_nopat_roic_uses_normalized_tax_and_both_average_capital_views():
    gaap = dict(OE_GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 100e9, accn="k23", filed="2024-02-15"),
        dur("2024-01-01", "2024-12-31", 100e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 20e9, accn="k23", filed="2024-02-15"),
        dur("2024-01-01", "2024-12-31", 25e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 30e9, accn="k25", filed="2026-02-15"),
    ])
    oe = build(gaap).owner_earnings
    assert float(oe.normalized_tax_rate) == 0.25
    assert float(oe.nopat) == 75e9
    assert float(oe.nopat_roic) == pytest.approx(75 / 810 * 100)
    assert float(oe.nopat_return_including_cash) == pytest.approx(75 / 850 * 100)


def test_nopat_accepts_only_an_exactly_reconciled_operating_income_fallback():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}

    def annual(value, year):
        return dur(f"{year}-01-01", f"{year}-12-31", value,
                   accn=f"k{year}", filed=f"{year + 1}-02-15")

    gaap.update({
        "GrossProfit": tagdata("USD", [annual(600e9, y) for y in (2023, 2024, 2025)]),
        "SellingGeneralAndAdministrativeExpense": tagdata(
            "USD", [annual(500e9, y) for y in (2023, 2024, 2025)]),
        "InterestIncomeExpenseNonoperatingNet": tagdata(
            "USD", [annual(5e9, y) for y in (2023, 2024, 2025)]),
        "OtherNonoperatingIncomeExpense": tagdata(
            "USD", [annual(5e9, y) for y in (2023, 2024, 2025)]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            tagdata("USD", [annual(110e9, y) for y in (2023, 2024, 2025)]),
        "IncomeTaxExpenseBenefit": tagdata("USD", [
            annual(22e9, 2023), annual(27.5e9, 2024), annual(33e9, 2025),
        ]),
    })

    snapshot = build(gaap)
    operating = snapshot.annual_operating_income[2025]
    assert float(operating.value) == 100e9
    assert "derived and reconciled" in operating.provenance.concept
    from screener.sync import _source
    source = _source(operating)
    assert "derived and reconciled" in source["concept"]
    assert {part["tag"] for part in source["components"]} == {
        "us-gaap:GrossProfit",
        "us-gaap:SellingGeneralAndAdministrativeExpense",
        ("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
         "ExtraordinaryItemsNoncontrollingInterest"),
        "us-gaap:InterestIncomeExpenseNonoperatingNet",
        "us-gaap:OtherNonoperatingIncomeExpense",
    }
    assert float(snapshot.owner_earnings.nopat) == 75e9

    # The subtraction inputs alone are insufficient: break the independent
    # pretax reconciliation and the latest operating income/NOPAT disappear.
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata(
        "USD", [annual(110e9, 2023), annual(110e9, 2024), annual(111e9, 2025)])
    snapshot = build(gaap)
    assert 2025 not in snapshot.annual_operating_income
    assert snapshot.owner_earnings.nopat is None


def test_biotechne_comparative_operating_income_scale_is_reconciled():
    """Bio-Techne's FY2015 10-K prints its statement in thousands.

    The FY2013 OperatingIncomeLoss XBRL comparative lost that table scale in
    Company Facts and arrived as 158,469 dollars.  Every other same-accession
    statement input remained in dollars and independently proves 158,469,000:
    gross profit less operating expenses, then operating plus nonoperating income
    to pretax income.  Only that exact 1,000x contradiction may displace a direct
    standard-tag fact.
    """
    def annual(value):
        return dur("2012-07-01", "2013-06-30", value,
                   accn="0001437749-15-016645", filed="2015-08-31")

    gaap = {
        "OperatingIncomeLoss": tagdata("USD", [annual(158_469)]),
        "GrossProfit": tagdata("USD", [annual(231_110_000)]),
        "OperatingExpenses": tagdata("USD", [annual(72_641_000)]),
        "NonoperatingIncomeExpense": tagdata("USD", [annual(2_193_000)]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            tagdata("USD", [annual(160_662_000)]),
    }

    from screener.normalize import _annual_operating_income
    operating = _annual_operating_income(gaap)[2013]
    assert float(operating.value) == 158_469_000
    assert "exact 1000x presentation-scale contradiction" in (
        operating.provenance.concept)
    assert {part.tag for part in operating.provenance.components} == {
        "us-gaap:OperatingIncomeLoss",
        "us-gaap:GrossProfit",
        "us-gaap:OperatingExpenses",
        "us-gaap:NonoperatingIncomeExpense",
        ("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
         "ExtraordinaryItemsNoncontrollingInterest"),
    }

    # A normal restatement disagreement is not a units error.  Keep the direct
    # fact unless the contradiction is an exact presentation factor.
    gaap["OperatingIncomeLoss"] = tagdata("USD", [annual(150_000_000)])
    assert float(_annual_operating_income(gaap)[2013].value) == 150_000_000


def test_dolphin_hidden_three_dollar_gross_profit_is_withheld():
    """A hidden standard-tag fact must not outrank the statement's arithmetic.

    Dolphin's FY2022 10-K prints $40,505,558 revenue and $3,566,336 direct costs
    but no gross-profit row.  Company Facts nevertheless exposes GrossProfit=3.
    Missing is the only defensible result; multiplying three by a guessed report
    scale would invent a subtotal the company never presented.
    """
    def annual(value):
        return dur("2022-01-01", "2022-12-31", value,
                   accn="0001553350-23-000246", filed="2023-03-31")

    gaap = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": tagdata(
            "USD", [annual(40_505_558)]),
        "CostOfRevenue": tagdata("USD", [annual(3_566_336)]),
        "GrossProfit": tagdata("USD", [annual(3)]),
    }

    from screener.normalize import _annual_gross_profit
    assert 2022 not in _annual_gross_profit(gaap)

    # Small results are not rejected merely for being unusual.  If the three
    # same-filing rows satisfy the accounting identity, the filed subtotal stays.
    gaap["CostOfRevenue"] = tagdata("USD", [annual(40_505_555)])
    assert float(_annual_gross_profit(gaap)[2022].value) == 3


def test_dolphin_repeated_hidden_gross_profit_series_is_withheld():
    """A hard-coded subtotal repeated across moving statements is not history."""
    def annual(year, value):
        return dur(f"{year}-01-01", f"{year}-12-31", value,
                   accn=f"k{year}", filed=f"{year + 1}-03-31")

    gaap = {
        "Revenues": tagdata("USD", [
            annual(2020, 24_054_480), annual(2021, 35_727_199),
            annual(2022, 40_505_558),
        ]),
        "CostOfRevenue": tagdata("USD", [
            annual(2020, 2_576_709), annual(2021, 3_879_409),
            annual(2022, 3_566_336),
        ]),
        "GrossProfit": tagdata("USD", [
            annual(2020, 3_000_000), annual(2021, 3_000_000),
            annual(2022, 3_000_000),
        ]),
    }

    from screener.normalize import _annual_gross_profit
    assert _annual_gross_profit(gaap) == {}

    # Repetition alone is not a rejection: if revenue less cost supports the
    # same rounded subtotal in each filing, all three years remain reported.
    gaap["CostOfRevenue"] = tagdata("USD", [
        annual(2020, 21_054_480), annual(2021, 32_727_199),
        annual(2022, 37_505_558),
    ])
    assert set(_annual_gross_profit(gaap)) == {2020, 2021, 2022}


def test_jnj_operating_income_is_recovered_from_complete_nonoperating_bridge():
    """J&J's printed 10-K has no OperatingIncomeLoss subtotal.

    The separate interest and other-nonoperating rows recover the exact subtotal,
    while gross profit and the identified operating-cost stack independently guard
    the classification.  These are the FY2023-FY2025 values printed on page 44 of
    accession 0000200406-26-000016, in millions of dollars.
    """
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}

    def annual(value, year):
        starts = {2023: "2023-01-02", 2024: "2024-01-01", 2025: "2024-12-30"}
        ends = {2023: "2023-12-31", 2024: "2024-12-29", 2025: "2025-12-28"}
        return dur(starts[year], ends[year], value * 1e6,
                   accn="0000200406-26-000016", filed="2026-02-11")

    values = {
        "GrossProfit": {2023: 58_606, 2024: 61_350, 2025: 63_937},
        "SellingGeneralAndAdministrativeExpense": {
            2023: 21_512, 2024: 22_869, 2025: 23_676,
        },
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost": {
            2023: 15_085, 2024: 17_232, 2025: 14_665,
        },
        "InvestmentIncomeInterest": {2023: 1_261, 2024: 1_332, 2025: 1_056},
        "InterestExpenseNonoperating": {2023: 772, 2024: 755, 2025: 971},
        "OtherNonoperatingIncomeExpense": {
            2023: -6_634, 2024: -4_694, 2025: 7_209,
        },
        "RestructuringCharges": {2023: 489, 2024: 234, 2025: 228},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            2023: 15_062, 2024: 16_687, 2025: 32_581,
        },
    }
    for tag, by_year in values.items():
        gaap[tag] = tagdata("USD", [annual(value, year)
                                     for year, value in by_year.items()])

    snapshot = build(gaap)
    assert {year: float(fact.value) for year, fact
            in snapshot.annual_operating_income.items() if year >= 2023} == {
        2023: 21_207e6,
        2024: 20_804e6,
        2025: 25_287e6,
    }
    latest = snapshot.annual_operating_income[2025]
    assert "pretax less complete nonoperating lines" in latest.provenance.concept
    assert {part.tag for part in latest.provenance.components} == {
        "us-gaap:GrossProfit",
        "us-gaap:SellingGeneralAndAdministrativeExpense",
        "us-gaap:ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
        "us-gaap:InvestmentIncomeInterest",
        "us-gaap:InterestExpenseNonoperating",
        "us-gaap:OtherNonoperatingIncomeExpense",
        "us-gaap:RestructuringCharges",
        ("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
         "ExtraordinaryItemsNoncontrollingInterest"),
    }

    # The inverse bridge is intentionally all-or-nothing: losing one nonoperating
    # row must not turn an incomplete statement into invented operating income.
    del gaap["InvestmentIncomeInterest"]
    assert 2025 not in build(gaap).annual_operating_income


def test_inverse_operating_bridge_rejects_a_contradictory_cost_stack():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}

    def annual(value):
        return dur("2025-01-01", "2025-12-31", value,
                   accn="k25", filed="2026-02-15")

    gaap.update({
        "GrossProfit": tagdata("USD", [annual(100)]),
        "SellingGeneralAndAdministrativeExpense": tagdata("USD", [annual(40)]),
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost": tagdata(
            "USD", [annual(40)]),
        "InvestmentIncomeInterest": tagdata("USD", [annual(5)]),
        "InterestExpenseNonoperating": tagdata("USD", [annual(2)]),
        "OtherNonoperatingIncomeExpense": tagdata("USD", [annual(3)]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            tagdata("USD", [annual(24)]),
    })
    # The bridge implies operating income 18 and therefore only 82 of operating
    # costs.  Raise identified R&D above that total: the guard must reject it.
    gaap["ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"] = tagdata(
        "USD", [annual(50)])
    assert 2025 not in build(gaap).annual_operating_income


def test_note_only_generic_research_does_not_create_fstr_operating_income():
    """FSTR's FY2014 R&D disclosure is already included in SG&A.

    Its printed statement has a separate $4.695m amortization row and equity-method
    income outside operations.  Treating generic R&D as another statement expense
    happens to yield a tiny residual but invents $38.364m instead of the actual
    $37.082m operating subtotal, so this shape must stay absent.
    """
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}

    def annual(value):
        return dur("2014-01-01", "2014-12-31", value * 1e3,
                   accn="0001193125-17-074622", filed="2017-03-08")

    gaap.update({
        "GrossProfit": tagdata("USD", [annual(121_591)]),
        "SellingGeneralAndAdministrativeExpense": tagdata(
            "USD", [annual(79_814)]),
        "ResearchAndDevelopmentExpense": tagdata("USD", [annual(3_096)]),
        "InvestmentIncomeInterest": tagdata("USD", [annual(530)]),
        "InterestExpense": tagdata("USD", [annual(512)]),
        "OtherNonoperatingIncomeExpense": tagdata("USD", [annual(678)]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest":
            tagdata("USD", [annual(39_060)]),
    })

    assert 2014 not in build(gaap).annual_operating_income


def test_annual_nopat_is_independent_of_owner_earnings_cash_bridge():
    """Missing D&A/CapEx must not erase an otherwise evidenced NOPAT return."""
    gaap = {k: v for k, v in OE_GAAP.items()
            if k not in ("DepreciationDepletionAndAmortization",
                         "PaymentsToAcquirePropertyPlantAndEquipment")}
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata(
        "USD", [
            dur("2024-01-01", "2024-12-31", 80e9,
                accn="k24", filed="2025-02-15"),
            dur("2025-01-01", "2025-12-31", 100e9,
                accn="k25", filed="2026-02-15"),
        ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 16e9,
            accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 20e9,
            accn="k25", filed="2026-02-15"),
    ])

    snapshot = build(gaap)
    assert snapshot.owner_earnings is None
    annual = snapshot.annual_operating_returns[2025]
    assert float(annual.normalized_tax_rate) == pytest.approx(0.20)
    assert float(annual.nopat.value) == 80e9
    assert float(annual.invested_capital) == 810e9
    assert float(annual.nopat_roic) == pytest.approx(80 / 810 * 100)


def test_annual_nopat_uses_both_pretax_geographies_when_direct_tag_ends():
    """DECK-shaped domestic + foreign evidence is one consolidated denominator."""
    gaap = dict(OE_GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"] = tagdata(
        "USD", [dur("2025-01-01", "2025-12-31", 70e9,
                    accn="k25", filed="2026-02-15")])
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign"] = tagdata(
        "USD", [dur("2025-01-01", "2025-12-31", 30e9,
                    accn="k25", filed="2026-02-15")])

    annual = build(gaap).annual_operating_returns[2025]
    assert float(annual.normalized_tax_rate) == pytest.approx(0.20)
    assert float(annual.nopat.value) == 80e9
    assert len(annual.pretax_income_inputs) == 1
    assert "Domestic + us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign" in (
        annual.pretax_income_inputs[0].provenance.tag)


def test_ten_year_operating_return_history_discloses_each_zero_assumption():
    """The UI history gets ten real calculations, not ten repeated latest values."""
    from screener.sync import _derive

    fiscal_years = range(2016, 2026)
    balance_years = range(2015, 2026)
    gaap = {k: v for k, v in OE_GAAP.items()
            if k not in ("DepreciationDepletionAndAmortization",
                         "PaymentsToAcquirePropertyPlantAndEquipment",
                         "Goodwill", "IntangibleAssetsNetExcludingGoodwill",
                         "ShortTermInvestments")}
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur(f"{year}-01-01", f"{year}-12-31", 5,
            accn=f"k{year}", filed=f"{year + 1}-02-15")
        for year in fiscal_years
    ])
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur(f"{year}-01-01", f"{year}-12-31", year * 1e6,
            accn=f"k{year}", filed=f"{year + 1}-02-15")
        for year in fiscal_years
    ])
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata(
        "USD", [
            dur(f"{year}-01-01", f"{year}-12-31", year * 1e6,
                accn=f"k{year}", filed=f"{year + 1}-02-15")
            for year in fiscal_years
        ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur(f"{year}-01-01", f"{year}-12-31", year * 0.2e6,
            accn=f"k{year}", filed=f"{year + 1}-02-15")
        for year in fiscal_years
    ])
    for tag, value in (
        ("Assets", 1000e9),
        ("LiabilitiesCurrent", 150e9),
        ("DebtCurrent", 0),
        ("CashAndCashEquivalentsAtCarryingValue", 40e9),
    ):
        gaap[tag] = tagdata("USD", [
            inst(f"{year}-12-31", value, form="10-K",
                 accn=f"k{year}", filed=f"{year + 1}-02-15")
            for year in balance_years
        ])

    strict = build_snapshot("TEST", "0000000001", facts_doc(gaap))
    assert strict.annual_operating_returns[2025].nopat_roic is None
    assert strict.annual_operating_returns[2025].ronta is None
    conservative = strict.conservative_operating_returns[2025]
    assert conservative.conservative_estimate is True
    assert conservative.nopat_roic is not None
    assert conservative.ronta is not None
    assert set(conservative.assumed_zero) == {
        "goodwill", "intangibles", "noncurrent_investments",
        "short_term_investments",
    }
    assert any("lower-bound return estimate" in note
               for note in conservative.caveats)

    status, strict_row = _derive("0000000001", "TEST", facts_doc(gaap))
    assert status == "ok"
    estimate = strict_row["operating_returns_estimate"]
    assert estimate["status"] == "CONSERVATIVE_LOWER_BOUND"
    assert estimate["conservative_estimate"] is True
    assert estimate["nopat_roic"] == pytest.approx(
        float(conservative.nopat_roic))
    assert estimate["ronta"] == pytest.approx(float(conservative.ronta))
    assert strict_row["operating_returns"].get("nopat_roic") is None
    assert strict_row["operating_returns"].get("ronta") is None

    assumed = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert len(assumed.annual_operating_returns) == 10
    for year, annual in assumed.annual_operating_returns.items():
        assert annual.nopat is not None, year
        assert annual.nopat_roic is not None, year
        assert annual.nopat_return_including_cash is not None, year
        assert annual.ronta is not None, year
        assert set(annual.assumed_zero) == {
            "goodwill", "intangibles", "noncurrent_investments",
            "short_term_investments",
        }

    status, row = _derive(
        "0000000001", "TEST", facts_doc(gaap), assume_absent_zero=True)
    assert status == "ok"
    history = row["annual_ratios"]
    assert sorted(history) == list(fiscal_years)
    assert all(history[year]["nopat"] is not None for year in fiscal_years)
    assert all(history[year]["nopat_roic"] is not None for year in fiscal_years)
    assert all(history[year]["ronta"] is not None for year in fiscal_years)
    assert history[2025]["operating_return_evidence"]["operating_income"][0][2] == "k2025"
    assert row["operating_returns"]["nopat"] == history[2025]["nopat"]
    assert row["operating_returns"]["ronta"] == history[2025]["ronta"]


def test_detail_zero_mode_fills_every_missing_operating_return_input():
    """The explicit detail mode leaves no dash merely because an input is absent."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 1,
                accn="k25", filed="2026-02-15"),
        ]),
    }

    strict = build_snapshot("TEST", "0000000001", facts_doc(gaap))
    assert strict.annual_operating_returns[2025].nopat is None
    assert strict.conservative_operating_returns[2025].nopat is None
    assert strict.conservative_operating_returns[2025].nopat_roic is None

    assumed = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert sorted(assumed.annual_operating_returns) == list(range(2016, 2026))
    for year, annual in assumed.annual_operating_returns.items():
        assert annual.operating_income.value == 0, year
        assert annual.normalized_tax_rate == 0, year
        assert annual.nopat.value == 0, year
        assert annual.invested_capital == 0, year
        assert annual.capital_including_cash == 0, year
        assert annual.average_net_tangible_operating_assets == 0, year
        assert annual.nopat_roic == 0, year
        assert annual.nopat_return_including_cash == 0, year
        assert annual.ronta is None, year
        assert {
            "operating_income", "tax_rate", "total_assets",
            "current_liabilities", "cash", "short_term_debt",
            "short_term_investments", "goodwill", "intangibles",
            "noncurrent_investments",
        } <= set(annual.assumed_zero)
        assert any("was not reported" in note for note in annual.caveats)


def test_negative_nopat_is_not_published_as_a_lower_bound():
    """A larger denominator lowers a positive return but raises a negative one."""
    from screener.sync import _derive

    gaap = {k: v for k, v in OE_GAAP.items()
            if k not in ("Goodwill", "IntangibleAssetsNetExcludingGoodwill",
                         "ShortTermInvestments")}
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", -10e9,
            accn="k25", filed="2026-02-15"),
    ])

    status, row = _derive("0000000001", "TEST", facts_doc(gaap))
    assert status == "ok"
    assert row["operating_returns_estimate"] is None


def test_denominator_floor_blocks_partial_estimate_from_discovery_payload():
    """A spectacular RONTA alone must not bypass a correctly withheld ROIC."""
    from screener.sync import _derive

    gaap = {k: v for k, v in OE_GAAP.items()
            if k not in ("Goodwill", "IntangibleAssetsNetExcludingGoodwill",
                         "ShortTermInvestments")}
    gaap["CashAndCashEquivalentsAtCarryingValue"] = tagdata("USD", [
        inst("2024-12-31", 840e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 840e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 840e9, accn="q126"),
    ])
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata(
        "USD", [dur("2025-01-01", "2025-12-31", 100e9,
                    accn="k25", filed="2026-02-15")])
    snapshot = build_snapshot("TEST", "0000000001", facts_doc(gaap))
    candidate = snapshot.conservative_operating_returns[2025]
    assert candidate.nopat_roic is None
    assert candidate.ronta is not None
    assert any("implausibly small" in caveat for caveat in candidate.caveats)

    status, row = _derive("0000000001", "TEST", facts_doc(gaap))
    assert status == "ok"
    assert row["operating_returns_estimate"] is None


def test_detail_zero_mode_creates_ten_slots_without_annual_filing_history():
    gaap = {
        "Assets": tagdata("USD", [inst("2026-06-30", 100e6)]),
        "AssetsCurrent": tagdata("USD", [inst("2026-06-30", 60e6)]),
        "LiabilitiesCurrent": tagdata("USD", [inst("2026-06-30", 20e6)]),
    }

    snapshot = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)

    assert sorted(snapshot.annual_operating_returns) == list(range(2017, 2027))
    assert all(item.nopat.value == 0
               for item in snapshot.annual_operating_returns.values())
    assert all(item.nopat_roic == 0
               for item in snapshot.annual_operating_returns.values())
    assert all(item.ronta is None
               for item in snapshot.annual_operating_returns.values())

    no_anchor = build_snapshot(
        "TEST", "0000000001", facts_doc({}), assume_absent_zero=True)
    assert len(no_anchor.annual_operating_returns) == 10
    assert all(item.nopat.value == 0
               for item in no_anchor.annual_operating_returns.values())


def test_ronta_uses_nopat_over_exact_average_net_tangible_operating_assets():
    gaap = dict(OE_GAAP)
    gaap.update({
        "Goodwill": tagdata("USD", [
            inst("2024-12-31", 50e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 60e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "IntangibleAssetsNetExcludingGoodwill": tagdata("USD", [
            inst("2024-12-31", 20e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 30e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "OtherLongTermInvestments": tagdata("USD", [
            inst("2024-12-31", 40e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 50e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": tagdata("USD", [
            dur("2023-01-01", "2023-12-31", 100e9, accn="k23", filed="2024-02-15"),
            dur("2024-01-01", "2024-12-31", 100e9, accn="k24", filed="2025-02-15"),
            dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
        ]),
        "IncomeTaxExpenseBenefit": tagdata("USD", [
            dur("2023-01-01", "2023-12-31", 20e9, accn="k23", filed="2024-02-15"),
            dur("2024-01-01", "2024-12-31", 25e9, accn="k24", filed="2025-02-15"),
            dur("2025-01-01", "2025-12-31", 30e9, accn="k25", filed="2026-02-15"),
        ]),
    })
    oe = build(gaap).owner_earnings

    # Each endpoint starts from assets - NIB current liabilities - cash -
    # short-term investments, then removes goodwill, other intangibles, and
    # noncurrent investments: 700 at the beginning and 670 at the end.
    assert float(oe.net_tangible_operating_assets_beginning.value) == 700e9
    assert float(oe.net_tangible_operating_assets_ending.value) == 670e9
    assert float(oe.average_net_tangible_operating_assets) == 685e9
    assert float(oe.nopat) == 75e9
    assert float(oe.ronta) == pytest.approx(75 / 685 * 100)
    assert "OtherLongTermInvestments" in (
        oe.net_tangible_operating_assets_ending.provenance.tag)

    from screener.sync import _owner_earnings_row
    payload = _owner_earnings_row(build(gaap))
    assert payload["average_net_tangible_operating_assets"] == 685e9
    assert payload["ronta"] == pytest.approx(75 / 685 * 100)
    assert payload["net_tangible_operating_assets_evidence"]["beginning"]["value"] == 700e9
    assert payload["net_tangible_operating_assets_evidence"]["ending"]["value"] == 670e9


def test_ronta_treats_lease_liabilities_as_financing_and_exposes_neutral_view():
    gaap = dict(OE_GAAP)
    gaap.update({
        "Goodwill": tagdata("USD", [
            inst("2024-12-31", 50e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 60e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "IntangibleAssetsNetExcludingGoodwill": tagdata("USD", [
            inst("2024-12-31", 20e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 30e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "OtherLongTermInvestments": tagdata("USD", [
            inst("2024-12-31", 40e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 50e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "OperatingLeaseLiabilityCurrent": tagdata("USD", [
            inst("2024-12-31", 20e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 25e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "OperatingLeaseRightOfUseAsset": tagdata("USD", [
            inst("2024-12-31", 100e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 120e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": tagdata(
            "USD", [
                dur("2023-01-01", "2023-12-31", 100e9, accn="k23", filed="2024-02-15"),
                dur("2024-01-01", "2024-12-31", 100e9, accn="k24", filed="2025-02-15"),
                dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
            ]),
        "IncomeTaxExpenseBenefit": tagdata("USD", [
            dur("2023-01-01", "2023-12-31", 20e9, accn="k23", filed="2024-02-15"),
            dur("2024-01-01", "2024-12-31", 25e9, accn="k24", filed="2025-02-15"),
            dur("2025-01-01", "2025-12-31", 30e9, accn="k25", filed="2026-02-15"),
        ]),
    })

    snapshot = build_snapshot("TEST", "0000000001", facts_doc(gaap))
    annual = snapshot.annual_operating_returns[2025]

    # The old NTOA endpoints were 700 and 670. Keeping the current lease
    # liability with financing makes them 720 and 695. Removing the ROU assets
    # for presentation comparability then makes them 620 and 575.
    assert float(annual.net_tangible_operating_assets_beginning.value) == 720e9
    assert float(annual.net_tangible_operating_assets_ending.value) == 695e9
    assert float(annual.average_net_tangible_operating_assets) == 707.5e9
    assert float(annual.lease_neutral_net_tangible_operating_assets_beginning.value) == 620e9
    assert float(annual.lease_neutral_net_tangible_operating_assets_ending.value) == 575e9
    assert float(annual.average_lease_neutral_net_tangible_operating_assets) == 597.5e9
    assert float(annual.ronta) == pytest.approx(75 / 707.5 * 100)
    assert float(annual.lease_neutral_ronta) == pytest.approx(75 / 597.5 * 100)
    assert "OperatingLeaseLiabilityCurrent" in (
        annual.net_tangible_operating_assets_ending.provenance.tag)
    assert "OperatingLeaseRightOfUseAsset" in (
        annual.lease_neutral_net_tangible_operating_assets_ending.provenance.tag)

    # A JNJ-shaped missing current lease split is not a safe zero. It must keep
    # RONTA blank even in the conservative discovery layer.
    del gaap["OperatingLeaseLiabilityCurrent"]
    without_current_lease = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap))
    assert without_current_lease.conservative_operating_returns[2025].ronta is None
    assert without_current_lease.conservative_operating_returns[2025].lease_neutral_ronta is None

    from screener.sync import _operating_return_history
    cell = _operating_return_history(snapshot)[2025]
    assert cell["average_net_tangible_operating_assets"] == 707.5e9
    assert cell["average_lease_neutral_net_tangible_operating_assets"] == 597.5e9
    assert cell["lease_neutral_ronta"] == pytest.approx(75 / 597.5 * 100)


def test_current_operating_lease_liability_can_be_derived_from_exact_total():
    exact = {
        "OperatingLeaseLiability": tagdata("USD", [
            inst("2025-12-31", 120e9, form="10-K", accn="k25",
                 filed="2026-02-15"),
        ]),
        "OperatingLeaseLiabilityNoncurrent": tagdata("USD", [
            inst("2025-12-31", 95e9, form="10-K", accn="k25",
                 filed="2026-02-15"),
        ]),
    }

    current = _current_operating_lease_liability(
        exact, date(2025, 12, 31))

    assert current.value == Decimal("25000000000")
    assert current.provenance.tag == (
        "us-gaap:OperatingLeaseLiability - "
        "us-gaap:OperatingLeaseLiabilityNoncurrent")
    assert len(current.provenance.components) == 2


def test_nonpositive_average_ntoa_makes_ronta_not_meaningful_in_detail_mode():
    gaap = dict(OE_GAAP)
    gaap.update({
        "Goodwill": tagdata("USD", [
            inst("2024-12-31", 900e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 900e9, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "IntangibleAssetsNetExcludingGoodwill": tagdata("USD", [
            inst("2024-12-31", 0, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "OtherLongTermInvestments": tagdata("USD", [
            inst("2024-12-31", 0, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
    })

    snapshot = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    annual = snapshot.annual_operating_returns[2025]
    assert annual.average_net_tangible_operating_assets == Decimal("-90000000000")
    assert annual.nopat is not None
    assert annual.ronta is None
    assert any("displays N/M" in caveat for caveat in annual.caveats)

    from screener.sync import _derive
    status, row = _derive(
        "0000000001", "TEST", facts_doc(gaap), assume_absent_zero=True)
    assert status == "ok"
    cell = row["annual_ratios"][2025]
    assert "ronta" not in cell
    assert "not greater than 0" in cell["ronta_undefined"]


def test_ronta_does_not_assume_an_unreported_noncurrent_investment_is_zero():
    gaap = dict(OE_GAAP)
    gaap["Goodwill"] = tagdata("USD", [
        inst("2024-12-31", 0, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-02-15"),
    ])
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2024-12-31", 0, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-02-15"),
    ])
    oe = build(gaap).owner_earnings

    assert oe.average_net_tangible_operating_assets is None
    assert oe.ronta is None
    assert any("noncurrent investments" in caveat and "RONTA" in caveat
               for caveat in oe.caveats)


def test_nopat_tax_normalization_rejects_misaligned_fiscal_periods():
    gaap = dict(OE_GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 100e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-30", 25e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 25e9, accn="k25", filed="2026-02-15"),
    ])
    oe = build(gaap).owner_earnings
    assert oe.normalized_tax_rate is None
    assert oe.nopat is None and oe.nopat_roic is None
    assert any("fewer than two aligned" in caveat and "NOPAT" in caveat
               for caveat in oe.caveats)


def test_epd_shape_keeps_owner_estimates_distinct_from_standard_fcf():
    """EPD proves why total capex and maintenance capex cannot be conflated."""
    gaap = dict(OE_GAAP)
    values = {
        "NetIncomeLoss": 5.810e9,
        "OperatingIncomeLoss": 7.266e9,  # must not become the owner numerator
        "DepreciationDepletionAndAmortization": 2.303e9,
        "PaymentsToAcquirePropertyPlantAndEquipment": 5.620e9,
        "NetCashProvidedByUsedInOperatingActivities": 8.585e9,
    }
    for tag, value in values.items():
        gaap[tag] = tagdata("USD", [
            dur("2025-01-01", "2025-12-31", value, accn="k25", filed="2026-02-15")])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 2.185e9,
            accn="k25", filed="2026-02-15")])

    snapshot = build(gaap)
    oe = snapshot.owner_earnings
    assert float(oe.all_capex_floor.value) == pytest.approx(2.493e9)
    assert float(oe.maintenance_estimate.value) == pytest.approx(5.810e9)
    assert float(oe.free_cash_flow.value) == pytest.approx(2.965e9)

    from screener.sync import _owner_earnings_row
    payload = _owner_earnings_row(snapshot)
    assert payload["status"] == "ESTIMATE_ONLY"
    assert payload["owner_earnings"] is None and payload["roic"] is None
    assert payload["maintenance_basis"] == "UNAVAILABLE_PRIMARY_XBRL"


def test_hon_shape_starts_with_net_income_not_operating_income():
    gaap = dict(OE_GAAP)
    for tag, value in {
        "NetIncomeLoss": 4.729e9,
        "OperatingIncomeLoss": 7.521e9,
        "DepreciationDepletionAndAmortization": 2.000e9,
        "PaymentsToAcquirePropertyPlantAndEquipment": 1.598e9,
    }.items():
        gaap[tag] = tagdata("USD", [
            dur("2025-01-01", "2025-12-31", value, accn="k25", filed="2026-02-15")])

    oe = build(gaap).owner_earnings
    assert float(oe.all_capex_floor.value) == pytest.approx(5.131e9)
    assert dict(oe.components)["reported earnings available to common"] == Decimal("4729000000.0")


def test_owner_earnings_per_share_keeps_each_complete_audited_year():
    gaap = dict(OE_GAAP)
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 80e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 64e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 80e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["DepreciationDepletionAndAmortization"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 10e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 16e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 20e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 8e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2024-01-01", "2024-12-31", 10e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15"),
    ])

    snapshot = build(gaap)
    annual = snapshot.owner_earnings.annual

    assert sorted(annual) == [2024, 2025]
    assert float(annual[2024].all_capex_floor.value) == 66e9
    assert float(annual[2024].all_capex_floor_per_share.value) == 6.6
    assert float(annual[2025].maintenance_estimate_per_share.value) == 8.0
    assert len(annual[2025].all_capex_floor.provenance.components) == 3
    assert len(annual[2025].maintenance_estimate_per_share.provenance.components) == 2

    from screener.sync import _owner_earnings_row
    payload = _owner_earnings_row(snapshot)
    cell = payload["annual_per_share"]["2024"]
    assert cell["all_capex_floor_per_share"] == 6.6
    assert cell["earnings_after_total_capex_per_share"] == 6.6
    assert cell["maintenance_estimate_per_share"] == 6.4
    assert cell["reported_earnings_assumption_per_share"] == 6.4
    assert cell["free_cash_flow_per_share"] is None
    assert cell["diluted_shares"] == 10e9
    assert cell["end"] == "2024-12-31"
    assert payload["sources"]["reported_earnings"]["accn"] == "k25"
    assert payload["sources"]["total_capex"]["tag"].endswith(
        "PaymentsToAcquirePropertyPlantAndEquipment")


def test_owner_earnings_keeps_an_eleventh_year_for_the_averaged_cagr_basis():
    from screener.sync import _owner_earnings_row

    gaap = dict(OE_GAAP)
    years = range(2015, 2026)

    def annual(tag, unit, value):
        gaap[tag] = tagdata(unit, [
            dur(f"{year}-01-01", f"{year}-12-31", value(year),
                accn=f"k{year}", filed=f"{year + 1}-02-15")
            for year in years
        ])

    annual("NetIncomeLoss", "USD", lambda year: (year - 2010) * 1e9)
    annual("DepreciationDepletionAndAmortization", "USD", lambda year: 1e9)
    annual("PaymentsToAcquirePropertyPlantAndEquipment", "USD", lambda year: 1e9)
    annual("NetCashProvidedByUsedInOperatingActivities", "USD",
           lambda year: (year - 2009) * 1e9)
    annual("WeightedAverageNumberOfDilutedSharesOutstanding", "shares",
           lambda year: 1e9)

    snapshot = build(gaap)
    payload = _owner_earnings_row(snapshot)

    assert sorted(snapshot.owner_earnings.annual) == list(years)
    assert list(payload["annual_per_share"]) == [str(year) for year in years]
    assert payload["annual_per_share"]["2015"][
        "reported_earnings_assumption_per_share"] == 5


def test_owner_earnings_retains_proved_same_period_da_scale_fusb_pattern():
    """A later comparative cannot turn $1.6m of D&A into $1.60.

    FUSB's FY2023 fact is independently present in its earlier 10-K, while both
    adjacent years corroborate that dollar scale.  Keep the reported earlier
    fact and disclose the rejected later comparative; do not manufacture a value
    from company size or from the neighbours themselves.
    """
    gaap = dict(OE_GAAP)

    def years(values, tag):
        gaap[tag] = tagdata("USD", [
            dur(f"{year}-01-01", f"{year}-12-31", value,
                accn=f"k{year}", filed=f"{year + 1}-03-15")
            for year, value in values.items()
        ])

    years({2022: 8_000_000, 2023: 8_500_000,
           2024: 9_000_000, 2025: 9_500_000}, "NetIncomeLoss")
    years({2022: 1_000_000, 2023: 1_100_000,
           2024: 1_200_000, 2025: 1_300_000},
          "PaymentsToAcquirePropertyPlantAndEquipment")
    years({2022: 10_000_000, 2023: 11_000_000,
           2024: 12_000_000, 2025: 13_000_000},
          "NetCashProvidedByUsedInOperatingActivities")
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur(f"{year}-01-01", f"{year}-12-31", 6_000_000,
            accn=f"k{year}", filed=f"{year + 1}-03-15")
        for year in range(2022, 2026)
    ])
    gaap["DepreciationDepletionAndAmortization"] = tagdata("USD", [
        dur("2022-01-01", "2022-12-31", 1_614_000,
            accn="k22", filed="2023-03-15"),
        dur("2023-01-01", "2023-12-31", 1_600_000,
            accn="k23", filed="2024-03-15"),
        dur("2023-01-01", "2023-12-31", Decimal("1.6"),
            accn="k24-bad-comparative", filed="2025-03-15"),
        dur("2024-01-01", "2024-12-31", 1_600_000,
            accn="k25", filed="2026-03-15"),
        dur("2025-01-01", "2025-12-31", 1_700_000,
            accn="k25", filed="2026-03-15"),
    ])

    snapshot = build(gaap)
    repaired = snapshot.owner_earnings.annual[2023].depreciation_and_amortisation

    assert repaired.value == Decimal("1600000")
    assert repaired.provenance.accession == "k23"
    assert "exact 1000000x presentation-scale contradiction" in (
        repaired.provenance.concept)
    assert {source.accession for source in repaired.provenance.components} == {
        "k22", "k23", "k24-bad-comparative", "k25",
    }
    assert any("FY2023 (1000000x)" in caveat
               for caveat in snapshot.owner_earnings.caveats)

    from screener.sync import _owner_earnings_row
    bridge = _owner_earnings_row(snapshot)["annual_per_share"]["2023"][
        "cash_flow_bridge"]
    assert bridge["depreciation_and_amortisation"][:5] == [
        1_600_000.0, "us-gaap:DepreciationDepletionAndAmortization",
        "10-K", "k23", "2023-12-31",
    ]

    extension_tag = (
        "DepreciationAndAmortizationOfPropertyPlantAndEquipmentAndComputerPrograms")
    sidecar = {"facts": {"ext:fusb/2024": {extension_tag: tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 1_581_000,
            accn="k24-bad-comparative", filed="2025-03-15"),
    ])}}}
    exact = build_snapshot(
        "FUSB", "0000717806", facts_doc(gaap), dimensioned=sidecar)
    exact_da = exact.owner_earnings.annual[2023].depreciation_and_amortisation
    assert exact_da.value == Decimal("1581000")
    assert exact_da.provenance.accession == "k24-bad-comparative"
    assert exact_da.provenance.tag == f"ext:fusb/2024:{extension_tag}"
    assert not any("FY2023 (1000000x)" in caveat
                   for caveat in exact.owner_earnings.caveats)


def test_da_scale_guard_preserves_normal_restatements_and_unproved_endpoints():
    from screener.normalize import (_annual_union,
                                    _reconcile_depreciation_scale)

    tag = "DepreciationAndAmortization"
    ordinary = {tag: tagdata("USD", [
        dur("2022-01-01", "2022-12-31", 1_500_000,
            accn="k22", filed="2023-03-15"),
        dur("2023-01-01", "2023-12-31", 1_600_000,
            accn="k23", filed="2024-03-15"),
        dur("2023-01-01", "2023-12-31", 1_550_000,
            accn="k24-restated", filed="2025-03-15"),
        dur("2024-01-01", "2024-12-31", 1_700_000,
            accn="k24", filed="2025-03-15"),
    ])}
    series = _annual_union(ordinary, (tag,))
    reconciled, repairs = _reconcile_depreciation_scale(ordinary, series)
    assert reconciled[2023].value == Decimal("1550000")
    assert reconciled[2023].provenance.accession == "k24-restated"
    assert repairs == []

    endpoint = {tag: tagdata("USD", [
        dur("2023-01-01", "2023-12-31", 1_600_000,
            accn="k23", filed="2024-03-15"),
        dur("2023-01-01", "2023-12-31", Decimal("1.6"),
            accn="k24-bad", filed="2025-03-15"),
        dur("2024-01-01", "2024-12-31", 1_700_000,
            accn="k24", filed="2025-03-15"),
    ])}
    series = _annual_union(endpoint, (tag,))
    reconciled, repairs = _reconcile_depreciation_scale(endpoint, series)
    assert reconciled[2023].value == Decimal("1.6")
    assert repairs == []


def test_owner_earnings_per_share_does_not_fill_a_missing_component():
    gaap = dict(OE_GAAP)
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 80e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 100e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["DepreciationDepletionAndAmortization"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 10e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 16e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 20e9, accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2024-01-01", "2024-12-31", 10e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15"),
    ])

    # The base fixture has capex only for FY2025. FY2024 must remain absent.
    assert sorted(build(gaap).owner_earnings.annual) == [2025]


def test_owner_earnings_per_share_restates_onto_the_traded_receipt():
    gaap = dict(OE_GAAP)
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2025-01-01", "2025-12-31", 10e9, accn="k25", filed="2026-02-15"),
    ])
    facts = facts_doc(gaap)
    ordinary = build_snapshot("ADR", "0000000001", facts)
    receipt = build_snapshot(
        "ADR", "0000000001", facts,
        receipt={"ratio": "2", "accn": "cover-1",
                 "title": "ADS, each representing 2 shares"},
    )

    ordinary_year = ordinary.owner_earnings.annual[2025]
    receipt_year = receipt.owner_earnings.annual[2025]
    assert receipt_year.all_capex_floor.value == ordinary_year.all_capex_floor.value
    assert receipt_year.diluted_shares.value == ordinary_year.diluted_shares.value / 2
    assert (receipt_year.maintenance_estimate_per_share.value
            == ordinary_year.maintenance_estimate_per_share.value * 2)


def test_owner_earnings_old_share_denominators_are_rebased_for_later_splits():
    gaap = dict(OE_GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        dur("2020-01-01", "2020-12-31", 10, accn="k20", filed="2021-02-15"),
        dur("2023-01-01", "2023-12-31", 10, accn="k23", filed="2024-02-15"),
        dur("2023-01-01", "2023-12-31", 5, accn="k24", filed="2025-02-15"),
        dur("2024-01-01", "2024-12-31", 6, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 7, accn="k25", filed="2026-02-15"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        dur("2020-01-01", "2020-12-31", 10e9, accn="k20", filed="2021-02-15"),
        dur("2023-01-01", "2023-12-31", 10e9, accn="k23", filed="2024-02-15"),
        dur("2023-01-01", "2023-12-31", 20e9, accn="k24", filed="2025-02-15"),
        dur("2024-01-01", "2024-12-31", 20e9, accn="k24", filed="2025-02-15"),
        dur("2025-01-01", "2025-12-31", 20e9, accn="k25", filed="2026-02-15"),
    ])
    for tag, value in (
        ("NetIncomeLoss", 100e9),
        ("DepreciationDepletionAndAmortization", 0),
        ("PaymentsToAcquirePropertyPlantAndEquipment", 0),
    ):
        gaap[tag] = tagdata("USD", [
            dur("2020-01-01", "2020-12-31", value, accn="k20", filed="2021-02-15"),
            dur("2025-01-01", "2025-12-31", value, accn="k25", filed="2026-02-15"),
        ])

    old = build(gaap).owner_earnings.annual[2020]

    assert old.diluted_shares.value == Decimal("20e9")
    assert old.maintenance_estimate_per_share.value == Decimal("5")
    assert "later split" in old.diluted_shares.provenance.concept


def test_proved_table_scale_is_repaired_before_split_rebasing():
    """FIZZ pattern: FY2018 is tagged as 46,921 shares before a 2:1 split,
    between 92M and 94M split-adjusted years. Filing arithmetic and adjacent
    counts prove the exact 1,000x unit for the public annual series before owner
    earnings applies the later 2:1 split to its comparable denominator."""
    gaap = dict(OE_GAAP)
    gaap["EarningsPerShareDiluted"] = tagdata("USD/shares", [
        _yr(2017, 2, "2018-02-15", "k17"),
        _yr(2017, 1, "2020-02-15", "k19"),
        _yr(2018, 2.2, "2019-02-15", "k18"),
        _yr(2018, 1.1, "2020-02-15", "k19"),
        _yr(2019, 1.2, "2020-02-15", "k19"),
        _yr(2025, 1.4, "2026-02-15", "k25"),
    ])
    gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = tagdata("shares", [
        _shares(2017, 46e6, "2018-02-15", "k17"),
        _shares(2017, 92e6, "2020-02-15", "k19"),
        _shares(2018, 46921, "2019-02-15", "k18"),
        _shares(2019, 94e6, "2020-02-15", "k19"),
        _shares(2025, 96e6, "2026-02-15", "k25"),
    ])
    for tag, value in (
        ("NetIncomeLoss", 100e6),
        ("DepreciationDepletionAndAmortization", 0),
        ("PaymentsToAcquirePropertyPlantAndEquipment", 0),
    ):
        gaap[tag] = tagdata("USD", [
            _yr(year, value, f"{year + 1}-02-15", f"k{str(year)[-2:]}")
            for year in (2017, 2018, 2019, 2025)
        ])

    snapshot = build(gaap)
    year = snapshot.owner_earnings.annual[2018]

    assert snapshot.annual_share_counts[2018].value == Decimal("46921000")
    assert "scaled 1000x" in snapshot.annual_share_counts[2018].provenance.concept
    assert year.diluted_shares.value == Decimal("93842000")
    assert round(float(year.maintenance_estimate_per_share.value), 6) == round(100e6 / 93842000, 6)
    assert "later split" in year.diluted_shares.provenance.concept
    assert not any("FY2018 (1000x)" in caveat for caveat in snapshot.owner_earnings.caveats)


def test_interest_bearing_current_debt_stays_in_invested_capital():
    """Only what suppliers and employees fund is netted off; borrowed money is capital."""
    gaap = {**OE_GAAP, "DebtCurrent": tagdata("USD", [
        inst("2024-12-31", 50e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 50e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 50e9, accn="q126"),
    ])}
    s = build(gaap)
    # current liabilities 150 less 50 of debt leaves 100 of non-interest-bearing funding
    assert float(s.owner_earnings.invested_capital) == 860e9


def test_no_classified_balance_sheet_yields_earnings_without_a_return():
    """Banks report no current liabilities, so invested capital cannot be separated —
    the earnings still compute, the ratio does not."""
    gaap = {k: v for k, v in OE_GAAP.items() if k != "LiabilitiesCurrent"}
    oe = build(gaap).owner_earnings
    assert float(oe.all_capex_floor.value) == 70e9
    assert oe.invested_capital is None and oe.all_capex_return is None


def test_bvps_keeps_intangibles_that_tbvps_removes():
    from screener.sync import _bvps, _tbvps
    s = build()
    # assets 1000 - liabilities 400 = 600 over 10B shares
    assert _bvps(s) == 60.0
    # tangible additionally sheds goodwill 50 and intangibles 30
    assert _tbvps(s) == 52.0


def test_historical_tbv_never_turns_a_missing_deduction_into_zero():
    """AEE files annual goodwill but no intangible-assets fact. Current P/TBV is
    therefore unavailable; the historical table must not quietly subtract zero
    and show six apparently comparable P/TBV values anyway."""
    gaap = {
        "EarningsPerShareDiluted": tagdata("USD/shares", [
            dur("2025-01-01", "2025-12-31", 6, accn="k25", filed="2026-02-15")]),
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 60, accn="k25", filed="2026-02-15")]),
        "WeightedAverageNumberOfDilutedSharesOutstanding": tagdata("shares", [
            dur("2025-01-01", "2025-12-31", 10, accn="k25", filed="2026-02-15")]),
        "Assets": tagdata("USD", [
            inst("2025-12-31", 1000, form="10-K", accn="k25", filed="2026-02-15")]),
        "Liabilities": tagdata("USD", [
            inst("2025-12-31", 400, form="10-K", accn="k25", filed="2026-02-15")]),
        "Goodwill": tagdata("USD", [
            inst("2025-12-31", 50, form="10-K", accn="k25", filed="2026-02-15")]),
    }
    from screener.normalize import (
        _annual_eps, _annual_net_income, annual_ratios,
    )
    eps, income = _annual_eps(gaap), _annual_net_income(gaap)
    ratios = annual_ratios(gaap, income, {}, {}, annual_eps=eps)

    assert ratios[2025]["bvps"] == 60
    assert "tbvps" not in ratios[2025]
    assert "return_on_net_tangible_assets" not in ratios[2025]

    # An explicit zero is evidence and restores the calculation; None is never
    # treated as that zero implicitly.
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-02-15")])
    ratios = annual_ratios(gaap, income, {}, {}, annual_eps=eps)
    assert ratios[2025]["tbvps"] == 55
    assert ratios[2025]["return_on_net_tangible_assets"] == round(60 / 550 * 100, 4)


def test_historical_book_uses_the_snapshot_selected_share_class():
    """A dimensioned weighted count is already filing-backed and class-matched by
    the snapshot. Recomputing from Company Facts here loses that axis and blanks
    Berkshire's historical P/B and P/TBV."""
    from screener.normalize import (
        _annual_eps, _annual_net_income, _annual_share_counts, annual_ratios,
    )

    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur("2025-01-01", "2025-12-31", 60, accn="k25", filed="2026-02-15")]),
        "Assets": tagdata("USD", [
            inst("2025-12-31", 1000, form="10-K", accn="k25", filed="2026-02-15")]),
        "Liabilities": tagdata("USD", [
            inst("2025-12-31", 400, form="10-K", accn="k25", filed="2026-02-15")]),
        "Goodwill": tagdata("USD", [
            inst("2025-12-31", 50, form="10-K", accn="k25", filed="2026-02-15")]),
        "IntangibleAssetsNetExcludingGoodwill": tagdata("USD", [
            inst("2025-12-31", 30, form="10-K", accn="k25", filed="2026-02-15")]),
    }
    classed = {
        "EarningsPerShareBasic": tagdata("USD/shares", [
            dict(dur("2025-01-01", "2025-12-31", 6,
                     accn="k25", filed="2026-02-15"),
                 segments="ClassOfStock=EquivalentClassB;")]),
        "WeightedAverageNumberOfSharesOutstandingBasic": tagdata("shares", [
            dict(dur("2025-01-01", "2025-12-31", 10,
                     accn="k25", filed="2026-02-15"),
                 segments="ClassOfStock=EquivalentClassB;")]),
    }
    income, eps = _annual_net_income(gaap), _annual_eps(classed)
    counts = _annual_share_counts(classed, {}, eps, income)

    ratios = annual_ratios(
        gaap, income, {}, {}, annual_eps=eps, annual_share_counts=counts)

    assert ratios[2025]["bvps"] == 60
    assert ratios[2025]["tbvps"] == 52
    assert ratios[2025]["return_on_net_tangible_assets"] == round(60 / 520 * 100, 4)


def test_finkle_roe_uses_average_equity_and_debt_ratio_uses_combined_debt():
    from screener.normalize import _annual_net_income, annual_ratios

    gaap = {
        "NetIncomeLoss": tagdata("USD", [
            dur("2024-01-01", "2024-12-31", 50, accn="k24", filed="2025-02-15"),
            dur("2025-01-01", "2025-12-31", 60, accn="k25", filed="2026-02-15"),
        ]),
        "Assets": tagdata("USD", [
            inst("2024-12-31", 900, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 1000, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "Liabilities": tagdata("USD", [
            inst("2024-12-31", 400, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 400, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "LongTermDebtNoncurrent": tagdata("USD", [
            inst("2024-12-31", 150, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 200, form="10-K", accn="k25", filed="2026-02-15"),
            # A later quarterly comparative must not replace annual history.
            inst("2025-12-31", 999, form="10-Q", accn="q126", filed="2026-05-01"),
        ]),
        "LongTermDebt": tagdata("USD", [
            inst("2024-12-31", 160, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 230, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "LongTermDebtCurrent": tagdata("USD", [
            inst("2024-12-31", 10, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 30, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "ShortTermBorrowings": tagdata("USD", [
            inst("2024-12-31", 4, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 5, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
        "DebtLongtermAndShorttermCombinedAmount": tagdata("USD", [
            inst("2024-12-31", 164, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 235, form="10-K", accn="k25", filed="2026-02-15"),
        ]),
    }

    income = _annual_net_income(gaap)
    ratios = annual_ratios(gaap, income, {}, {})

    assert ratios[2025]["common_equity"] == 600
    assert ratios[2025]["average_common_equity"] == 550
    assert ratios[2025]["return_on_equity"] == round(60 / 550 * 100, 4)
    assert ratios[2025]["combined_debt"] == 235
    assert ratios[2025]["debt_to_equity"] == round(235 / 600, 4)


def test_stale_zero_on_priority_debt_tag_loses_to_newer_fact():
    """SRI pattern: the filer stopped updating LongTermDebtNoncurrent at a zero;
    the newer figure on a lower-priority tag must win, not the stale zero."""
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2025-09-30", 0, accn="q325", filed="2025-11-05")])
    gaap["LongTermDebt"] = tagdata("USD", [inst("2025-12-31", 180.9e6, form="10-K", accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 180.9e6
    assert "LongTermDebt" in s.long_term_debt.provenance.tag


def test_at_one_date_the_larger_debt_figure_is_the_whole_one():
    """Chain order used to break the tie, which let a footnote fragment outrank the
    balance-sheet line: Carriage Services shipped 14.4M where its own LongTermDebt
    reads 526,016,000 at the same date and LongTermDebtNoncurrent 5,411,000 is a
    note (10-Q 0001016281-26-000055). Preferring the larger is safe because the
    plain tag suppresses its own current twin from the short bucket."""
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["LongTermDebt"] = tagdata("USD", [inst("2026-03-31", 33e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 33e9
    # ...and the plain tag's own current twin must not be counted a second time
    assert "LongTermDebtCurrent" not in (
        s.short_term_debt.provenance.tag if s.short_term_debt else "")
    assert s.long_term_debt.provenance.tag == "us-gaap:LongTermDebt"


def test_parent_only_liabilities_derivation_skips_nci():
    """Parent-only equity leaves NCI inside derived liabilities; subtracting
    MinorityInterest again would remove it twice."""
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 590e9, accn="q126")])
    gaap["MinorityInterest"] = tagdata("USD", [inst("2026-03-31", 10e9, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 410e9  # NCI stays inside
    assert s.noncontrolling_interest is None

    # counterpart: equity INCLUDING NCI keeps NCI out of liabilities -> it must be subtracted
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 600e9, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 400e9
    assert float(s.noncontrolling_interest.value) == 10e9


def test_stale_debt_rollup_loses_to_fresher_parts():
    """SRI: the combined rollup froze a quarter before the parts moved; the
    fresher basis must win, so the stale rollup is dropped entirely."""
    gaap = dict(GAAP)
    gaap["DebtAndCapitalLeaseObligations"] = tagdata("USD", [inst("2025-09-30", 947e3, accn="q325", filed="2025-11-05")])
    gaap["LongTermDebt"] = tagdata("USD", [inst("2025-12-31", 180.9e6, form="10-K", accn="k25", filed="2026-02-15")])
    s = build(gaap)
    assert s.total_debt is None
    assert float(s.long_term_debt.value) == 180.9e6

    # counterexample: rollup at the same period end as the parts is kept
    gaap["DebtAndCapitalLeaseObligations"] = tagdata("USD", [inst("2025-12-31", 182e6, form="10-K", accn="k25", filed="2026-02-15")])
    assert float(build(gaap).total_debt.value) == 182e6


def test_convertible_notes_are_the_whole_debt_ddog_style():
    """DDOG/SNOW: converts under ConvertibleDebtNoncurrent are the only debt."""
    gaap = dict(GAAP)
    gaap["ConvertibleDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 985.5e6, accn="q126")])
    gaap["ConvertibleNotesPayableCurrent"] = tagdata("USD", [inst("2026-03-31", 0, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 985.5e6
    # current convertible slot is a valid zero -> short bucket computable
    assert float(s.short_term_debt.value) == 0


def test_convertible_family_not_added_beside_a_primary_rollup():
    # NOW/AFRM: the rollup already contains the converts
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["ConvertibleDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 10e9, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 30e9


def test_notes_plus_loans_summed_realty_income_style():
    gaap = dict(GAAP)
    gaap["NotesPayable"] = tagdata("USD", [inst("2026-03-31", 25091.6e6, accn="q126")])
    gaap["LoansPayable"] = tagdata("USD", [inst("2026-03-31", 2760.4e6, accn="q126")])
    # convertible would hide inside the notes total: never a separate slot here
    gaap["ConvertibleNotesPayable"] = tagdata("USD", [inst("2026-03-31", 5e9, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 25091.6e6 + 2760.4e6
    # notes totals include the current portion: the ltd_current slot is suppressed
    assert s.short_term_debt is None


def test_notes_and_loans_parent_rollup_wins_over_the_pair():
    gaap = dict(GAAP)
    gaap["NotesAndLoansPayable"] = tagdata("USD", [inst("2026-03-31", 27e9, accn="q126")])
    gaap["NotesPayable"] = tagdata("USD", [inst("2026-03-31", 25e9, accn="q126")])
    gaap["LoansPayable"] = tagdata("USD", [inst("2026-03-31", 2e9, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 27e9


def test_credit_line_is_additive_beside_other_instruments_sri_style():
    gaap = dict(GAAP)
    gaap["LongTermLineOfCredit"] = tagdata("USD", [inst("2026-03-31", 151.1e6, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 151.1e6


def test_secured_debt_takes_the_larger_representation_never_both():
    # AVA: instruments show a 3M fragment while SecuredLongTermDebt holds 2,759M
    gaap = dict(GAAP)
    gaap["LongTermLoansPayable"] = tagdata("USD", [inst("2026-03-31", 3e6, accn="q126")])
    gaap["SecuredLongTermDebt"] = tagdata("USD", [inst("2026-03-31", 2759e6, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 2759e6

    # BRT: a fresh zero revolver must not block the secured figure
    gaap = dict(GAAP)
    gaap["LongTermLineOfCredit"] = tagdata("USD", [inst("2026-03-31", 0, accn="q126")])
    gaap["SecuredDebt"] = tagdata("USD", [inst("2026-03-31", 469e6, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 469e6

    # the reverse: instruments larger -> secured dropped, never summed
    gaap = dict(GAAP)
    gaap["NotesPayable"] = tagdata("USD", [inst("2026-03-31", 10e9, accn="q126")])
    gaap["SecuredDebt"] = tagdata("USD", [inst("2026-03-31", 4e9, accn="q126")])
    assert float(build(gaap).long_term_debt.value) == 10e9


def test_combined_finance_lease_tag_suppresses_its_current_twin_boeing_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["FinanceLeaseLiability"] = tagdata("USD", [inst("2026-03-31", 111e6, accn="q126")])
    gaap["FinanceLeaseLiabilityCurrent"] = tagdata("USD", [inst("2026-03-31", 40e6, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 5e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 30e9 + 111e6
    assert float(s.short_term_debt.value) == 5e9  # lease current NOT double counted


def test_noncurrent_lease_tag_still_lets_the_current_lease_count():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["FinanceLeaseLiabilityNoncurrent"] = tagdata("USD", [inst("2026-03-31", 71e6, accn="q126")])
    gaap["FinanceLeaseLiabilityCurrent"] = tagdata("USD", [inst("2026-03-31", 40e6, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 5e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 30e9 + 71e6
    assert float(s.short_term_debt.value) == 5e9 + 40e6


def test_notes_payable_current_equal_to_commercial_paper_counts_once_ed_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["NotesPayableCurrent"] = tagdata("USD", [inst("2026-03-31", 869e6, accn="q126")])
    gaap["CommercialPaper"] = tagdata("USD", [inst("2026-03-31", 869e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 869e6

    # different figures = genuinely different lines: both count
    gaap["NotesPayableCurrent"] = tagdata("USD", [inst("2026-03-31", 500e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 500e6 + 869e6


def test_bank_loans_and_notes_tag_terminates_the_borrowings_slot_key_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["ShortTermBankLoansAndNotesPayable"] = tagdata("USD", [inst("2026-03-31", 3680e6, accn="q126")])
    gaap["OtherShortTermBorrowings"] = tagdata("USD", [inst("2026-03-31", 3680e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 3680e6


def test_commercial_paper_and_other_borrowings_are_disjoint_ko_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 30e9, accn="q126")])
    gaap["CommercialPaper"] = tagdata("USD", [inst("2026-03-31", 250e6, accn="q126")])
    gaap["OtherShortTermBorrowings"] = tagdata("USD", [inst("2026-03-31", 56e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 306e6


def test_current_family_never_added_beside_a_current_rollup_smp_style():
    # SMP: LinesOfCreditCurrent + OtherLongTermDebtCurrent == LongTermDebtCurrent exactly
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 54.6e6, accn="q126")])
    gaap["LinesOfCreditCurrent"] = tagdata("USD", [inst("2026-03-31", 34.6e6, accn="q126")])
    gaap["OtherLongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 20e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 54.6e6


def test_revolver_only_filer_gets_a_short_bucket_cal_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    gaap["LinesOfCreditCurrent"] = tagdata("USD", [inst("2026-03-31", 347.5e6, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 347.5e6


def test_senior_notes_current_only_without_parent_and_rollup_teva_style():
    # TEVA: SeniorNotesCurrent == the entire LongTermDebtCurrent rollup
    gaap = dict(GAAP)
    gaap["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 15e9, accn="q126")])
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 2.1e9, accn="q126")])
    gaap["SeniorNotesCurrent"] = tagdata("USD", [inst("2026-03-31", 2.1e9, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 2.1e9

    del gaap["LongTermDebtCurrent"]
    assert float(build(gaap).short_term_debt.value) == 2.1e9


def test_other_intangibles_beat_a_smaller_parts_sum_hban_style():
    # HBAN: Other 1,727M contains the finite 969M plus 758M of MSRs
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 969e6, accn="q126")])
    gaap["OtherIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 1727e6, accn="q126")])
    assert float(build(gaap).intangibles.value) == 1727e6
    # SPGI: when the Other line is the smaller residual, the parts sum wins — never both
    gaap["OtherIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 100e6, accn="q126")])
    assert float(build(gaap).intangibles.value) == 969e6


def test_indefinite_class_tags_fill_the_empty_slot_ko_style():
    # KO: trademarks live only in IndefiniteLivedTrademarks
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["IndefiniteLivedTrademarks"] = tagdata("USD", [inst("2026-03-31", 12463e6, accn="q126")])
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [inst("2026-03-31", 100e6, accn="q126")])
    s = build(gaap)
    assert float(s.intangibles.value) == 12463e6 + 100e6
    # the rollup wins over class tags when both exist
    gaap["IndefiniteLivedIntangibleAssetsExcludingGoodwill"] = tagdata("USD", [inst("2026-03-31", 12463e6, accn="q126")])
    assert float(build(gaap).intangibles.value) == 12463e6 + 100e6


def test_misused_intangible_total_adds_separate_trademarks_eml_style():
    gaap = dict(GAAP)
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2026-01-03", 5269204, form="10-K", accn="k25", filed="2026-03-03"),
        inst("2026-07-04", 4121143, accn="q226", filed="2026-08-11"),
    ])
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [
        inst("2026-01-03", 5269204, form="10-K", accn="k25", filed="2026-03-03"),
    ])
    gaap["IndefiniteLivedTrademarks"] = tagdata("USD", [
        inst("2026-01-03", 5082767, form="10-K", accn="k25", filed="2026-03-03"),
        inst("2026-07-04", 5082816, accn="q226", filed="2026-08-11"),
    ])

    # Exact equality is not a market-wide heuristic; without filing-backed
    # issuer verification the nominal total remains authoritative.
    assert float(build(gaap).intangibles.value) == 4121143

    s = build_snapshot("EML", "0000031107", facts_doc(gaap))
    assert float(s.intangibles.value) == 4121143 + 5082816
    assert s.intangibles.provenance.period_end == date(2026, 7, 4)
    assert "IntangibleAssetsNetExcludingGoodwill" in s.intangibles.provenance.tag
    assert "IndefiniteLivedTrademarks" in s.intangibles.provenance.tag

    # Exact equality is the proof: a genuine rollup remains authoritative when
    # it exceeds its finite-lived component.
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2026-01-03", 10351971, form="10-K", accn="k25", filed="2026-03-03"),
        inst("2026-07-04", 9203959, accn="q226", filed="2026-08-11"),
    ])
    assert float(build_snapshot(
        "EML", "0000031107", facts_doc(gaap)).intangibles.value) == 9203959


def test_verified_opk_intangible_total_includes_separately_printed_iprd():
    gaap = dict(GAAP)
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2026-06-30", 477566000, accn="q226", filed="2026-07-27"),
    ])
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [
        inst("2026-06-30", 477566000, accn="q226", filed="2026-07-27"),
    ])
    gaap["IndefiniteLivedIntangibleAssetsExcludingGoodwill"] = tagdata("USD", [
        inst("2026-06-30", 672600000, accn="q226", filed="2026-07-27"),
    ])

    # AVD uses this same nominal indefinite-lived element for gross cost. The
    # market-wide rule must therefore keep the exact net balance-sheet total.
    assert float(build(gaap).intangibles.value) == 477566000

    # OPK's filing says the larger value is net intangibles other than goodwill,
    # including the separately printed $195m IPR&D balance.
    s = build_snapshot("OPK", "0000944809", facts_doc(gaap))
    assert float(s.intangibles.value) == 672600000
    assert s.intangibles.provenance.period_end == date(2026, 6, 30)
    assert "IndefiniteLivedIntangibleAssetsExcludingGoodwill" in s.intangibles.provenance.tag


def test_verified_brkr_intangible_successor_and_bmrn_total_control():
    gaap = dict(GAAP)
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2025-12-31", 899.6e6, form="10-K", accn="k25",
             filed="2026-02-27"),
    ])
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [
        inst("2026-03-31", 867.8e6, accn="q126")])
    assert float(build(gaap).intangibles.value) == 899.6e6
    assert float(build_snapshot(
        "BRKR", "0001109354", facts_doc(gaap)).intangibles.value) == 867.8e6

    # BMRN uses both finite-lived and nominal total for the complete net amount;
    # its separately filed indefinite balance is already inside that total.
    gaap["IntangibleAssetsNetExcludingGoodwill"] = tagdata("USD", [
        inst("2026-03-31", 4879367e3, accn="q126")])
    gaap["FiniteLivedIntangibleAssetsNet"] = tagdata("USD", [
        inst("2026-03-31", 4879367e3, accn="q126")])
    gaap["IndefiniteLivedIntangibleAssetsExcludingGoodwill"] = tagdata("USD", [
        inst("2026-03-31", 300e6, accn="q126")])
    assert float(build_snapshot(
        "BMRN", "0001048477", facts_doc(gaap)).intangibles.value) == 4879367e3


def test_combined_minus_goodwill_derivation_needs_a_common_period_end():
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    # goodwill is annual-only; the combined line is quarterly — common end is FY
    gaap["Goodwill"] = tagdata("USD", [inst("2025-12-31", 50e9, form="10-K", accn="k25", filed="2026-02-15")])
    gaap["IntangibleAssetsNetIncludingGoodwill"] = tagdata("USD", [
        inst("2025-12-31", 80e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 82e9, accn="q126"),
    ])
    s = build(gaap)
    assert float(s.intangibles.value) == 30e9
    assert "IntangibleAssetsNetIncludingGoodwill - us-gaap:Goodwill" in s.intangibles.provenance.tag

    # CALM guard: combined below goodwill is a mistag, never a negative value
    gaap["IntangibleAssetsNetIncludingGoodwill"] = tagdata("USD", [
        inst("2025-12-31", 40e9, form="10-K", accn="k25", filed="2026-02-15")])
    assert build(gaap).intangibles is None


def test_gross_minus_accumulated_requires_matching_period_end_bby_style():
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["FiniteLivedIntangibleAssetsGross"] = tagdata("USD", [
        inst("2025-12-31", 96e8, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 97e8, accn="q126"),
    ])
    gaap["FiniteLivedIntangibleAssetsAccumulatedAmortization"] = tagdata("USD", [
        inst("2025-12-31", 95e8, form="10-K", accn="k25", filed="2026-02-15")])
    s = build(gaap)
    # matched at 2025-12-31: 9.6B - 9.5B, never the fresh gross alone
    assert float(s.intangibles.value) == 1e8


def test_goodwill_derived_from_gross_when_never_impaired_aaon_style():
    gaap = {k: v for k, v in GAAP.items() if k != "Goodwill"}
    gaap["GoodwillGross"] = tagdata("USD", [inst("2026-03-31", 573e6, accn="q126")])
    s = build(gaap)
    assert float(s.goodwill.value) == 573e6
    assert "never tagged" in s.goodwill.provenance.concept


def test_servicing_assets_sum_both_measurement_books_wfc_style():
    gaap = {k: v for k, v in GAAP.items()
            if k not in ("IntangibleAssetsNetExcludingGoodwill",)}
    gaap["ServicingAssetAtFairValueAmount"] = tagdata("USD", [inst("2026-03-31", 1.3e9, accn="q126")])
    gaap["ServicingAssetAtAmortizedValue"] = tagdata("USD", [inst("2026-03-31", 0.4e9, accn="q126")])
    assert float(build(gaap).intangibles.value) == 1.7e9


def test_capitalized_software_is_the_disclosed_last_resort_mcd_style():
    gaap = {k: v for k, v in GAAP.items() if k != "IntangibleAssetsNetExcludingGoodwill"}
    gaap["CapitalizedComputerSoftwareNet"] = tagdata("USD", [inst("2026-03-31", 800e6, accn="q126")])
    s = build(gaap)
    assert float(s.intangibles.value) == 800e6
    assert "standing in" in s.intangibles.provenance.concept


def test_partners_capital_derives_liabilities_for_mlps_paa_style():
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 29218e6, accn="q126")])
    gaap["PartnersCapitalIncludingPortionAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 14291e6, accn="q126")])
    gaap["PartnersCapitalAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 3212e6, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 29218e6 - 14291e6
    # PCI kept NCI out of liabilities -> the partners-capital NCI is subtracted
    assert float(s.noncontrolling_interest.value) == 3212e6


def test_parent_only_partners_capital_skips_nci_dkl_style():
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e6, accn="q126")])
    gaap["PartnersCapital"] = tagdata("USD", [inst("2026-03-31", -69.3e6, accn="q126")])  # deficit is fine
    gaap["PartnersCapitalAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 50e6, accn="q126")])
    s = build(gaap)
    assert float(s.total_liabilities.value) == 1000e6 + 69.3e6
    assert s.noncontrolling_interest is None  # NCI already inside derived liabilities


def test_nonredeemable_nci_is_an_alternative_never_added_ms_style():
    gaap = dict(GAAP)
    gaap["MinorityInterest"] = tagdata("USD", [inst("2026-03-31", 1111e6, accn="q126")])
    gaap["NonredeemableNoncontrollingInterest"] = tagdata("USD", [inst("2026-03-31", 1111e6, accn="q126")])
    assert float(build(gaap).noncontrolling_interest.value) == 1111e6


def test_redeemable_nci_components_only_when_the_total_is_absent_udr_style():
    gaap = dict(GAAP)
    gaap["RedeemableNoncontrollingInterestEquityCarryingAmount"] = \
        tagdata("USD", [inst("2026-03-31", 155.6e6, accn="q126")])
    gaap["RedeemableNoncontrollingInterestEquityPreferredCarryingAmount"] = \
        tagdata("USD", [inst("2026-03-31", 155.6e6, accn="q126")])
    assert float(build(gaap).noncontrolling_interest.value) == 155.6e6

    del gaap["RedeemableNoncontrollingInterestEquityCarryingAmount"]
    gaap["RedeemableNoncontrollingInterestEquityCommonCarryingAmount"] = \
        tagdata("USD", [inst("2026-03-31", 44.4e6, accn="q126")])
    assert float(build(gaap).noncontrolling_interest.value) == 200e6


def test_redeemable_fair_value_blocked_by_the_incl_nci_temporary_tag_heico_style():
    gaap = dict(GAAP)
    gaap["RedeemableNoncontrollingInterestEquityFairValue"] = \
        tagdata("USD", [inst("2026-03-31", 103.1e6, accn="q126")])
    gaap["TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests"] = \
        tagdata("USD", [inst("2026-03-31", 536.7e6, accn="q126")])
    s = build(gaap)
    # same holders once: the incl-NCI line fills the temporary-equity slot instead
    assert s.noncontrolling_interest is None
    assert float(s.temporary_equity.value) == 536.7e6

    # OMC-style clean case: fair value stands when nothing else names the holders
    del gaap["TemporaryEquityCarryingAmountIncludingPortionAttributableToNoncontrollingInterests"]
    s = build(gaap)
    assert float(s.noncontrolling_interest.value) == 103.1e6
    assert s.temporary_equity is None


def test_temporary_equity_deducted_when_preferred_is_a_zero_placeholder_kdp_style():
    gaap = dict(GAAP)
    gaap["PreferredStockValue"] = tagdata("USD", [inst("2026-03-31", 0, accn="q126")])
    gaap["TemporaryEquityCarryingAmountAttributableToParent"] = \
        tagdata("USD", [inst("2026-03-31", 4418e6, accn="q126")])
    s = build(gaap)
    assert float(s.temporary_equity.value) == 4418e6

    # nonzero preferred keeps the slot: never both
    gaap["PreferredStockValue"] = tagdata("USD", [inst("2026-03-31", 600e6, accn="q126")])
    assert build(gaap).temporary_equity is None


def test_temporary_equity_skipped_when_liabilities_were_derived():
    # a derived L = LSE - equity figure already contains the mezzanine (CMCSA/ACN)
    gaap = {k: v for k, v in GAAP.items() if k != "Liabilities"}
    gaap["LiabilitiesAndStockholdersEquity"] = tagdata("USD", [inst("2026-03-31", 1000e9, accn="q126")])
    gaap["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"] = \
        tagdata("USD", [inst("2026-03-31", 600e9, accn="q126")])
    gaap["TemporaryEquityCarryingAmountAttributableToParent"] = \
        tagdata("USD", [inst("2026-03-31", 5e9, accn="q126")])
    assert build(gaap).temporary_equity is None


def test_liquidation_preference_is_the_last_resort_eikon_style():
    gaap = dict(GAAP)
    gaap["PreferredStockValue"] = tagdata("USD", [inst("2026-03-31", 0, accn="q126")])
    gaap["TemporaryEquityLiquidationPreference"] = \
        tagdata("USD", [inst("2026-03-31", 1159.8e6, accn="q126")])
    assert float(build(gaap).temporary_equity.value) == 1159.8e6


def test_impairment_rollup_note_only_without_a_specific_line_gis_style():
    base = dict(GAAP)
    base["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
        tagdata("USD", [dur("2026-01-01", "2026-03-31", 2e9, form="10-Q", accn="q126", filed="2026-05-05")])
    gaap = dict(base)
    gaap["GoodwillImpairmentLoss"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 302.9e6, form="10-Q", accn="q126", filed="2026-05-05")])
    gaap["GoodwillAndIntangibleAssetImpairment"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 1750e6, form="10-Q", accn="q126", filed="2026-05-05")])
    notes = [n["text"] for n in build(gaap).earnings_quality]
    assert not any("rollup" in n or "Goodwill and intangible" in n for n in notes)

    gaap = dict(base)
    gaap["GoodwillAndIntangibleAssetImpairment"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 1750e6, form="10-Q", accn="q126", filed="2026-05-05")])
    notes = [n["text"] for n in build(gaap).earnings_quality]
    assert any("goodwill and intangible impairment" in n.lower() for n in notes)


def test_combined_debt_rollup_including_current_maturities_jpm_style():
    gaap = dict(GAAP)
    gaap["LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"] = \
        tagdata("USD", [inst("2026-03-31", 460.5e9, accn="q126")])
    assert float(build(gaap).total_debt.value) == 460.5e9


def test_long_term_notes_and_loans_rollup_teva_style():
    gaap = dict(GAAP)
    gaap["LongTermNotesAndLoans"] = tagdata("USD", [inst("2026-03-31", 16.8e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 16.8e9
    # long-term-only variant: the current side stays open
    gaap["LongTermDebtCurrent"] = tagdata("USD", [inst("2026-03-31", 2e9, accn="q126")])
    assert float(build(gaap).short_term_debt.value) == 2e9


def test_combined_senior_notes_suppress_only_their_current_twin_syf_style():
    gaap = dict(GAAP)
    gaap["SeniorNotes"] = tagdata("USD", [inst("2026-03-31", 7.7e9, accn="q126")])
    gaap["SeniorNotesCurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    gaap["OtherNotesPayableCurrent"] = tagdata("USD", [inst("2026-03-31", 200e6, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 7.7e9
    # senior-current inside the combined tag; other notes still count
    assert float(s.short_term_debt.value) == 200e6


def test_other_loans_payable_and_junior_subordinated_notes_and_warehouse():
    gaap = dict(GAAP)
    gaap["OtherLoansPayable"] = tagdata("USD", [inst("2026-03-31", 935e6, accn="q126")])
    gaap["JuniorSubordinatedNotes"] = tagdata("USD", [inst("2026-03-31", 37e6, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 935e6 + 37e6

    gaap2 = dict(GAAP)
    gaap2["LongTermDebtNoncurrent"] = tagdata("USD", [inst("2026-03-31", 1e9, accn="q126")])
    gaap2["WarehouseAgreementBorrowings"] = tagdata("USD", [inst("2026-03-31", 300e6, accn="q126")])
    assert float(build(gaap2).short_term_debt.value) == 300e6


def test_current_liabilities_derived_from_the_noncurrent_split():
    gaap = {k: v for k, v in GAAP.items() if k != "LiabilitiesCurrent"}
    gaap["LiabilitiesNoncurrent"] = tagdata("USD", [inst("2026-03-31", 250e9, accn="q126")])
    s = build(gaap)
    # Liabilities 400e9 - noncurrent 250e9
    assert float(s.current_liabilities.value) == 150e9
    assert "derived" in s.current_liabilities.provenance.concept


def test_redeemable_nci_other_component_counts_without_a_total_et_style():
    gaap = dict(GAAP)
    gaap["RedeemableNoncontrollingInterestEquityOtherCarryingAmount"] = \
        tagdata("USD", [inst("2026-03-31", 256e6, accn="q126")])
    assert float(build(gaap).noncontrolling_interest.value) == 256e6

    # the total wins when present — components never stack on it
    gaap["RedeemableNoncontrollingInterestEquityCarryingAmount"] = \
        tagdata("USD", [inst("2026-03-31", 256e6, accn="q126")])
    assert float(build(gaap).noncontrolling_interest.value) == 256e6


def test_secured_plus_unsecured_axis_beats_a_lone_instrument_gs_style():
    """GS reports $348B unsecured and $11.6B secured with no instrument rollup;
    subordinated debt sits inside unsecured and must not be added on top."""
    gaap = dict(GAAP)
    gaap["UnsecuredLongTermDebt"] = tagdata("USD", [inst("2026-03-31", 347.96e9, accn="q126")])
    gaap["SecuredLongTermDebt"] = tagdata("USD", [inst("2026-03-31", 11.56e9, accn="q126")])
    gaap["SubordinatedDebt"] = tagdata("USD", [inst("2026-03-31", 14.81e9, accn="q126")])
    s = build(gaap)
    assert float(s.long_term_debt.value) == 347.96e9 + 11.56e9

    # a filer with subordinated debt and no axis tags still gets its figure
    gaap2 = {k: v for k, v in gaap.items()
             if k not in ("UnsecuredLongTermDebt", "SecuredLongTermDebt")}
    assert float(build(gaap2).long_term_debt.value) == 14.81e9


def test_negative_assets_are_a_sign_error_not_a_balance_sheet():
    gaap = dict(GAAP)
    gaap["AssetsCurrent"] = tagdata("USD", [inst("2026-03-31", -26, accn="q126")])
    s = build(gaap)
    assert s.current_assets is None


def test_a_lease_book_does_not_stand_in_for_a_companys_borrowings():
    """Ford's noncurrent debt element went stale in 2020, leaving a $754M finance
    lease as the only fresh long-term figure and criterion 3 passing at 0.09x. Its
    interest bill is $1,254M — 166% of the lease principal, which no lease produces."""
    gaap = dict(GAAP)
    gaap["FinanceLeaseLiabilityNoncurrent"] = tagdata("USD", [
        inst("2025-12-31", 754e6, form="10-K", accn="k25", filed="2026-02-11")])
    gaap["InterestExpense"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 1254e6, accn="k25", filed="2026-02-11")])
    assert build(gaap).long_term_debt is None


def test_a_debt_free_filers_lease_is_still_its_debt():
    """Vertex, Incyte, MongoDB and Plexus all carry a lease-only bucket and no
    borrowings; their interest is a fraction of the principal and the figure stands."""
    gaap = dict(GAAP)
    gaap["FinanceLeaseLiabilityNoncurrent"] = tagdata("USD", [
        inst("2025-12-31", 106.7e6, form="10-K", accn="k25", filed="2026-02-11")])
    gaap["InterestExpense"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 13.3e6, accn="k25", filed="2026-02-11")])
    ltd = build(gaap).long_term_debt
    assert ltd is not None and float(ltd.value) == 106700000.0


def test_a_convertible_preferred_is_disclosed_and_not_added_to_the_count():
    """Graham counts shares "including the conversion of preferred" (table 18.6), but the
    tag carries conversions already done as well as conversions still to come, and the
    preferred count that separates them is filed under a class axis. So the overhang is
    stated and the denominator left alone — Structure Therapeutics' 67.0M sit inside its
    71.2M common already, and adding them would have restated every per-share figure."""
    gaap = dict(GAAP)
    gaap["CommonStockSharesOutstanding"] = tagdata("shares", [inst("2026-03-31", 20e9, accn="q126")])
    gaap["PreferredStockLiquidationPreferenceValue"] = tagdata("USD", [
        inst("2026-03-31", 5e9, accn="q126")])
    gaap["ConvertiblePreferredStockSharesIssuedUponConversion"] = tagdata("shares", [
        inst("2026-03-31", 4e9, accn="q126")])
    s = build(gaap)
    assert float(s.shares_outstanding.value) == 20e9          # untouched
    assert float(s.preferred_stock.value) == 5e9              # still a senior claim
    note = next(n for n in s.context_notes if n["kind"] == "Convertible preferred")
    assert "4,000.0M shares" in note["text"] and "20,000.0M common" in note["text"]
    assert "capitalisation note" in note["text"]              # names the document to read


def test_an_abandoned_tag_does_not_outrank_the_element_that_replaced_it():
    """Energy Transfer moved its redeemable minority interest to the `Other` element
    after 2025-12-31 and left the old one on file. Reading the old one first put
    $250M into a June balance sheet whose printed page says $256M, and the
    difference came straight off common equity. Newer wins."""
    gaap = dict(GAAP) | {
        "MinorityInterest": tagdata("USD", [
            inst("2026-06-30", 15191e6, accn="q226", filed="2026-08-06")]),
        # abandoned mid-history, still within the staleness window
        "RedeemableNoncontrollingInterestEquityCarryingAmount": tagdata("USD", [
            inst("2025-12-31", 250e6, form="10-K", accn="k25", filed="2026-02-19")]),
        "RedeemableNoncontrollingInterestEquityOtherCarryingAmount": tagdata("USD", [
            inst("2026-06-30", 256e6, accn="q226", filed="2026-08-06")]),
    }
    s = build(gaap)
    assert float(s.noncontrolling_interest.value) == 15191e6 + 256e6
    assert s.noncontrolling_interest.provenance.period_end.isoformat() == "2026-06-30"


def test_a_total_still_wins_over_its_own_components_at_one_date():
    """A rollup is the whole; the components on file may be a fragment of it. Only
    a newer date displaces it — UDR tags a component equal to its own total."""
    gaap = dict(GAAP) | {
        "MinorityInterest": tagdata("USD", [
            inst("2026-06-30", 100e6, accn="q226", filed="2026-08-06")]),
        "RedeemableNoncontrollingInterestEquityCarryingAmount": tagdata("USD", [
            inst("2026-06-30", 80e6, accn="q226", filed="2026-08-06")]),
        "RedeemableNoncontrollingInterestEquityCommonCarryingAmount": tagdata("USD", [
            inst("2026-06-30", 30e6, accn="q226", filed="2026-08-06")]),
    }
    s = build(gaap)
    assert float(s.noncontrolling_interest.value) == 180e6      # total, not the part
