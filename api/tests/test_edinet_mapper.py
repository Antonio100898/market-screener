from decimal import Decimal

import pytest

from screener.normalize import build_snapshot
from screener.sources import edinet_mapper


def _record(document, entity, security):
    return {
        "docID": document,
        "edinetCode": entity,
        "secCode": security,
        "docTypeCode": "120",
        "xbrlFlag": "1",
        "submitDateTime": "2026-06-19 15:30",
        "periodStart": "2025-04-01",
        "periodEnd": "2026-03-31",
    }


def _fact(namespace, tag, value, *, unit="JPY", start="2025-04-01",
          end="2026-03-31", dimensions=None, document="S100YETA"):
    return {
        "namespace": f"http://disclosure.edinet-fsa.go.jp/taxonomy/{namespace}",
        "tag": tag,
        "value": Decimal(str(value)),
        "unit": unit,
        "start": start,
        "end": end,
        "dimensions": dimensions or [],
        "document_id": document,
        "entity": "E02367-000" if document == "S100Y9NX" else "E01772-000",
        "source_file": "XBRL/PublicDoc/annual.xbrl",
    }


def _entry(bundle, concept, unit="JPY"):
    return bundle["facts"]["canonical"][concept]["units"][unit][0]


def test_missing_period_end_is_an_unsupported_filing(monkeypatch):
    record = _record("S100WK02", "E00001", "11110")
    record["periodEnd"] = None
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: [])
    monkeypatch.setattr(edinet_mapper, "reported_period_end", lambda archive, doc: None)

    with pytest.raises(ValueError, match="no period end"):
        edinet_mapper.build_edinet_companyfacts(record, b"archive", ticker="1111.T")


def test_panasonic_ifrs_concepts_keep_filing_evidence_and_prefer_statement(monkeypatch):
    raw = [
        _fact("jpcrp_cor", "RevenueIFRSSummaryOfBusinessResults", 8048722000000),
        _fact("jpigp_cor", "RevenueIFRS", 8048722000001),
        _fact("jpigp_cor", "DilutedEarningsLossPerShareIFRS", "81.17",
              unit="JPY/shares"),
        _fact("jpigp_cor", "AssetsIFRS", 10172412000000, start=None),
        _fact("jpigp_cor", "RevenueIFRS", 1,
              dimensions=[{"axis": "ConsolidationAxis", "member": "NonConsolidatedMember"}]),
        _fact("jpigp_cor", "UnknownRevenue", 999),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)

    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive", ticker="6752.T")

    revenue = _entry(bundle, "Revenues")
    assert Decimal(revenue["val"]) == Decimal("8048722000001")
    assert revenue["_source_tag"] == "RevenueIFRS"
    assert revenue["_source_namespace"] == "jpigp_cor"
    assert revenue["_normalized_tag"] == "Revenues"
    assert revenue["_source_document"] == revenue["accn"] == "S100YETA"
    assert revenue["_source_unit"] == "JPY"
    assert revenue["_canonical_adapter"] == "edinet_xbrl"
    assert (revenue["start"], revenue["end"], revenue["filed"]) == (
        "2025-04-01", "2026-03-31", "2026-06-19")
    assert Decimal(_entry(bundle, "EarningsPerShareDiluted", "JPY/shares")["val"]) == Decimal("81.17")
    assert Decimal(_entry(bundle, "Assets")["val"]) == Decimal("10172412000000")
    assert "start" not in _entry(bundle, "Assets")
    assert set(bundle["facts"]["canonical"]) == {
        "Revenues", "EarningsPerShareDiluted", "Assets"}
    assert bundle["_adapter"]["reporting_currency"] == "JPY"
    assert bundle["_adapter"]["security_code"] == "67520"
    assert bundle["_adapter"]["reports"][0]["period_end"] == "2026-03-31"
    snapshot = build_snapshot("6752.T", "E01772", bundle)
    assert snapshot.reporting_currency == "JPY"
    assert snapshot.total_assets.value == Decimal("10172412000000")
    assert snapshot.total_assets.provenance.tag == "jpigp_cor:AssetsIFRS"
    assert snapshot.total_assets.provenance.document == "S100YETA"


def test_nintendo_jgaap_known_rows_and_missing_diluted_eps(monkeypatch):
    raw = [
        _fact("jppfs_cor", "NetSales", 2313051000000, document="S100Y9NX"),
        _fact("jpcrp_cor", "BasicEarningsLossPerShareSummaryOfBusinessResults",
              "364.51", unit="JPY/shares", document="S100Y9NX"),
        _fact("jppfs_cor", "Assets", 3805312000000, start=None,
              document="S100Y9NX"),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        {**_record("S100Y9NX", "E02367", "79740"),
         "filerName": "任天堂株式会社", "filerNameEnglish": "Nintendo Co., Ltd."},
        b"archive")
    assert bundle["entityName"] == "Nintendo Co., Ltd."
    assert Decimal(_entry(bundle, "Revenues")["val"]) == Decimal("2313051000000")
    assert Decimal(_entry(bundle, "EarningsPerShareBasic", "JPY/shares")["val"]) == Decimal("364.51")
    assert Decimal(_entry(bundle, "Assets")["val"]) == Decimal("3805312000000")
    assert "EarningsPerShareDiluted" not in bundle["facts"]["canonical"]


def test_mixed_currency_and_dimensioned_balance_do_not_define_statement(monkeypatch):
    raw = [
        _fact("jpigp_cor", "AssetsIFRS", 100, start=None,
              dimensions=[{"axis": "ConsolidationAxis", "member": "NonConsolidatedMember"}]),
        _fact("jpcrp_cor", "RevenueIFRSSummaryOfBusinessResults", 200),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    with pytest.raises(ValueError, match="balance currency"):
        edinet_mapper.build_edinet_companyfacts(
            _record("S100YETA", "E01772", "67520"), b"archive")

    raw[0]["dimensions"] = []
    raw.append(_fact("jpigp_cor", "RevenueIFRS", 300, unit="USD"))
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    assert list(bundle["facts"]["canonical"]["Revenues"]["units"]) == ["JPY"]


def test_other_entity_context_cannot_supply_the_reported_balance(monkeypatch):
    foreign_balance = _fact("jpigp_cor", "AssetsIFRS", 100, start=None)
    foreign_balance["entity"] = "E99999-000"
    monkeypatch.setattr(edinet_mapper, "xbrl_facts",
                        lambda archive, doc: [foreign_balance])
    with pytest.raises(ValueError, match="balance currency"):
        edinet_mapper.build_edinet_companyfacts(
            _record("S100YETA", "E01772", "67520"), b"archive")


def test_complete_corrected_report_keeps_its_own_filing_form(monkeypatch):
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: [
        _fact("jpigp_cor", "AssetsIFRS", 10172412000000, start=None)])
    monkeypatch.setattr(edinet_mapper, "reported_period_end",
                        lambda archive, doc: "2026-03-31")
    record = {**_record("S100YETA", "E01772", "67520"), "docTypeCode": "130",
              "periodStart": None, "periodEnd": None, "parentDocID": "S100Y000"}
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"archive")
    assert _entry(bundle, "Assets")["form"] == "JP-AR/A"
    assert _entry(bundle, "Assets")["end"] == "2026-03-31"
    assert bundle["_adapter"]["reports"][0]["corrects"] == "S100Y000"


def test_ifrs_summary_is_used_only_when_direct_row_is_missing(monkeypatch):
    raw = [
        _fact("jpcrp_cor", "RevenueIFRSSummaryOfBusinessResults", 8048722000000),
        _fact("jpigp_cor", "AssetsIFRS", 10172412000000, start=None),
        _fact("jpcrp_cor", "BasicEarningsLossPerShareSummaryOfBusinessResults",
              999, unit="JPY/shares"),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    assert _entry(bundle, "Revenues")["_source_namespace"] == "jpcrp_cor"
    assert Decimal(_entry(bundle, "Revenues")["val"]) == Decimal("8048722000000")
    assert "EarningsPerShareBasic" not in bundle["facts"]["canonical"]


def test_five_year_summary_eps_remains_on_reported_fiscal_periods(monkeypatch):
    raw = [_fact("jpigp_cor", "AssetsIFRS", 10172412000000, start=None)]
    for year, eps in [(2022, "109.37"), (2023, "113.72"),
                      (2024, "190.15"), (2025, "156.83"), (2026, "81.17")]:
        raw.append(_fact(
            "jpcrp_cor", "DilutedEarningsLossPerShareIFRSSummaryOfBusinessResults",
            eps, unit="JPY/shares", start=f"{year-1}-04-01", end=f"{year}-03-31"))
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    entries = bundle["facts"]["canonical"]["EarningsPerShareDiluted"]["units"]["JPY/shares"]
    assert [entry["end"] for entry in entries] == [f"{year}-03-31" for year in range(2022, 2027)]
    assert [Decimal(entry["val"]) for entry in entries] == [
        Decimal(value) for value in ("109.37", "113.72", "190.15", "156.83", "81.17")]


def test_observed_ifrs_statement_rows_map_without_inventing_missing_fields(monkeypatch):
    balances = {
        "AssetsIFRS": ("Assets", 1000),
        "CurrentAssetsIFRS": ("AssetsCurrent", 400),
        "TotalCurrentLiabilitiesIFRS": ("LiabilitiesCurrent", 200),
        "LiabilitiesIFRS": ("Liabilities", 600),
        "EquityAttributableToOwnersOfParentIFRS": ("StockholdersEquity", 350),
        "EquityIFRS": (
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 400),
        "CashAndCashEquivalentsIFRS": ("CashAndCashEquivalentsAtCarryingValue", 120),
        "InventoriesCAIFRS": ("InventoryNet", 80),
        "GoodwillIFRS": ("Goodwill", 30),
        "IntangibleAssetsIFRS": ("IntangibleAssetsNetExcludingGoodwill", 20),
        "ShortTermDebtIncludingCurrentPortionOfLongTermDebtCLIFRS": ("DebtCurrent", 50),
    }
    flows = {
        "OperatingProfitLossIFRS": ("OperatingIncomeLoss", 90),
        "ProfitLossAttributableToOwnersOfParentIFRS": ("NetIncomeLoss", 70),
        "NetCashProvidedByUsedInOperatingActivitiesIFRS": (
            "NetCashProvidedByUsedInOperatingActivities", 110),
        "DepreciationAndAmortizationOpeCFIFRS": ("DepreciationAndAmortization", 25),
        "DividendsPaidToOwnersOfParentFinCFIFRS": ("PaymentsOfOrdinaryDividends", -15),
    }
    raw = [
        *(_fact("jpigp_cor", source, value, start=None)
          for source, (_, value) in balances.items()),
        *(_fact("jpigp_cor", source, value)
          for source, (_, value) in flows.items()),
        _fact("jpigp_cor", "BasicEarningsLossPerShareIFRS", "82.00",
              unit="JPY/shares"),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    for source, (canonical, value) in {**balances, **flows}.items():
        entry = _entry(bundle, canonical)
        assert entry["_source_tag"] == source
        expected = abs(value) if canonical == "PaymentsOfOrdinaryDividends" else value
        assert Decimal(entry["val"]) == expected
    assert Decimal(_entry(bundle, "EarningsPerShareBasic", "JPY/shares")["val"]) == Decimal("82.00")
    dividend = _entry(bundle, "PaymentsOfOrdinaryDividends")
    assert dividend["_source_concept"].endswith("source signed negative)")
    snapshot = build_snapshot("6752.T", "E01772", bundle)
    assert snapshot.current_assets.value == 400
    assert snapshot.current_liabilities.value == 200
    assert snapshot.total_liabilities.value == 600
    assert snapshot.short_term_debt.value == 50


def test_observed_jgaap_rows_without_parent_equity_rows(monkeypatch):
    balances = {
        "Assets": ("Assets", 1000),
        "CurrentAssets": ("AssetsCurrent", 400),
        "CurrentLiabilities": ("LiabilitiesCurrent", 200),
        "Liabilities": ("Liabilities", 600),
        "Inventories": ("InventoryNet", 80),
    }
    flows = {
        "NetSales": ("Revenues", 500),
        "OperatingIncome": ("OperatingIncomeLoss", 90),
        "ProfitLossAttributableToOwnersOfParent": ("NetIncomeLoss", 70),
        "ProfitLoss": ("ProfitLoss", 75),
        "IncomeTaxes": ("IncomeTaxExpenseBenefit", 25),
        "NetCashProvidedByUsedInOperatingActivities": (
            "NetCashProvidedByUsedInOperatingActivities", 110),
        "CashDividendsPaidFinCF": ("PaymentsOfOrdinaryDividends", -15),
    }
    raw = [
        *(_fact("jppfs_cor", source, value, start=None, document="S100Y9NX")
          for source, (_, value) in balances.items()),
        *(_fact("jppfs_cor", source, value, document="S100Y9NX")
          for source, (_, value) in flows.items()),
        _fact("jppfs_cor", "NetAssets", 400, start=None, document="S100Y9NX"),
        _fact("jppfs_cor", "IntangibleAssets", 20, start=None,
              document="S100Y9NX"),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100Y9NX", "E02367", "79740"), b"archive")
    for source, (canonical, value) in {**balances, **flows}.items():
        entry = _entry(bundle, canonical)
        assert entry["_source_tag"] == source
        expected = abs(value) if canonical == "PaymentsOfOrdinaryDividends" else value
        assert Decimal(entry["val"]) == expected
    assert "StockholdersEquity" not in bundle["facts"]["canonical"]
    assert "IntangibleAssetsNetExcludingGoodwill" not in bundle["facts"]["canonical"]
    assert _entry(bundle, "IntangibleAssetsNetIncludingGoodwill")["val"] == "20"


def test_standalone_filer_uses_its_standalone_statements_only(monkeypatch):
    standalone = [{"axis": "jppfs_cor:ConsolidatedOrNonConsolidatedAxis",
                   "member": "jppfs_cor:NonConsolidatedMember"}]
    segment = [{"axis": "jpcrp_cor:OperatingSegmentsAxis", "member": "RetailMember"}]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: [
        _fact("jppfs_cor", "Assets", 5000, start=None, dimensions=standalone),
        _fact("jppfs_cor", "NetSales", 900, dimensions=standalone),
        _fact("jppfs_cor", "NetSales", 400, dimensions=segment),
    ])
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    assert _entry(bundle, "Assets")["val"] == "5000"
    assert _entry(bundle, "Revenues")["val"] == "900"
    assert len(bundle["facts"]["canonical"]["Revenues"]["units"]["JPY"]) == 1
    assert bundle["_adapter"]["statement_scope"] == "standalone"


def test_group_accounts_win_over_parent_standalone_rows(monkeypatch):
    standalone = [{"axis": "jppfs_cor:ConsolidatedOrNonConsolidatedAxis",
                   "member": "jppfs_cor:NonConsolidatedMember"}]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: [
        _fact("jppfs_cor", "Assets", 8000, start=None),
        _fact("jppfs_cor", "Assets", 5000, start=None, dimensions=standalone),
        _fact("jppfs_cor", "NetSales", 300, dimensions=standalone),
    ])
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    assert _entry(bundle, "Assets")["val"] == "8000"
    assert "Revenues" not in bundle["facts"]["canonical"]
    assert bundle["_adapter"]["statement_scope"] == "consolidated"


STANDALONE_AXIS = [{"axis": "jppfs_cor:ConsolidatedOrNonConsolidatedAxis",
                    "member": "jppfs_cor:NonConsolidatedMember"}]


def _voting_rows(issued, **rows):
    """The voting-rights table: an undimensioned issued total and its rows."""
    out = [_fact("jpcrp_cor", "NumberOfSharesIssuedSharesVotingRights", issued,
                 unit="shares", start=None)]
    for member, value in rows.items():
        out.append(_fact("jpcrp_cor", "NumberOfSharesIssuedSharesVotingRights", value,
                         unit="shares", start=None,
                         dimensions=[{"axis": "jpcrp_cor:ClassesOfSharesAxis",
                                      "member": f"jpcrp_cor:{member}"}]))
    return out


def _treasury_holders(*values):
    return [_fact("jpcrp_cor", "TotalNumberOfSharesHeldTreasurySharesEtc", value,
                  unit="shares", start=None,
                  dimensions=[{"axis": "jpcrp_cor:RowsAxis", "member": f"Row{i}Member"}])
            for i, value in enumerate(values, 1)]


def _shares(monkeypatch, raw):
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: [
        _fact("jpigp_cor", "AssetsIFRS", 1000, start=None), *raw])
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    units = bundle["facts"]["canonical"].get("CommonStockSharesOutstanding")
    return units["units"]["shares"][0] if units else None


def test_shares_outstanding_is_issued_less_treasury_at_fiscal_year_end(monkeypatch):
    # Kojima FY2026: 77,912,716 issued, 882,600 held as treasury.
    entry = _shares(monkeypatch, _voting_rows(
        77912716,
        OrdinarySharesTreasurySharesSharesWithFullVotingRightsTreasurySharesEtcMember=882600,
        OrdinarySharesSharesWithFullVotingRightsOtherMember=76984300,
        OrdinarySharesSharesLessThanOneUnitMember=45816,
    ))
    assert Decimal(entry["val"]) == Decimal("77030116")
    assert entry["end"] == "2026-03-31" and entry["_source_unit"] == "shares"
    assert entry["_source_tag"] == (
        "NumberOfSharesIssuedSharesVotingRights - jpcrp_cor:NumberOfSharesIssuedSharesVotingRights"
        "[OrdinarySharesTreasurySharesSharesWithFullVotingRightsTreasurySharesEtcMember]")
    assert "issued 77912716 less treasury 882600" in entry["_source_concept"]


def test_combined_treasury_row_counts_only_for_a_single_holder(monkeypatch):
    rows = dict(OrdinarySharesSharesWithFullVotingRightsTreasurySharesEtcMember=2211600,
                OrdinarySharesSharesWithFullVotingRightsOtherMember=187424700,
                OrdinarySharesSharesLessThanOneUnitMember=1025761)
    single = _shares(monkeypatch, [*_voting_rows(190662061, **rows), *_treasury_holders(2211600)])
    assert Decimal(single["val"]) == Decimal("188450461")
    # Kintetsu: the row adds cross-holdings of two affiliates to its own shares.
    assert _shares(monkeypatch, [*_voting_rows(190662061, **rows),
                                 *_treasury_holders(213700, 1843800, 154100)]) is None


def test_no_treasury_row_is_proven_nil_only_by_the_table_total(monkeypatch):
    rows = dict(OrdinarySharesSharesWithFullVotingRightsOtherMember=999900,
                OrdinarySharesSharesLessThanOneUnitMember=100)
    entry = _shares(monkeypatch, _voting_rows(1000000, **rows))
    assert Decimal(entry["val"]) == Decimal("1000000")
    assert "no treasury row" in entry["_source_concept"]
    assert _shares(monkeypatch, _voting_rows(1000001, **rows)) is None


def test_another_share_class_leaves_the_ordinary_count_unknown(monkeypatch):
    assert _shares(monkeypatch, _voting_rows(
        1000000, OrdinarySharesSharesWithFullVotingRightsOtherMember=900000,
        ClassASharesSharesWithNoVotingRightsMember=100000)) is None


def test_ifrs_debt_noncontrolling_capex_and_dividend_rows(monkeypatch):
    raw = [
        _fact("jpigp_cor", "AssetsIFRS", 1000, start=None),
        _fact("jpigp_cor", "CurrentAssetsIFRS", 500, start=None),
        _fact("jpigp_cor", "TotalCurrentLiabilitiesIFRS", 300, start=None),
        _fact("jpigp_cor", "LiabilitiesIFRS", 600, start=None),
        _fact("jpigp_cor", "BondsAndBorrowingsCLIFRS", 40, start=None),
        _fact("jpigp_cor", "BondsAndBorrowingsNCLIFRS", 150, start=None),
        _fact("jpigp_cor", "LeaseLiabilitiesNCLIFRS", 70, start=None),
        _fact("jpigp_cor", "NonControllingInterestsIFRS", 25, start=None),
        _fact("jpigp_cor", "GoodwillAndIntangibleAssetsIFRS", 60, start=None),
        _fact("jpigp_cor", "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS", -80),
        _fact("jpigp_cor", "PurchaseOfIntangibleAssetsInvCFIFRS", -12),
        _fact("jpigp_cor", "ProfitLossBeforeTaxIFRS", 100),
        _fact("jpigp_cor", "IncomeTaxExpenseIFRS", 30),
        # The summary files the dividend per share on the standalone axis.
        _fact("jpcrp_cor", "DividendPaidPerShareSummaryOfBusinessResults", "50.00",
              unit="JPY/shares", dimensions=STANDALONE_AXIS),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    expected = {
        "DebtCurrent": ("BondsAndBorrowingsCLIFRS", 40),
        "LongTermDebtNoncurrent": ("BondsAndBorrowingsNCLIFRS", 150),
        "MinorityInterest": ("NonControllingInterestsIFRS", 25),
        "IntangibleAssetsNetIncludingGoodwill": ("GoodwillAndIntangibleAssetsIFRS", 60),
        "PaymentsToAcquirePropertyPlantAndEquipment": (
            "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS", 80),
        "PaymentsToAcquireIntangibleAssets": ("PurchaseOfIntangibleAssetsInvCFIFRS", 12),
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
            "ProfitLossBeforeTaxIFRS", 100),
        "IncomeTaxExpenseBenefit": ("IncomeTaxExpenseIFRS", 30),
    }
    for canonical, (source, value) in expected.items():
        entry = _entry(bundle, canonical)
        assert (entry["_source_tag"], Decimal(entry["val"])) == (source, value)
    dividend = _entry(bundle, "CommonStockDividendsPerShareDeclared", "JPY/shares")
    assert Decimal(dividend["val"]) == Decimal("50.00")
    # IFRS lease liabilities are not borrowings, as for ifrs-full filers.
    assert all("Lease" not in tag for tag in bundle["facts"]["canonical"])
    snapshot = build_snapshot("6752.T", "E01772", bundle)
    assert snapshot.short_term_debt.value == 40
    assert snapshot.long_term_debt.value == 150
    assert snapshot.noncontrolling_interest.value == 25
    assert (snapshot.goodwill.value, snapshot.intangibles.value) == (0, 60)


def _jgaap(monkeypatch, rows):
    raw = [_fact("jppfs_cor", tag, value, start=None, document="S100Y9NX")
           for tag, value in rows.items()]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    return edinet_mapper.build_edinet_companyfacts(
        _record("S100Y9NX", "E02367", "79740"), b"archive")


def test_jgaap_borrowings_are_summed_by_instrument(monkeypatch):
    # Kintetsu FY2026 rows (JPY millions), plus commercial paper and convertibles.
    bundle = _jgaap(monkeypatch, {
        "Assets": 2593502, "CurrentAssets": 400000, "CurrentLiabilities": 600000,
        "Liabilities": 1901538,
        "ShortTermLoansPayable": 215402, "CommercialPapersLiabilities": 10000,
        "CurrentPortionOfLongTermLoansPayable": 5, "CurrentPortionOfBonds": 3137,
        "LeaseObligationsCL": 16800,
        "LongTermLoansPayable": 733357, "BondsPayable": 313643,
        "ConvertibleBondTypeBondsWithSubscriptionRightsToShares": 20000,
        "LeaseObligationsNCL": 51398,
    })
    snapshot = build_snapshot("7974.T", "E02367", bundle)
    assert snapshot.short_term_debt.value == 215402 + 10000 + 5 + 3137 + 16800
    assert snapshot.long_term_debt.value == 733357 + 313643 + 20000 + 51398
    assert "jppfs_cor:BondsPayable" in snapshot.long_term_debt.provenance.tag


def test_jgaap_consolidated_parent_equity_and_goodwill(monkeypatch):
    # Kintetsu FY2026 as printed in JPY millions: the rounded parts sum to
    # 691,962 against 691,964 of net assets.
    million = 1_000_000
    rows = {"Assets": 2593502, "Liabilities": 1901538, "NetAssets": 691964,
            "ShareholdersEquity": 456622, "ValuationAndTranslationAdjustments": 155083,
            "NonControllingInterests": 80257, "Goodwill": 49440, "IntangibleAssets": 151896}
    bundle = _jgaap(monkeypatch, {tag: value * million for tag, value in rows.items()})
    equity = _entry(bundle, "StockholdersEquity")
    assert Decimal(equity["val"]) == (456622 + 155083) * million
    assert equity["_source_tag"] == (
        "ShareholdersEquity + jppfs_cor:ValuationAndTranslationAdjustments")
    snapshot = build_snapshot("7974.T", "E02367", bundle)
    assert snapshot.noncontrolling_interest.value == 80257 * million
    assert snapshot.goodwill.value == 49440 * million
    assert snapshot.intangibles.value == (151896 - 49440) * million

    # A difference beyond rounding means a net-asset row the mapping does not
    # know, such as share acquisition rights: no parent equity.
    rows["NetAssets"] = 691964 + 30
    bundle = _jgaap(monkeypatch, {tag: value * million for tag, value in rows.items()})
    assert "StockholdersEquity" not in bundle["facts"]["canonical"]


def test_jgaap_capex_flows_are_outflow_magnitudes(monkeypatch):
    raw = [
        _fact("jppfs_cor", "Assets", 1000, start=None),
        _fact("jppfs_cor", "PurchaseOfPropertyPlantAndEquipmentInvCF", -1939),
        _fact("jppfs_cor", "PurchaseOfIntangibleAssetsInvCF", -120),
        _fact("jppfs_cor", "PurchaseOfNoncurrentAssetsInvCF", -2100),
        _fact("jppfs_cor", "DepreciationAndAmortizationOpeCF", 1575),
        _fact("jppfs_cor", "IncomeBeforeIncomeTaxes", 8000),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    assert _entry(bundle, "PaymentsToAcquirePropertyPlantAndEquipment")["val"] == "1939"
    assert _entry(bundle, "PaymentsToAcquireIntangibleAssets")["val"] == "120"
    assert _entry(bundle, "PaymentsToAcquireProductiveAssets")["val"] == "2100"
    assert _entry(bundle, "DepreciationAndAmortization")["val"] == "1575"
    assert _entry(
        bundle, "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"
    )["val"] == "8000"


def test_standalone_filer_equity_excludes_subscription_rights(monkeypatch):
    # Kojima FY2026 (JPY millions): no accumulated other comprehensive income
    # row; net assets are shareholders equity plus subscription rights.
    raw = [
        *(_fact("jppfs_cor", tag, value, start=None, dimensions=STANDALONE_AXIS)
          for tag, value in {"Assets": 128669, "CurrentAssets": 92972,
                             "CurrentLiabilities": 40027, "Liabilities": 55075,
                             "ShareholdersEquity": 73439,
                             "SubscriptionRightsToShares": 154, "NetAssets": 73593,
                             "IntangibleAssets": 774}.items()),
        _fact("jpcrp_cor", "DividendPaidPerShareSummaryOfBusinessResults", "29.00",
              unit="JPY/shares", dimensions=STANDALONE_AXIS),
        *_voting_rows(
            77912716,
            OrdinarySharesTreasurySharesSharesWithFullVotingRightsTreasurySharesEtcMember=882600,
            OrdinarySharesSharesWithFullVotingRightsOtherMember=76984300,
            OrdinarySharesSharesLessThanOneUnitMember=45816),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"archive")
    equity = _entry(bundle, "StockholdersEquity")
    assert (equity["val"], equity["_source_tag"]) == ("73439", "ShareholdersEquity")
    assert equity["_source_scope"].startswith("standalone")
    assert _entry(bundle, "CommonStockDividendsPerShareDeclared", "JPY/shares")["val"] == "29.00"
    snapshot = build_snapshot("6752.T", "E01772", bundle)
    assert snapshot.shares_outstanding.value == 77030116
    assert snapshot.intangibles.value == 774
    assert snapshot.goodwill.value == 0  # contained in the intangible-assets subtotal


def test_adapter_bundle_absence_never_becomes_an_assumed_zero():
    from screener import normalize

    facts = {"canonical": {"Assets": {"units": {"JPY": [{
        "val": 100, "end": "2026-03-31", "form": "JP-AR", "filed": "2026-06-19"}]}}}}
    assert normalize._absent_zero_candidates(facts) == set()
    from datetime import date
    assert normalize._historical_absent_zero_candidates(facts, date(2026, 3, 31)) == set()


def test_every_tokyo_industry_maps_to_a_screener_sector():
    from screener import sectors

    assert len(sectors.JPX_SECTORS) == 33
    assert set(sectors.JPX_SECTORS.values()) <= set(sectors.all_sectors())
    assert sectors.JPX_SECTORS["Banks"] == "Financials"
