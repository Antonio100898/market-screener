"""The disclosures: what the figures do not say on their own, and the
guards that decide when a note is worth making."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from screener.normalize import (PendingFilingFactsError, UnsupportedFilerError,
                                _fy_label, build_snapshot)
from helpers import *  # noqa: F403 — the shared fixtures, by design
from helpers import EPS, GAAP, build, dur, facts_doc, inst, tagdata, texts
from helpers import _dimensioned, _reported, _shares, _years, _yr  # underscored, so `import *` skips them


def _foreign_gaap():
    foreign_gaap = {
        tag: tagdata(unit, [
            {**entry, "form": "20-F" if entry["form"].startswith("10-K") else "6-K"}
            for entry in entries
        ])
        for tag, data in GAAP.items()
        for unit, entries in data["units"].items()
    }
    return foreign_gaap


def _foreign_ifrs(*, currency="USD", filed="2026-03-25", accn="ifrs25"):
    annual = lambda value: dur(  # noqa: E731 - compact filing-shaped fixture
        "2025-01-01", "2025-12-31", value, form="20-F", accn=accn, filed=filed)
    instant = lambda value: inst(  # noqa: E731
        "2025-12-31", value, form="20-F", accn=accn, filed=filed)
    return {
        "Assets": tagdata(currency, [instant(1_000_000_000)]),
        "EquityAndLiabilities": tagdata(currency, [instant(1_000_000_000)]),
        "CurrentAssets": tagdata(currency, [instant(400_000_000)]),
        "CurrentLiabilities": tagdata(currency, [instant(200_000_000)]),
        "Equity": tagdata(currency, [instant(500_000_000)]),
        "EquityAttributableToOwnersOfParent": tagdata(currency, [instant(450_000_000)]),
        "NoncontrollingInterests": tagdata(currency, [instant(50_000_000)]),
        "LongtermBorrowings": tagdata(currency, [instant(100_000_000)]),
        "ShorttermBorrowings": tagdata(currency, [instant(20_000_000)]),
        "LeaseLiabilities": tagdata(currency, [instant(30_000_000)]),
        "Goodwill": tagdata(currency, [instant(10_000_000)]),
        "IntangibleAssetsOtherThanGoodwill": tagdata(currency, [instant(5_000_000)]),
        "NumberOfSharesOutstanding": tagdata("shares", [instant(100_000_000)]),
        "WeightedAverageShares": tagdata("shares", [annual(100_000_000)]),
        "DilutedEarningsLossPerShare": tagdata(f"{currency}/shares", [annual(2.0)]),
        "ProfitLossAttributableToOwnersOfParent": tagdata(currency, [annual(200_000_000)]),
        "ProfitLoss": tagdata(currency, [annual(210_000_000)]),
        "ProfitLossAttributableToNoncontrollingInterests": tagdata(
            currency, [annual(10_000_000)]),
        "Revenue": tagdata(currency, [annual(1_000_000_000)]),
        "ProfitLossFromOperatingActivities": tagdata(currency, [annual(250_000_000)]),
        "ProfitLossBeforeTax": tagdata(currency, [annual(240_000_000)]),
        "DividendsPaidClassifiedAsFinancingActivities": tagdata(
            currency, [annual(20_000_000)]),
    }


def test_foreign_us_gaap_filer_requires_an_exact_cover_security():
    with pytest.raises(UnsupportedFilerError, match="security title"):
        build(_foreign_gaap())


def test_new_foreign_annual_without_statements_is_pending_not_unsupported():
    from screener.sync import _derive

    filed = (date.today() - timedelta(days=1)).isoformat()
    facts = facts_doc(_foreign_gaap(), {
        "EntityCommonStockSharesOutstanding": tagdata("shares", [
            inst("2026-06-30", 100_000_000, form="20-F", accn="new26",
                 filed=filed)]),
    })
    receipt = {"symbol": "TEST", "title": "Ordinary Shares", "ratio": None,
               "accn": "k25"}

    with pytest.raises(PendingFilingFactsError) as raised:
        build_snapshot("TEST", "0000000001", facts, receipt=receipt)
    assert raised.value.filing == (filed, "new26")

    status, row = _derive("0000000001", "TEST", facts, receipt=receipt)
    assert status == "pending_facts"
    assert row["cik"] == "0000000001"
    assert row["data_pending"]["accession"] == "new26"
    assert row["sources"]["total_assets"]["accn"] != "new26"


def test_cover_verified_foreign_ordinary_share_is_supported():
    snapshot = build_snapshot(
        "TEST", "0000000001", facts_doc(_foreign_gaap()),
        receipt={"symbol": "TEST", "title": "Ordinary Shares", "ratio": None,
                 "accn": "k25"},
    )
    assert snapshot.balance_sheet_date == date(2026, 3, 31)


def test_foreign_depositary_share_without_a_ratio_is_rejected():
    with pytest.raises(UnsupportedFilerError, match="ratio"):
        build_snapshot(
            "TEST", "0000000001", facts_doc(_foreign_gaap()),
            receipt={"symbol": "TEST", "title": "American Depositary Shares",
                     "ratio": None, "accn": "k25"},
        )


def test_later_ticker_keeps_annual_cover_as_depositary_ratio_authority():
    snapshot = build_snapshot(
        "NEW", "0000000001", facts_doc(_foreign_gaap()),
        receipt={
            "symbol": "NEW",
            "title": "American Depositary Shares",
            "ratio": "2",
            "accn": "later-f3",
            "basis_accn": "k25",
        },
    )

    assert "cover of k25" in snapshot.shares_outstanding.provenance.concept
    assert "later-f3" not in snapshot.shares_outstanding.provenance.concept


def test_foreign_security_identity_must_come_from_the_current_annual_cover():
    with pytest.raises(UnsupportedFilerError, match="current annual cover"):
        build_snapshot(
            "TEST", "0000000001", facts_doc(_foreign_gaap()),
            receipt={"symbol": "TEST", "title": "Ordinary Shares", "ratio": None,
                     "accn": "k24"},
        )


def test_later_sec_same_class_ticker_reaches_a_supported_foreign_snapshot():
    snapshot = build_snapshot(
        "NEW", "0000000001", facts_doc(_foreign_gaap()),
        receipt={
            "symbol": "NEW",
            "title": "Ordinary Shares",
            "ratio": None,
            "accn": "later-f3",
            "basis_accn": "k25",
        },
    )

    assert snapshot.ticker == "NEW"
    assert snapshot.balance_sheet_date == date(2026, 3, 31)


def test_later_sec_ticker_rejects_a_mismatched_annual_basis():
    with pytest.raises(UnsupportedFilerError, match="current annual cover"):
        build_snapshot(
            "NEW", "0000000001", facts_doc(_foreign_gaap()),
            receipt={
                "symbol": "NEW",
                "title": "Ordinary Shares",
                "ratio": None,
                "accn": "later-f3",
                "basis_accn": "k24",
            },
        )


def test_later_sec_ticker_without_an_annual_basis_fails_closed():
    with pytest.raises(UnsupportedFilerError, match="current annual cover"):
        build_snapshot(
            "NEW", "0000000001", facts_doc(_foreign_gaap()),
            receipt={
                "symbol": "NEW",
                "title": "Ordinary Shares",
                "ratio": None,
                "accn": "later-f3",
            },
        )


def test_foreign_noncommon_security_is_rejected_even_when_the_symbol_matches():
    with pytest.raises(UnsupportedFilerError, match="common equity"):
        build_snapshot(
            "TEST", "0000000001", facts_doc(_foreign_gaap()),
            receipt={"symbol": "TEST", "title": "Preferred shares, par value $0.01",
                     "ratio": None, "accn": "k25"},
        )


def test_current_ifrs_report_uses_its_own_basis_not_old_us_gaap_history():
    facts = facts_doc(_foreign_gaap())
    facts["facts"]["ifrs-full"] = _foreign_ifrs(filed="2027-03-01", accn="ifrs25")
    snapshot = build_snapshot(
        "TEST", "0000000001", facts,
        receipt={"symbol": "TEST", "title": "Ordinary Shares", "ratio": None,
                 "accn": "ifrs25"},
    )
    assert snapshot.total_assets.value == Decimal("1000000000")
    assert snapshot.total_assets.provenance.tag == "ifrs-full:Assets"


def test_foreign_statement_currency_prefers_a_real_usd_balance_anchor():
    gaap = _foreign_gaap()
    gaap["AssetsCurrent"]["units"]["CAD"] = [
        inst("2025-12-31", 280e9, form="20-F", accn="k25", filed="2026-02-15")]
    snapshot = build_snapshot(
        "TEST", "0000000001", facts_doc(gaap),
        receipt={"symbol": "TEST", "title": "Ordinary Shares", "ratio": None,
                 "accn": "k25"},
    )
    assert snapshot.reporting_currency == "USD"
    # A convenience/secondary CAD line cannot be mixed into the USD statement.
    assert snapshot.current_assets.value == Decimal("300000000000.0")
    assert snapshot.current_assets.provenance.accession == "q126"


def test_foreign_ifrs_without_a_usd_balance_anchor_remains_unsupported():
    facts = {"facts": {"ifrs-full": {
        "Revenue": tagdata("USD", [dur("2025-01-01", "2025-12-31", 1.0, form="20-F")])
    }}}
    with pytest.raises(UnsupportedFilerError, match="coherent standard"):
        build_snapshot("IFRS", "0000000002", facts)


def test_cover_verified_usd_ifrs_filer_uses_equivalent_concepts_with_original_provenance():
    from screener.sync import _derive

    facts = {"facts": {"ifrs-full": _foreign_ifrs()}}
    receipt = {"symbol": "IFRS", "title": "Class A common shares",
               "ratio": None, "accn": "ifrs25"}
    snapshot = build_snapshot(
        "IFRS", "0000000002", facts, receipt=receipt,
    )
    assert snapshot.current_assets.value == Decimal("400000000")
    assert snapshot.current_liabilities.value == Decimal("200000000")
    assert snapshot.total_liabilities.value == Decimal("500000000")
    assert snapshot.annual_revenue[2025].value == Decimal("1000000000")
    assert snapshot.annual_net_income[2025].value == Decimal("200000000")
    assert snapshot.annual_operating_income[2025].value == Decimal("250000000")
    assert snapshot.annual_eps[2025].value == Decimal("2.0")
    assert snapshot.shares_outstanding.value == Decimal("100000000")
    assert snapshot.long_term_debt.value == Decimal("100000000")
    assert snapshot.short_term_debt.value == Decimal("20000000")
    assert snapshot.long_term_debt.provenance.tag == "ifrs-full:LongtermBorrowings"
    assert snapshot.total_liabilities.provenance.tag == (
        "ifrs-full:EquityAndLiabilities - ifrs-full:Equity")
    # IFRS 16's generic lease total is context, not silently finance debt.
    assert snapshot.long_term_debt.value != Decimal("130000000")

    status, row = _derive("0000000002", "IFRS", facts, receipt=receipt)
    assert status == "ok"
    assert row["annual_ratios"][2025]["current_ratio"] == 2.0
    assert row["annual_ratios"][2025]["net_margin"] == 20.0
    assert row["sources"]["eps"]["tag"] == "ifrs-full:DilutedEarningsLossPerShare"


def test_incorporated_statement_keeps_annual_selection_and_exact_source_provenance():
    from screener.sync import _derive

    annual_accn = "0000000001-26-000010"
    source_accn = "0000000001-26-000009"
    gaap = _foreign_ifrs(accn=annual_accn, filed="2026-03-01")
    for tagdata_ in gaap.values():
        for entries in tagdata_["units"].values():
            for entry in entries:
                entry.update({
                    "_source_accession": source_accn,
                    "_source_form": "6-K",
                    "_source_filed": "2026-02-28",
                    "_source_document": "statements.htm",
                    "_annual_accession": annual_accn,
                    "_annual_form": "40-F",
                    "_annual_filed": "2026-03-01",
                })
    facts = {"facts": {"ifrs-full": gaap}}
    receipt = {
        "symbol": "IFRS", "title": "Class A common shares",
        "ratio": None, "accn": annual_accn,
    }

    snapshot = build_snapshot("IFRS", "0000000001", facts, receipt=receipt)
    provenance = snapshot.total_assets.provenance
    assert (provenance.accession, provenance.form, provenance.filed) == (
        source_accn, "6-K", date(2026, 2, 28),
    )
    assert (
        provenance.annual_accession, provenance.annual_form, provenance.annual_filed,
    ) == (annual_accn, "40-F", date(2026, 3, 1))

    status, row = _derive("0000000001", "IFRS", facts, receipt=receipt)
    assert status == "ok"
    assert row["sources"]["total_assets"] == {
        "tag": "ifrs-full:Assets",
        "form": "6-K",
        "accn": source_accn,
        "end": "2025-12-31",
        "filed": "2026-02-28",
        "annual_accn": annual_accn,
        "annual_form": "40-F",
        "annual_filed": "2026-03-01",
        "document": "statements.htm",
        "canonical_tag": "Assets",
    }
    derived = row["sources"]["total_liabilities"]
    assert derived["annual_accn"] == annual_accn
    assert derived["accn"] == source_accn
    assert all(component["annual_accn"] == annual_accn
               for component in derived["components"])


def test_ifrs_continuing_operations_cash_flow_builds_fcf_with_original_provenance():
    gaap = _foreign_ifrs()
    annual = lambda value: dur(  # noqa: E731 - filing-shaped fixture
        "2025-01-01", "2025-12-31", value, form="20-F", accn="ifrs25",
        filed="2026-03-25")
    gaap.update({
        "AdjustmentsForDepreciationAndAmortisationExpense": tagdata("USD", [annual(80_000_000)]),
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": tagdata(
            "USD", [annual(120_000_000)]),
        "CashFlowsFromUsedInOperatingActivitiesContinuingOperations": tagdata(
            "USD", [annual(300_000_000)]),
    })

    snapshot = build_snapshot(
        "IFRS", "0000000002", {"facts": {"ifrs-full": gaap}},
        receipt={"symbol": "IFRS", "title": "Ordinary Shares", "ratio": None,
                 "accn": "ifrs25"},
    )

    assert snapshot.owner_earnings.free_cash_flow.value == Decimal("180000000")
    assert snapshot.owner_earnings.free_cash_flow.provenance.components[0].tag == (
        "ifrs-full:CashFlowsFromUsedInOperatingActivitiesContinuingOperations")


def test_ifrs_cash_flow_alias_keeps_the_primary_total_for_an_overlapping_period():
    from screener.normalize import _annual_series, _ifrs_as_us_gaap, _with_fiscal_calendar

    annual = lambda value: dur(  # noqa: E731 - filing-shaped fixture
        "2025-01-01", "2025-12-31", value, form="20-F", accn="ifrs25",
        filed="2026-03-25")
    gaap = {
        "Assets": tagdata("USD", [inst("2025-12-31", 1_000_000_000,
                                         form="20-F", accn="ifrs25", filed="2026-03-25")]),
        "CashFlowsFromUsedInOperatingActivities": tagdata("USD", [
            {**annual(400_000_000), "accn": "ifrs24", "filed": "2025-03-25"},
            annual(400_000_000),
        ]),
        "CashFlowsFromUsedInOperatingActivitiesContinuingOperations": tagdata(
            "USD", [annual(300_000_000)]),
    }

    normalized = _ifrs_as_us_gaap(gaap)
    assert len(normalized["NetCashProvidedByUsedInOperatingActivities"]["units"]["USD"]) == 2
    series = _annual_series(_with_fiscal_calendar(normalized),
                            "NetCashProvidedByUsedInOperatingActivities", unit=("USD",))

    assert series[2025].value == Decimal("400000000")
    assert series[2025].provenance.tag == "ifrs-full:CashFlowsFromUsedInOperatingActivities"


def test_non_usd_ifrs_statements_are_normalized_in_their_reported_currency():
    facts = {"facts": {"ifrs-full": _foreign_ifrs(currency="EUR")}}
    snapshot = build_snapshot(
        "IFRS", "0000000002", facts,
        receipt={"symbol": "IFRS", "title": "Ordinary Shares",
                 "ratio": None, "accn": "ifrs25"},
    )
    assert snapshot.reporting_currency == "EUR"
    assert snapshot.total_assets.value == Decimal("1000000000")
    assert snapshot.total_assets.provenance.unit == "EUR"
    assert snapshot.annual_eps[2025].value == Decimal("2.0")
    assert snapshot.annual_eps[2025].provenance.unit == "EUR/shares"


def test_dividend_recent_positive_payment():
    s = build()
    assert s.pays_dividend is True
    assert s.dividend.provenance.tag == "us-gaap:PaymentsOfDividendsCommonStock"


def test_no_dividend_facts_means_not_paying():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    assert build(gaap).pays_dividend is False


def test_stale_dividend_means_not_paying():
    gaap = dict(GAAP)
    gaap["PaymentsOfDividendsCommonStock"] = tagdata("USD", [
        dur("2022-01-01", "2022-03-31", 2e9, form="10-Q", accn="q122", filed="2022-05-05"),
    ])
    assert build(gaap).pays_dividend is False


def test_unchained_dividend_tag_gives_unknown_not_false():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["DividendsDeclaredButUnpaid"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 2e9, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)
    assert s.pays_dividend is None  # unknown, never a confident FAIL


def test_suspended_quarterly_payer_fails_within_two_quarters():
    # last positive quarterly dividend ended two quarters before the balance sheet
    gaap = dict(GAAP)
    gaap["PaymentsOfDividendsCommonStock"] = tagdata("USD", [
        dur("2025-07-01", "2025-09-30", 2e9, form="10-Q", accn="q325", filed="2025-11-05")])
    assert build(gaap).pays_dividend is False


def test_annual_cadence_dividend_still_current():
    # filer tags dividends only in the 10-K: a full-year fact ending a quarter ago is current
    gaap = dict(GAAP)
    gaap["PaymentsOfDividendsCommonStock"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 8e9, form="10-K", accn="k25", filed="2026-02-15")])
    assert build(gaap).pays_dividend is True


def test_common_specific_dividend_tag_preferred_over_aggregate():
    gaap = dict(GAAP)
    gaap["PaymentsOfDividends"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 9e9, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)  # GAAP already has PaymentsOfDividendsCommonStock
    assert s.dividend.provenance.tag.endswith("PaymentsOfDividendsCommonStock")
    assert "common stock" in s.dividend.provenance.concept


def test_aggregate_dividend_tag_is_labelled_as_such():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["PaymentsOfDividends"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 9e9, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)
    assert s.pays_dividend is True
    assert "aggregate" in s.dividend.provenance.concept


def test_inbound_dividends_are_not_payer_evidence():
    # dividends RECEIVED from equity-method investees (INTC/BRK pattern) must not
    # soften a non-payer's confident False into unknown
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["EquityMethodInvestmentDividendsOrDistributions"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 4e8, form="10-Q", accn="q126", filed="2026-05-05")])
    assert build(gaap).pays_dividend is False


def test_widened_dividend_chain_tag_detected():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["DividendsCommonStockCash"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 2e9, form="10-Q", accn="q126", filed="2026-05-05")])
    assert build(gaap).pays_dividend is True


def test_foreign_ifrs_filer_requires_an_exact_cover_security():
    facts = {"facts": {"ifrs-full": {
        "Assets": tagdata("USD", [inst("2025-12-31", 1e9, form="20-F")])
    }}}
    with pytest.raises(UnsupportedFilerError, match="security title"):
        build_snapshot("IFRS", "0000000003", facts)


def test_historical_debt_does_not_block_a_current_filing_silence_opt_in():
    # Retired historical borrowings do not prove a current balance.
    gaap = dict(GAAP)
    gaap["UnsecuredDebt"] = tagdata("USD", [inst("2018-12-31", 500e6, form="10-K", accn="k18", filed="2019-02-15")])
    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert "debt" in s.assumed_zero


def test_assume_zero_blocked_by_current_annual_debt_evidence():
    gaap = dict(GAAP)
    gaap["UnsecuredDebt"] = tagdata("USD", [
        inst("2025-12-31", 500e6, form="10-K", accn="k25", filed="2026-02-15")])
    s = build_snapshot("TEST", "0000000001", facts_doc(gaap), assume_absent_zero=True)
    assert "debt" not in s.assumed_zero


def test_fiscal_year_label_january_end_belongs_to_prior_year():
    assert _fy_label(date(2026, 1, 31)) == 2025
    assert _fy_label(date(2025, 9, 27)) == 2025


def test_pretax_income_never_replaces_reported_owner_earnings():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
        tagdata("USD", [dur("2025-01-01", "2025-12-31", 90e9, accn="k25", filed="2026-02-15")])
    oe = build(gaap).owner_earnings
    assert float(oe.all_capex_floor.value) == 70e9  # 70 reported + 12 D&A - 12 capex
    assert not any("pre-tax" in c for c in oe.caveats)


def test_income_tax_is_not_deducted_twice_from_reported_net_income():
    """Net income is already after tax; changing the tax line must not change this lens."""
    gaap = dict(OE_GAAP)
    gaap["IncomeTaxExpenseBenefit"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", -20e9, accn="k25", filed="2026-02-15")])
    oe = build(gaap).owner_earnings
    assert float(oe.all_capex_floor.value) == 70e9


def test_dividend_record_streak_and_interruption():
    gaap = dict(GAAP)
    quarters = []
    for year in (2020, 2021, 2023, 2024, 2025):     # skipped 2022 entirely
        quarters.append(dur(f"{year}-01-01", f"{year}-03-31", 1e8, form="10-Q",
                            accn=f"q{year}", filed=f"{year}-05-05"))
    gaap["PaymentsOfDividendsCommonStock"] = tagdata("USD", quarters)
    s = build(gaap)
    assert s.dividend_record == {"first": 2020, "latest": 2025,
                                 "streak_from": 2023, "paid_years": 5}


def test_legacy_dividend_payable_settlement_is_not_a_current_dividend_pfho_style():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["PaymentsOfDividends"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 37_000, accn="k25", filed="2026-03-13")])
    gaap["DividendsCash"] = tagdata("USD", [
        dur("2023-01-01", "2023-03-31", 1_000, form="10-Q", accn="q23",
            filed="2023-05-01"),
        dur("2024-01-01", "2024-03-31", 1_000, form="10-Q", accn="q24",
            filed="2024-05-01"),
        dur("2024-01-01", "2024-12-31", 0, accn="k24", filed="2025-03-14"),
        dur("2025-01-01", "2025-12-31", 750, accn="k25", filed="2026-03-13"),
    ])
    gaap["DividendsPayableCurrent"] = tagdata("USD", [
        inst("2024-12-31", 37_000, form="10-K", accn="k25", filed="2026-03-13"),
        inst("2025-06-30", 36_375, form="10-Q", accn="q25", filed="2025-08-04"),
        inst("2025-12-31", 0, form="10-K", accn="k25", filed="2026-03-13"),
    ])
    s = build(gaap)
    assert s.pays_dividend is False
    assert s.dividend is None
    assert s.dividend_record == {"first": 2023, "latest": 2023,
                                 "streak_from": 2023, "paid_years": 1}


def test_lp_distributions_count_as_the_common_payout_epd_style():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["DistributionMadeToLimitedPartnerCashDistributionsPaid"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 1.2e9, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)
    assert s.pays_dividend is True
    assert "common" in s.dividend.provenance.concept


def test_partners_capital_distributions_carry_the_aggregate_label_mplx_style():
    # the equity-statement total includes GP/IDR holders: disclosed, not "common"
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["PartnersCapitalAccountDistributions"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 1.02e9, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)
    assert s.pays_dividend is True
    assert "aggregate" in s.dividend.provenance.concept


def test_distribution_named_tag_is_evidence_for_unknown_not_fail():
    gaap = {k: v for k, v in GAAP.items() if k != "PaymentsOfDividendsCommonStock"}
    gaap["DistributionsMade"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 5e8, form="10-Q", accn="q126", filed="2026-05-05")])
    assert build(gaap).pays_dividend is None  # unknown, never a false FAIL


def test_one_time_gain_is_disclosed_with_flipped_wording():
    gaap = dict(GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
        tagdata("USD", [dur("2026-01-01", "2026-03-31", 1e9, form="10-Q", accn="q126", filed="2026-05-05")])
    gaap["GainLossOnInvestments"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 300e6, form="10-Q", accn="q126", filed="2026-05-05")])
    s = build(gaap)
    note = next(n["text"] for n in s.earnings_quality if "investment gain" in n["text"].lower())
    assert "added to" in note  # positive gain BOOSTS income — opposite of a charge


def test_additional_sale_gain_tags_use_the_same_earnings_quality_guard():
    cases = (
        ("GainOnSaleOfInvestments", "investment sale gain"),
        ("GainLossOnSaleOfProperty", "property disposal gain"),
        ("GainOrLossOnSaleOfStockInSubsidiary", "subsidiary stock sale gain"),
    )
    for tag, label in cases:
        gaap = dict(GAAP)
        gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
            tagdata("USD", [dur("2026-01-01", "2026-03-31", 1e9, form="10-Q", accn="q126", filed="2026-05-05")])
        gaap[tag] = tagdata("USD", [
            dur("2026-01-01", "2026-03-31", 300e6, form="10-Q", accn="q126", filed="2026-05-05")])

        note = next(n["text"] for n in build(gaap).earnings_quality
                    if label in n["text"].lower())

        assert "added to" in note


def test_debt_conversion_and_inflation_effects_are_earnings_quality_inputs():
    gaap = dict(GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
        tagdata("USD", [dur("2026-01-01", "2026-03-31", 1e9,
                            form="10-Q", accn="q126", filed="2026-05-05")])
    gaap["InducedConversionOfConvertibleDebtExpense"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 250e6,
            form="10-Q", accn="q126", filed="2026-05-05")])
    gaap["AmountRecognizedInIncomeDueToInflationaryAccounting"] = tagdata("USD", [
        dur("2026-01-01", "2026-03-31", 150e6,
            form="10-Q", accn="q126", filed="2026-05-05")])

    notes = [n["text"] for n in build(gaap).earnings_quality]
    conversion = next(n for n in notes if "debt-conversion" in n.lower())
    inflation = next(n for n in notes if "inflationary" in n.lower())
    assert "reduced pre-tax income" in conversion
    assert "direction the filing's sign convention cannot settle" in inflation


def test_other_nonrecurring_expense_successor_is_visible_without_duplication():
    gaap = dict(GAAP)
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"] = \
        tagdata("USD", [dur("2026-01-01", "2026-03-31", 200e6,
                            form="10-Q", accn="q126", filed="2026-05-05")])
    current = dur("2026-01-01", "2026-03-31", 39.4e6,
                  form="10-Q", accn="q126", filed="2026-05-05")
    gaap["OtherNonrecurringExpense"] = tagdata("USD", [current])
    notes = [n["text"] for n in build(gaap).earnings_quality
             if "nonrecurring" in n["text"].lower()]
    assert len(notes) == 1 and "reduced pre-tax income" in notes[0]

    # BRKR tags one printed rollup under both predecessor and successor. Equal
    # facts are one economic event and must remain one warning.
    gaap["OtherNonrecurringIncomeExpense"] = tagdata("USD", [current])
    notes = [n["text"] for n in build(gaap).earnings_quality
             if "nonrecurring" in n["text"].lower()]
    assert len(notes) == 1


def test_afs_successor_tag_is_the_fragment_never_the_total_pfe_style():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "ShortTermInvestments"}
    gaap["OtherShortTermInvestments"] = tagdata("USD", [
        inst("2024-12-31", 12454e6, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 12454e6, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 12454e6, accn="q126"),
    ])
    gaap["AvailableForSaleSecuritiesDebtSecuritiesCurrent"] = tagdata("USD", [
        inst("2024-12-31", 9183e6, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 9183e6, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 9183e6, accn="q126"),
    ])
    s = build(gaap)
    # invested capital = 1000e9 assets - 40e9 cash - 12.454e9 investments - 150e9 nibcl
    assert float(s.owner_earnings.invested_capital) == 1000e9 - 40e9 - 12454e6 - 150e9

    # with no earlier chain member, the successor tag finally serves (AGIO)
    del gaap["OtherShortTermInvestments"]
    s = build(gaap)
    assert float(s.owner_earnings.invested_capital) == 1000e9 - 40e9 - 9183e6 - 150e9


def test_restricted_cash_netted_only_from_the_inclusive_rollup_aal_style():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "CashAndCashEquivalentsAtCarryingValue"}
    gaap["CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"] = \
        tagdata("USD", [
            inst("2024-12-31", 40e9, form="10-K", accn="k24", filed="2025-02-15"),
            inst("2025-12-31", 40e9, form="10-K", accn="k25", filed="2026-02-15"),
            inst("2026-03-31", 40e9, accn="q126"),
        ])
    gaap["RestrictedCash"] = tagdata("USD", [
        inst("2024-12-31", 3e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 3e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 3e9, accn="q126"),
    ])
    s = build(gaap)
    assert float(s.owner_earnings.invested_capital) == 1000e9 - 37e9 - 150e9
    assert any("restricted" in c for c in s.owner_earnings.caveats)

    # plain carrying-value tag: never netted
    s = build(OE_GAAP | {"RestrictedCash": tagdata("USD", [
        inst("2024-12-31", 3e9, form="10-K", accn="k24", filed="2025-02-15"),
        inst("2025-12-31", 3e9, form="10-K", accn="k25", filed="2026-02-15"),
        inst("2026-03-31", 3e9, accn="q126"),
    ])})
    assert float(s.owner_earnings.invested_capital) == 810e9


def test_segment_capex_fills_only_missing_years_schl_style():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "PaymentsToAcquirePropertyPlantAndEquipment"}
    gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 10e9, accn="k24", filed="2025-02-15")])
    gaap["SegmentExpenditureAdditionToLongLivedAssets"] = tagdata("USD", [
        dur("2024-01-01", "2024-12-31", 99e9, accn="k24", filed="2025-02-15"),  # loses to payments
        dur("2025-01-01", "2025-12-31", 12e9, accn="k25", filed="2026-02-15")])  # fills the dead year
    s = build(gaap)
    # latest shared year 2025 uses the segment figure: 70 + 12 - 12
    assert float(s.owner_earnings.all_capex_floor.value) == 70e9


def test_context_notes_flag_weak_cash_conversion():
    gaap = dict(GAAP)
    gaap["NetIncomeLoss"] = tagdata("USD", [
        dur(f"{y}-01-01", f"{y}-12-31", 1000e6, accn=f"k{y}", filed=f"{y+1}-02-15")
        for y in (2023, 2024, 2025)])
    gaap["NetCashProvidedByUsedInOperatingActivities"] = tagdata("USD", [
        dur(f"{y}-01-01", f"{y}-12-31", 400e6, accn=f"k{y}", filed=f"{y+1}-02-15")
        for y in (2023, 2024, 2025)])
    note = next(n for n in texts(build(gaap)) if "operating cash" in n)
    assert "40%" in note


def test_context_notes_flag_thin_interest_cover():
    gaap = dict(GAAP)
    gaap["OperatingIncomeLoss"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 200e6, accn="k25", filed="2026-02-15")])
    gaap["InterestExpense"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 100e6, accn="k25", filed="2026-02-15")])
    note = next(n for n in texts(build(gaap)) if "covers interest" in n)
    assert "2.0x" in note


def test_domestic_pretax_never_overrides_reported_owner_earnings():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 30e9, accn="k25", filed="2026-02-15")])
    oe = build(gaap).owner_earnings
    assert float(oe.maintenance_estimate.value) == 70e9


def test_geographic_pretax_sum_never_overrides_reported_owner_earnings():
    gaap = {k: v for k, v in OE_GAAP.items() if k != "OperatingIncomeLoss"}
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 30e9, accn="k25", filed="2026-02-15")])
    gaap["IncomeLossFromContinuingOperationsBeforeIncomeTaxesForeign"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 70e9, accn="k25", filed="2026-02-15")])
    oe = build(gaap).owner_earnings
    assert float(dict(oe.components)["reported earnings available to common"]) == 70e9
    assert not any("domestic and foreign" in c for c in oe.caveats)


def test_receivables_outrunning_sales_are_stated():
    """Revenue booked and not collected looks like growth until it is written off."""
    gaap = dict(GAAP)
    gaap["Revenues"] = _years("Revenues", "USD", {2022: 1000e6, 2023: 1050e6,
                                                  2024: 1100e6, 2025: 1150e6})
    gaap["AccountsReceivableNetCurrent"] = _years(
        "AccountsReceivableNetCurrent", "USD",
        {2022: 100e6, 2023: 140e6, 2024: 190e6, 2025: 260e6}, instant=True)
    note = next(n for n in texts(build(gaap)) if n.startswith("Receivables"))
    assert "160%" in note and "15%" in note        # receivables +160%, sales +15%


def test_inventory_in_line_with_sales_says_nothing():
    gaap = dict(GAAP)
    gaap["Revenues"] = _years("Revenues", "USD", {2022: 1000e6, 2023: 1100e6,
                                                  2024: 1200e6, 2025: 1300e6})
    gaap["InventoryNet"] = _years("InventoryNet", "USD",
                                  {2022: 200e6, 2023: 220e6, 2024: 240e6, 2025: 260e6},
                                  instant=True)
    assert not any(n.startswith("Inventory") for n in texts(build(gaap)))


def test_lease_obligations_are_disclosed_beside_the_debt_test_that_ignores_them():
    # GAAP's equity is 1000bn - 400bn = 600bn, so the lease must be large to speak
    gaap = dict(GAAP)
    gaap["OperatingLeaseLiability"] = tagdata("USD", [inst("2026-03-31", 200e9, accn="q126")])
    note = next(n for n in texts(build(gaap)) if "lease obligations" in n)
    assert "200,000M" in note and "long-term debt" in note


def test_lease_cost_and_fixed_charge_proxy_keep_one_reported_period():
    gaap = dict(OE_GAAP)
    gaap["OperatingLeaseLiability"] = tagdata("USD", [
        inst("2026-03-31", 200e9, accn="q126")])
    gaap["OperatingLeaseCost"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 8e9, accn="k25", filed="2026-02-15")])
    gaap["InterestExpense"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 2e9, accn="k25", filed="2026-02-15")])
    snapshot = build(gaap)
    assert float(snapshot.operating_lease_liability.value) == 200e9
    assert float(snapshot.lease_cost.value) == 8e9
    assert float(snapshot.fixed_charge_coverage.value) == pytest.approx(10.8)
    assert len(snapshot.fixed_charge_coverage.provenance.components) == 3


def test_a_lease_book_that_is_trivial_to_the_company_says_nothing():
    """NVIDIA's leases are more than a quarter of its borrowings and 2% of its
    equity; against a balance sheet that size the obligation tells a reader
    nothing."""
    gaap = dict(GAAP)
    gaap["OperatingLeaseLiability"] = tagdata("USD", [inst("2026-03-31", 4.3e9, accn="q126")])
    assert not any("lease obligations" in n for n in texts(build(gaap)))


def test_debt_sold_below_face_costs_more_than_its_coupon_says():
    """NVF's debentures paid 5% and sold at 43% of par. Where amortised discount
    is most of the interest bill, the coupon is not the cost of the money."""
    gaap = dict(GAAP)
    gaap["InterestExpense"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 100e6, accn="k25", filed="2026-02-15")])
    gaap["AmortizationOfDebtDiscountPremium"] = tagdata("USD", [
        dur("2025-01-01", "2025-12-31", 60e6, accn="k25", filed="2026-02-15")])
    note = next(n for n in texts(build(gaap)) if "amortised debt discount" in n)
    assert "60%" in note


def test_a_full_valuation_allowance_beside_profits_is_disclosed():
    gaap = deferred_tax_gaap(
        DeferredTaxAssetsGross=[k25("2025-12-31", 60e9)],
        DeferredTaxAssetsValuationAllowance=[k25("2025-12-31", 45e9)],
    )
    note = next(n for n in texts(build(gaap)) if "valuation allowance" in n)
    assert "75%" in note and "1,000M" in note


def test_valaris_reserves_more_than_the_gross_tag_admits_to():
    """VAL tags a 1,368M 'gross' deferred tax asset and a 3,292M allowance
    against it. An allowance cannot exceed what it reserves against, so the two
    tags are not describing one thing and neither is reported."""
    gaap = deferred_tax_gaap(
        DeferredTaxAssetsGross=[k25("2025-12-31", 1368e6)],
        DeferredTaxAssetsValuationAllowance=[k25("2025-12-31", 3292e6)],
    )
    assert not any("valuation allowance" in n for n in texts(build(gaap)))


def test_the_net_tag_rebuilds_a_base_the_gross_tag_cannot_supply():
    """Biogen's gross tag stopped in 2021 while its allowance is current. Net
    plus allowance is the same balance sheet's own arithmetic."""
    gaap = deferred_tax_gaap(
        DeferredTaxAssetsGross=[inst("2021-06-30", 945e6, form="10-K", accn="k21",
                                     filed="2021-08-01")],
        DeferredTaxAssetsNet=[k25("2025-12-31", 30e9)],
        DeferredTaxAssetsValuationAllowance=[k25("2025-12-31", 40e9)],
    )
    note = next(n for n in texts(build(gaap)) if "valuation allowance" in n)
    assert "70,000M" in note and "57%" in note


def test_a_loss_making_filer_gets_no_allowance_note():
    """A company with no profits is expected to reserve its tax assets; the
    contradiction only exists when it is earning money and still reserving."""
    gaap = deferred_tax_gaap(
        DeferredTaxAssetsGross=[k25("2025-12-31", 60e9)],
        DeferredTaxAssetsValuationAllowance=[k25("2025-12-31", 45e9)],
    )
    annual_10k("NetIncomeLoss", {2024: -500e6, 2025: -900e6}, gaap)
    assert not any("valuation allowance" in n for n in texts(build(gaap)))


def test_a_tax_charge_that_was_not_paid_is_disclosed():
    """Service Corp's FY2025 charge of 191M against 171M reported: the current
    half was a refund."""
    gaap = deferred_tax_gaap()
    annual_10k("IncomeTaxExpenseBenefit", {2025: 170.9e6}, gaap)
    annual_10k("DeferredIncomeTaxExpenseBenefit", {2025: 191.5e6}, gaap)
    note = next(n for n in texts(build(gaap)) if "deferred" in n and "income tax charge" in n)
    assert "112%" in note and "21M refund" in note


def test_a_tax_footnote_older_than_the_earnings_record_is_not_reported():
    gaap = deferred_tax_gaap()
    annual_10k("IncomeTaxExpenseBenefit", {2021: 100e6}, gaap)
    annual_10k("DeferredIncomeTaxExpenseBenefit", {2021: 95e6}, gaap)
    assert not any("income tax charge" in n for n in texts(build(gaap)))


def test_a_deferred_amount_immaterial_to_earnings_says_nothing():
    gaap = deferred_tax_gaap()
    annual_10k("IncomeTaxExpenseBenefit", {2025: 10e6}, gaap)
    annual_10k("DeferredIncomeTaxExpenseBenefit", {2025: 9e6}, gaap)   # 0.9% of net income
    assert not any("income tax charge" in n for n in texts(build(gaap)))


def test_a_preferred_that_already_converted_raises_no_warning():
    """Company Facts drops the preferred count because it is filed on a share-class
    axis; the DERA datasets keep the axis. Structure Therapeutics' 67.0M is its IPO
    conversion, already inside the common count — nothing to disclose."""
    s = build_snapshot("TEST", "0000000001", facts_doc(CONVERTING),
                       dimensioned=_dimensioned([
                           inst("2026-03-31", 0, form="10-K", accn="k25")
                           | {"segments": "ClassOfStock=SeriesAConvertiblePreferred;"}]))
    assert not any(n["kind"] == "Convertible preferred" for n in s.context_notes)


def test_a_live_preferred_is_named_with_what_is_outstanding():
    """Every class is summed rather than chosen: whether the conversion is still
    ahead of the company does not depend on which series it sits in."""
    s = build_snapshot("TEST", "0000000001", facts_doc(CONVERTING),
                       dimensioned=_dimensioned([
                           inst("2026-03-31", 30000, form="10-K", accn="k25")
                           | {"segments": "ClassOfStock=SeriesA;"},
                           inst("2026-03-31", 1333, form="10-K", accn="k25")
                           | {"segments": "ClassOfStock=SeriesB;"}]))
    note = next(n for n in s.context_notes if n["kind"] == "Convertible preferred")
    assert "31,333 preferred shares are still outstanding" in note["text"]


def test_a_total_beside_its_own_classes_is_not_added_to_them():
    """A filer that tags both the rollup and the parts would otherwise double."""
    s = build_snapshot("TEST", "0000000001", facts_doc(CONVERTING),
                       dimensioned=_dimensioned([
                           inst("2026-03-31", 31333, form="10-K", accn="k25"),
                           inst("2026-03-31", 30000, form="10-K", accn="k25")
                           | {"segments": "ClassOfStock=SeriesA;"},
                           inst("2026-03-31", 1333, form="10-K", accn="k25")
                           | {"segments": "ClassOfStock=SeriesB;"}]))
    note = next(n for n in s.context_notes if n["kind"] == "Convertible preferred")
    assert "31,333 preferred shares are still outstanding" in note["text"]


def test_no_preferred_count_on_file_says_so_rather_than_guessing():
    """The filers DERA does not reach keep the weaker note, not a wrong answer."""
    s = build(CONVERTING)
    note = next(n for n in s.context_notes if n["kind"] == "Convertible preferred")
    assert "no preferred count is on file" in note["text"]


def test_the_series_rule_ranks_meaning_above_depth():
    """Written three times with the three keys in three different orders, two of them
    wrong. Recency first, so an abandoned element cannot answer for today; then what
    the element MEANS; then how much of it there is. Starwood files sixteen years of
    the group's profit beside fourteen of its own, and ranking depth second let two
    extra years swap one concept for the other."""
    from screener.normalize import _best_series
    parent = {y: y for y in range(2012, 2026)}          # 14 years, preferred concept
    group = {y: y for y in range(2010, 2026)}           # 16 years, wrong concept
    order = {"NetIncomeLoss": 0, "ProfitLoss": 2}
    tag, _ = _best_series([("NetIncomeLoss", parent), ("ProfitLoss", group)], order)
    assert tag == "NetIncomeLoss"

    # ...but a dead series of the right concept still yields to a live one
    stopped = {y: y for y in range(2012, 2024)}
    tag, _ = _best_series([("NetIncomeLoss", stopped), ("ProfitLoss", group)], order)
    assert tag == "ProfitLoss"


def test_a_preferred_concept_outranks_the_tag_order_itself():
    """§5.3 prefers continuing operations, which is a statement about scope rather
    than about which element a filer happens to use."""
    from screener.normalize import _best_series
    a = {y: y for y in range(2012, 2026)}
    b = {y: y for y in range(2012, 2026)}
    tag, _ = _best_series([("first", a), ("second", b)], {"first": 0, "second": 1},
                          prefer=lambda t: t == "second")
    assert tag == "second"
