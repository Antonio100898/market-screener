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
    record = {**_record("S100YETA", "E01772", "67520"), "docTypeCode": "130"}
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"archive")
    assert _entry(bundle, "Assets")["form"] == "JP-AR/A"


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
        "DividendsPaidToOwnersOfParentFinCFIFRS": ("PaymentsOfDividendsCommonStock", -15),
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
        expected = abs(value) if canonical == "PaymentsOfDividendsCommonStock" else value
        assert Decimal(entry["val"]) == expected
    assert Decimal(_entry(bundle, "EarningsPerShareBasic", "JPY/shares")["val"]) == Decimal("82.00")
    dividend = _entry(bundle, "PaymentsOfDividendsCommonStock")
    assert dividend["_source_concept"].endswith("source signed negative)")
    snapshot = build_snapshot("6752.T", "E01772", bundle)
    assert snapshot.current_assets.value == 400
    assert snapshot.current_liabilities.value == 200
    assert snapshot.total_liabilities.value == 600
    assert snapshot.short_term_debt.value == 50


def test_observed_jgaap_rows_keep_net_assets_and_intangibles_unmapped(monkeypatch):
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


def _s100z3o7_cashflow_rows():
    # Native S100Z3O7 archive SHA4c0a0a9e374faa5dea7af0b746282077f2cdef59eb6f927098e5c08d62bf68e0.
    member = "XBRL/PublicDoc/jpcrp030000-asr-001_E00009-000_2026-06-30_01_2026-09-24.xbrl"
    rows = [_fact("jppfs_cor", "Assets", "28461000000", start=None,
                  end="2026-06-30", document="S100Z3O7")]
    values = {
        "ProfitLossAttributableToOwnersOfParent": ("1720000000", "2925000000"),
        "NetCashProvidedByUsedInOperatingActivities": ("2915000000", "4885000000"),
        "DepreciationAndAmortizationOpeCF": ("1771000000", "1815000000"),
        "PurchaseOfPropertyPlantAndEquipmentInvCF": ("-2026000000", "-1521000000"),
        "PurchaseOfIntangibleAssetsInvCF": ("-19000000", "-0"),
    }
    for tag, amounts in values.items():
        for (start, end), amount in zip(
                (("2024-07-01", "2025-06-30"), ("2025-07-01", "2026-06-30")), amounts):
            rows.append(_fact("jppfs_cor", tag, amount, start=start, end=end,
                              document="S100Z3O7"))
    for row in rows:
        row.update(namespace="http://disclosure.edinet-fsa.go.jp/taxonomy/jppfs/2025-11-01/jppfs_cor",
                   entity="E00009-000", source_file=member)
    record = {**_record("S100Z3O7", "E00009", "13810"),
              "submitDateTime": "2026-09-24", "periodStart": "2025-07-01",
              "periodEnd": "2026-06-30"}
    return record, rows


def test_retained_jgaap_cash_purchases_and_depreciation_keep_separate_source_rows(monkeypatch):
    record, raw = _s100z3o7_cashflow_rows()
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"fixture", ticker="1381.T")
    expected = {
        "DepreciationAndAmortization": ("DepreciationAndAmortizationOpeCF", ("1771000000", "1815000000")),
        "PaymentsToAcquirePropertyPlantAndEquipment": ("PurchaseOfPropertyPlantAndEquipmentInvCF", ("2026000000", "1521000000")),
        "PaymentsToAcquireIntangibleAssets": ("PurchaseOfIntangibleAssetsInvCF", ("19000000", "0")),
    }
    for tag, (source_tag, values) in expected.items():
        entries = bundle["facts"]["canonical"][tag]["units"]["JPY"]
        assert [entry["val"] for entry in entries] == list(values)
        assert [(entry["start"], entry["end"]) for entry in entries] == [
            ("2024-07-01", "2025-06-30"), ("2025-07-01", "2026-06-30")]
        for entry in entries:
            assert entry["_source_tag"] == source_tag
            assert entry["_source_namespace"] == "jppfs_cor"
            assert entry["_source_unit"] == "JPY"
            assert entry["_source_document"] == entry["accn"] == "S100Z3O7"
            assert entry["_source_file"] == raw[0]["source_file"]
            assert entry["filed"] == "2026-09-24" and entry["form"] == "JP-AR"
        if tag.startswith("Payments"):
            assert entries[0]["_source_concept"].endswith("source signed negative)")
    snapshot = build_snapshot("1381.T", "E00009", bundle)
    owner = snapshot.owner_earnings
    assert owner is not None
    assert owner.annual[2025].free_cash_flow.value == Decimal("889000000")
    assert owner.annual[2025].capitalized_intangible_investment.value == Decimal("19000000")
    assert owner.annual[2025].expanded_free_cash_flow.value == Decimal("870000000")
    assert owner.annual[2026].free_cash_flow.value == Decimal("3364000000")
    assert owner.annual[2026].capitalized_intangible_investment.value == 0
    assert owner.annual[2025].diluted_shares is None
    assert owner.annual[2025].free_cash_flow_per_share is None

    from screener.sync import _owner_earnings_row

    bridge = _owner_earnings_row(snapshot)["annual_per_share"]["2025"]["cash_flow_bridge"]
    capex = bridge["total_capital_expenditure"]
    fact = owner.annual[2025].total_capital_expenditure
    assert capex[:7] == [2026000000.0, "jppfs_cor:PurchaseOfPropertyPlantAndEquipmentInvCF",
                         "JP-AR", "S100Z3O7", "2025-06-30", "JPY", "S100Z3O7"]
    assert capex[7] == {
        "tag": capex[1], "form": "JP-AR", "accn": "S100Z3O7",
        "start": "2024-07-01", "end": "2025-06-30", "filed": "2026-09-24",
        "unit": "JPY", "document": "S100Z3O7",
        "canonical_tag": "PaymentsToAcquirePropertyPlantAndEquipment",
        "concept": fact.provenance.concept,
    }
    assert capex[7]["concept"].endswith("source signed negative)")
    depreciation = bridge["depreciation_and_amortisation"]
    assert depreciation[0] == 1771000000.0
    assert depreciation[7]["start"] == "2024-07-01"
    assert depreciation[7]["canonical_tag"] == "DepreciationAndAmortization"
    assert "source signed negative" not in depreciation[7]["concept"]
    assert owner.annual[2025].free_cash_flow.value == Decimal("889000000")

    from dataclasses import replace

    without_location = replace(fact, provenance=replace(fact.provenance, unit=None, document=None))
    annual = replace(owner.annual[2025], total_capital_expenditure=without_location)
    sparse_snapshot = replace(snapshot, owner_earnings=replace(owner, annual={2025: annual}))
    sparse = _owner_earnings_row(sparse_snapshot)["annual_per_share"]["2025"]["cash_flow_bridge"]["total_capital_expenditure"]
    assert sparse[:5] == capex[:5]
    assert sparse[5:7] == [None, None]
    assert sparse[7]["start"] == "2024-07-01"
    assert "unit" not in sparse[7] and "document" not in sparse[7]


@pytest.mark.parametrize("problem", ["dimension", "entity", "currency"])
def test_retained_cashflow_aliases_do_not_admit_tampered_scope(monkeypatch, problem):
    record, raw = _s100z3o7_cashflow_rows()
    for row in raw:
        if row["tag"] not in {"DepreciationAndAmortizationOpeCF",
                              "PurchaseOfPropertyPlantAndEquipmentInvCF", "PurchaseOfIntangibleAssetsInvCF"}:
            continue
        if problem == "dimension":
            row["dimensions"] = [{"axis": "ConsolidatedOrNonConsolidatedAxis", "member": "NonConsolidatedMember"}]
        elif problem == "entity":
            row["entity"] = "E99999-000"
        else:
            row["unit"] = "USD"
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"fixture", ticker="1381.T")
    assert not {"DepreciationAndAmortization", "PaymentsToAcquirePropertyPlantAndEquipment",
                "PaymentsToAcquireIntangibleAssets"} & bundle["facts"]["canonical"].keys()


def test_native_ifrs_cash_payments_use_the_same_separate_cash_contract(monkeypatch):
    # QB Net, S100Z383, current-year native JPY balance and cash-payment rows.
    record = {**_record("S100Z383", "E33838", "65710"),
              "submitDateTime": "2026-09-24", "periodStart": "2025-07-01",
              "periodEnd": "2026-06-30"}
    rows = [
        _fact("jpigp_cor", "AssetsIFRS", "35719000000", start=None, end="2026-06-30"),
        _fact("jpigp_cor", "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS", "-1323000000",
              start="2025-07-01", end="2026-06-30"),
        _fact("jpigp_cor", "PurchaseOfIntangibleAssetsInvCFIFRS", "-218000000",
              start="2025-07-01", end="2026-06-30"),
    ]
    for row in rows:
        row["entity"] = "E33838-000"
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: rows)
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"fixture", ticker="6571.T")
    ppe = _entry(bundle, "PaymentsToAcquirePropertyPlantAndEquipment")
    intangible = _entry(bundle, "PaymentsToAcquireIntangibleAssets")
    assert ppe["val"] == "1323000000" and intangible["val"] == "218000000"
    assert ppe["_source_namespace"] == intangible["_source_namespace"] == "jpigp_cor"
    assert ppe["_source_tag"] == "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS"
    assert intangible["_source_tag"] == "PurchaseOfIntangibleAssetsInvCFIFRS"


def test_direct_jgaap_cash_equivalents_do_not_relabel_cash_and_deposits(monkeypatch):
    record, rows = _s100z3o7_cashflow_rows()
    for tag, value in (("CashAndCashEquivalents", "100"), ("CashAndDeposits", "150")):
        cash = {**rows[0], "tag": tag, "value": Decimal(value)}
        rows.append(cash)
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: rows)
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"fixture", ticker="1381.T")
    cash = _entry(bundle, "CashAndCashEquivalentsAtCarryingValue")
    assert cash["val"] == "100" and cash["_source_tag"] == "CashAndCashEquivalents"


def test_tse_quote_currency_does_not_follow_a_usd_statement(monkeypatch):
    from screener import sync
    from screener.evidence import EvidenceBundle

    raw = [
        _fact("jpigp_cor", "AssetsIFRS", 100, start=None, unit="USD"),
        _fact("jpigp_cor", "BasicEarningsLossPerShareIFRS", "2.5", unit="USD/shares"),
    ]
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: raw)
    bundle = edinet_mapper.build_edinet_companyfacts(
        _record("S100YETA", "E01772", "67520"), b"fixture", ticker="6752.T")
    assert bundle["_adapter"]["reporting_currency"] == "USD"
    assert bundle["_adapter"]["quote_currency"] == "JPY"
    status, row = sync._derive_evidence(EvidenceBundle("E01772", "6752.T", bundle, None, None))
    assert status == "ok"
    priced = sync.apply_price(row, 5000)
    assert next(criterion for criterion in priced["criteria"] if criterion["n"] == 1)["status"] == "INSUFFICIENT_DATA"


def test_combined_jgaap_purchase_keeps_tangible_and_intangible_scope(monkeypatch):
    record, rows = _s100z3o7_cashflow_rows()
    # A filing-shaped scope control using Beauty Kadan's reported combined cash payment.
    rows = [row for row in rows if row["tag"] not in {
        "PurchaseOfPropertyPlantAndEquipmentInvCF", "PurchaseOfIntangibleAssetsInvCF"}]
    combined = {**rows[-1], "tag": "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF",
                "value": Decimal("-41661000")}
    rows.append(combined)
    monkeypatch.setattr(edinet_mapper, "xbrl_facts", lambda archive, doc: rows)
    bundle = edinet_mapper.build_edinet_companyfacts(record, b"fixture", ticker="1381.T")
    purchase = _entry(bundle, "PaymentsToAcquireProductiveAssets")
    assert purchase["val"] == "41661000"
    assert purchase["_source_tag"] == "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF"
    assert "PaymentsToAcquirePropertyPlantAndEquipment" not in bundle["facts"]["canonical"]
    assert "PaymentsToAcquireIntangibleAssets" not in bundle["facts"]["canonical"]
