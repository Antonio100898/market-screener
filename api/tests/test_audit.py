"""The independent payload checker must compare like dates and shown precision."""

import json
from datetime import date
from decimal import Decimal

from screener import audit, coverage


def test_filing_audit_opens_exact_incorporated_source_not_annual_wrapper(monkeypatch):
    requested = []

    class Edgar:
        def _get_text(self, url):
            requested.append(url)
            return "statement"

    monkeypatch.setattr(audit.statements, "find", lambda summary, kind: "R1.htm")
    monkeypatch.setattr(audit.statements, "lines", lambda document: [("Assets", [10])])
    monkeypatch.setattr(audit.statements, "columns", lambda document: [date(2025, 12, 31)])
    monkeypatch.setattr(audit.statements, "elements", lambda document: {})
    row = {
        "cik": "0000000123",
        "sources": {"total_assets": {
            "accn": "0000000123-26-000009",
            "annual_accn": "0000000123-26-000010",
            "end": "2025-12-31",
        }},
    }

    audit._read_statement(row, Edgar(), "balance_sheet")

    assert requested
    assert all("000000012326000009" in url for url in requested)
    assert all("000000012326000010" not in url for url in requested)


def test_audit_cli_can_validate_a_candidate_without_replacing_the_ui_payload(
        tmp_path, monkeypatch):
    dashboard = tmp_path / "candidate.json"
    candidate_rows = [{"ticker": "TEST"}, {"ticker": "OTHER"}]
    dashboard.write_text(json.dumps({"rows": candidate_rows}), encoding="utf-8")
    seen = {}

    class FakeEdgar:
        cache_dir = tmp_path

    def fake_audit(rows, cache_dir, quiet, edgar):
        seen.update(rows=rows, cache_dir=cache_dir, quiet=quiet, edgar=edgar)
        return {"totals": {
            "sourced_ok": 0, "sourced_bad": 0, "derived_ok": 0,
            "derived_bad": 0, "mixed_dates": 0, "filing_ok": 0,
            "filing_bad": 0, "filing_unknown": 0, "unchecked": 0,
        }, "problems": []}

    monkeypatch.setattr(audit, "EdgarClient", FakeEdgar)
    monkeypatch.setattr(audit, "audit", fake_audit)

    assert audit.main(["--dashboard", str(dashboard), "--all", "--quiet"]) == 0
    assert seen["rows"] == candidate_rows
    assert seen["cache_dir"] == tmp_path


def test_dimensioned_share_source_is_checked_against_the_exact_dera_class(tmp_path):
    cik = "0000000001"
    tag = "WeightedAverageNumberOfDilutedSharesOutstanding"
    base = {
        "end": "2025-12-31", "accn": "test-2025", "form": "10-K",
        "filed": "2026-02-01", "start": "2025-01-01",
    }
    (tmp_path / f"companyfacts_{cik}.json").write_text(json.dumps({
        "facts": {"us-gaap": {tag: {"units": {"shares": [
            {**base, "val": 1_500_000},
        ]}}}},
    }), encoding="utf-8")
    (tmp_path / f"dimensioned_{cik}.json").write_text(json.dumps({
        "facts": {"us-gaap": {tag: {"units": {"shares": [
            {**base, "val": 1_500_000,
             "segments": "ClassOfStock=CommonClassA;"},
            {**base, "val": 10_000_000,
             "segments": "ClassOfStock=CommonClassB;"},
        ]}}}},
    }), encoding="utf-8")
    row = {
        "cik": cik, "ticker": "CLASS-B", "shares": 10_000_000,
        "sources": {"shares": {
            "tag": f"us-gaap:{tag}", "accn": "test-2025",
            "form": "10-K", "start": "2025-01-01", "end": "2025-12-31",
            "segments": "ClassOfStock=CommonClassB;",
        }},
    }

    result = audit.audit([row], tmp_path, quiet=True)

    assert result["totals"]["sourced_ok"] == 1
    assert result["totals"]["sourced_bad"] == 0
    assert result["totals"]["unchecked"] == 0


def test_payload_audit_allows_one_fact_on_both_sides_of_a_ratio(
        tmp_path, monkeypatch):
    payload = {"rows": [{
        "ticker": "LEASE",
        "sources": {"fixed_charge_coverage": {
            "tag": (
                "(us-gaap:OperatingIncomeLoss + us-gaap:OperatingLeaseCost) / "
                "(us-gaap:InterestExpense + us-gaap:OperatingLeaseCost)"),
        }},
    }, {
        "ticker": "DOUBLE",
        "sources": {"bad_sum": {
            "tag": "us-gaap:Cash + us-gaap:Cash",
        }},
    }, {
        "ticker": "RECONCILED",
        "sources": {"operating_income": {
            "tag": (
                "us-gaap:GrossProfit - us-gaap:OperatingExpenses; reconciled "
                "with us-gaap:NonoperatingIncomeExpense to "
                "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"),
            "concept": "Operating income (derived and reconciled to pretax income)",
            "components": [
                {"tag": "us-gaap:GrossProfit"},
                {"tag": "us-gaap:OperatingExpenses"},
                {"tag": "us-gaap:NonoperatingIncomeExpense"},
                {"tag": "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"},
                {"tag": "us-gaap:OperatingIncomeLoss"},
            ],
        }},
    }, {
        "ticker": "ADAPTER",
        "sources": {"total_liabilities": {
            "tag": (
                "adidas-workbook:Total liabilities and equity - "
                "adidas-workbook:Total equity"),
            "components": [
                {"tag": "adidas-workbook:Total liabilities and equity"},
                {"tag": "adidas-workbook:Total equity"},
            ],
        }},
    }]}
    dashboard = tmp_path / "dashboard.json"
    dashboard.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(coverage, "DASHBOARD", dashboard)

    problems = coverage.audit_payload()

    assert not any(problem.startswith("LEASE:") for problem in problems)
    assert not any(problem.startswith("RECONCILED:") for problem in problems)
    assert not any(problem.startswith("ADAPTER:") for problem in problems)
    assert "DOUBLE: bad_sum counts Cash twice" in problems


def test_income_audit_does_not_compare_one_share_class_with_another(monkeypatch):
    row = {
        "cik": "0001067983",
        "ticker": "BRK-B",
        "annual_eps": {"2025": 31.04},
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "sources": {"eps": {
            "tag": "us-gaap:EarningsPerShareBasic",
            "form": "10-K",
            "accn": "brk-2025",
            "end": "2025-12-31",
            "segments": "ClassOfStock=EquivalentClassB;",
        }},
    }
    printed = [("Basic earnings per share — Class A", [Decimal("46563")])]
    tagged = {"us-gaap_EarningsPerShareBasic": [Decimal("46563")]}
    monkeypatch.setattr(
        audit,
        "_read_statement",
        lambda *_: ((printed, tagged), [0], [date(2025, 12, 31)], None),
    )

    result = audit.against_income(row, object())

    assert result == [("FILING?", "FY2025 eps", 31.04, 46563.0,
                       "filed under a share-class dimension that the rendered "
                       "statement reader cannot distinguish")]


def test_income_audit_rebuilds_reconciled_operating_income(monkeypatch):
    row = {
        "cik": "0000320187",
        "ticker": "NKE",
        "annual_operating_income": {"2026": 3_797.0},
        "annual_ratios": {"2026": {"end": "2026-05-31"}},
        "sources": {
            "eps": {
                "tag": "us-gaap:EarningsPerShareDiluted", "form": "10-K",
                "accn": "nke-2026", "end": "2026-05-31",
            },
            "operating_income": {
                "concept": (
                    "Operating income (derived and reconciled to pretax income; "
                    "direct comparative had an exact 1000x presentation-scale "
                    "contradiction)"),
                "tag": "derived expression",
                "components": [
                    {"tag": "us-gaap:GrossProfit"},
                    {"tag": "us-gaap:SellingGeneralAndAdministrativeExpense"},
                    {"tag": ("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                             "ExtraordinaryItemsNoncontrollingInterest")},
                    {"tag": "us-gaap:InterestIncomeExpenseNonoperatingNet"},
                    {"tag": "us-gaap:OtherNonoperatingIncomeExpense"},
                    # The contradictory direct fact stays in provenance for audit
                    # visibility; statement arithmetic, not this bad value, wins.
                    {"tag": "us-gaap:OperatingIncomeLoss"},
                ],
            },
        },
    }
    tagged = {
        "us-gaap_GrossProfit": [Decimal("19911")],
        "us-gaap_SellingGeneralAndAdministrativeExpense": [Decimal("16114")],
        "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [Decimal("3900")],
        # The rendered expense rows show income in parentheses even though the
        # source facts used by the extraction reconciliation are positive income.
        "us-gaap_InterestIncomeExpenseNonoperatingNet": [Decimal("-50")],
        "us-gaap_OtherNonoperatingIncomeExpense": [Decimal("-53")],
        "us-gaap_OperatingIncomeLoss": [Decimal("3.797")],
    }
    monkeypatch.setattr(
        audit,
        "_read_statement",
        lambda *_: (([], tagged), [0], [date(2026, 5, 31)], None),
    )

    result = audit.against_income(row, object())

    assert result == [("FILING-OK", "FY2026 operating_income", 3_797.0,
                       3_797.0, None)]


def test_income_audit_rebuilds_jnj_inverse_operating_bridge(monkeypatch):
    row = {
        "cik": "0000200406",
        "ticker": "JNJ",
        "annual_operating_income": {"2025": 25_287.0},
        "annual_ratios": {"2025": {"end": "2025-12-28"}},
        "sources": {
            "eps": {
                "tag": "us-gaap:EarningsPerShareDiluted", "form": "10-K",
                "accn": "jnj-2025", "end": "2025-12-28",
            },
            "operating_income": {
                "concept": (
                    "Operating income (derived and reconciled from pretax less "
                    "complete nonoperating lines)"),
                "tag": "derived expression",
                "components": [
                    {"tag": "us-gaap:GrossProfit"},
                    {"tag": "us-gaap:SellingGeneralAndAdministrativeExpense"},
                    {"tag": (
                        "us-gaap:ResearchAndDevelopmentExpenseExcludingAcquired"
                        "InProcessCost")},
                    {"tag": "us-gaap:RestructuringCharges"},
                    {"tag": (
                        "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                        "ExtraordinaryItemsNoncontrollingInterest")},
                    {"tag": "us-gaap:InvestmentIncomeInterest"},
                    {"tag": "us-gaap:InterestExpenseNonoperating"},
                    {"tag": "us-gaap:OtherNonoperatingIncomeExpense"},
                ],
            },
        },
    }
    tagged = {
        "us-gaap_GrossProfit": [Decimal("63937")],
        "us-gaap_SellingGeneralAndAdministrativeExpense": [Decimal("23676")],
        "us-gaap_ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost": [
            Decimal("14665")],
        "us-gaap_RestructuringCharges": [Decimal("228")],
        "us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": [Decimal("32581")],
        "us-gaap_InvestmentIncomeInterest": [Decimal("-1056")],
        "us-gaap_InterestExpenseNonoperating": [Decimal("971")],
        "us-gaap_OtherNonoperatingIncomeExpense": [Decimal("-7209")],
    }
    monkeypatch.setattr(
        audit,
        "_read_statement",
        lambda *_: (([], tagged), [0], [date(2025, 12, 28)], None),
    )

    result = audit.against_income(row, object())

    assert result == [("FILING-OK", "FY2025 operating_income", 25_287.0,
                       25_287.0, None)]


def test_income_audit_accepts_combined_fresh_start_periods(monkeypatch):
    row = {
        "cik": "0001498710", "ticker": "FLYYQ",
        "annual_net_income": {"2025": -2_760_453_000.0},
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "sources": {
            "eps": {
                "tag": "us-gaap:EarningsPerShareDiluted", "form": "10-K",
                "accn": "flyy-2025", "end": "2024-12-31",
            },
            "net_income": {
                "tag": "us-gaap:NetIncomeLoss", "form": "10-K/A",
                "accn": "flyy-2025a", "end": "2025-12-31",
            },
        },
    }
    printed = [("Net income (loss)", [
        Decimal("72216000"), Decimal("-2832669000"),
        Decimal("-1229495000"),
    ])]
    headings = [date(2025, 3, 12), date(2025, 12, 31),
                date(2024, 12, 31)]
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: ((printed, {}), [1], headings, None),
    )

    result = audit.against_income(row, object())

    assert result == [(
        "FILING-OK", "FY2025 net_income", -2_760_453_000.0,
        -2_760_453_000.0,
        "sum of predecessor and successor statement periods",
    )]


def test_balance_audit_reads_each_fields_own_source_accession(monkeypatch):
    row = {
        "cik": "1", "ticker": "TEST", "intangibles": 399.0,
        "sources": {
            "total_assets": {"tag": "us-gaap:Assets", "accn": "later-q",
                             "end": "2026-06-30"},
            "intangibles": {
                "tag": "us-gaap:IntangibleAssetsNetExcludingGoodwill",
                "accn": "source-k", "end": "2025-12-31",
            },
        },
    }
    calls = []

    def read(_row, _edgar, _kind, source_override=None):
        accn = (source_override or row["sources"]["total_assets"])["accn"]
        calls.append(accn)
        if accn == "source-k":
            return (([], {"us-gaap_IntangibleAssetsNetExcludingGoodwill": [
                Decimal("399")]}), [0], [date(2025, 12, 31)], None)
        return (([], {"us-gaap_IntangibleAssetsNetExcludingGoodwill": [
            Decimal("242")]}), [0], [date(2026, 6, 30)], None)

    monkeypatch.setattr(audit, "_read_statement", read)

    result = audit.against_filing(row, object())

    assert result == [("FILING-OK", "intangibles", 399.0, 399.0,
                       "printed as 'us-gaap_IntangibleAssetsNetExcludingGoodwill'")]
    assert calls == ["later-q", "source-k"]


def test_balance_audit_uses_visible_row_when_element_link_misses_a_column(
        monkeypatch):
    row = {
        "cik": "1", "ticker": "TEST", "intangibles": 121_590.0,
        "sources": {
            "total_assets": {"tag": "us-gaap:Assets", "accn": "k25",
                             "end": "2025-09-30"},
            "intangibles": {"tag": "us-gaap:OtherIntangibleAssetsNet",
                            "accn": "k25", "end": "2025-09-30"},
        },
    }
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([("Other intangible assets, net",
                       [Decimal("121590"), Decimal("126913")])],
                     {"us-gaap_OtherIntangibleAssetsNet": [Decimal("126913")]}),
                    [0], [date(2025, 9, 30), date(2024, 9, 30)], None),
    )

    result = audit.against_filing(row, object())

    assert result[0][:4] == ("FILING-OK", "intangibles", 121_590.0, 121_590.0)


def test_income_audit_distinguishes_dual_currency_facts(monkeypatch):
    source = {
        "tag": "us-gaap:GrossProfit", "form": "20-F", "accn": "foreign-k",
        "start": "2025-01-01", "end": "2025-12-31",
    }
    row = {
        "cik": "1", "ticker": "TEST",
        "annual_gross_profit": {"2025": 100.0},
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "sources": {"eps": source, "gross_profit": source},
    }
    facts = {"facts": {"us-gaap": {"GrossProfit": {"units": {
        "USD": [{"start": "2025-01-01", "end": "2025-12-31",
                 "val": 100, "accn": "foreign-k"}],
        "CNY": [{"start": "2025-01-01", "end": "2025-12-31",
                 "val": 700, "accn": "foreign-k"}],
    }}}}}
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([], {"us-gaap_GrossProfit": [Decimal("700")]}),
                    [0], [date(2025, 12, 31)], None),
    )

    result = audit.against_income(row, object(), facts)

    assert result == [("FILING?", "FY2025 gross_profit", 100.0, 700.0,
                       "the same concept is filed in another currency/unit; "
                       "the rendered statement chose that presentation")]


def test_income_audit_accepts_annual_value_in_duplicate_end_date_column(
        monkeypatch):
    """A 10-K can render the latest quarter and full year under one end date."""
    source = {
        "tag": "us-gaap:GrossProfit", "form": "10-K", "accn": "annual-k",
        "end": "2025-12-31",
    }
    row = {
        "cik": "1", "ticker": "TEST",
        "annual_gross_profit": {"2025": 100.0},
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "sources": {"eps": source, "gross_profit": source},
    }
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([("Gross profit", [Decimal("25"), Decimal("100")])],
                     {"us-gaap_GrossProfit": [Decimal("25"), Decimal("100")]}),
                    [0, 1], [date(2025, 12, 31), date(2025, 12, 31)], None),
    )

    assert audit.against_income(row, object()) == [
        ("FILING-OK", "FY2025 gross_profit", 100.0, 100.0, None)]


def test_cash_flow_audit_ignores_empty_tagged_comparative_cell(monkeypatch):
    row = {
        "cik": "1", "ticker": "TEST",
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "owner_earnings": {
            "fiscal_year": 2025,
            "components": [["operating cash flow", 100.0]],
        },
    }
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([], {
            "us-gaap_NetCashProvidedByUsedInOperatingActivities": [None],
        }), [0], [date(2025, 12, 31)], None),
    )

    assert audit.against_cash_flow(row, object()) == [(
        "FILING?", "all-capex evidence operating cash flow", 100.0, None,
        "the statement tags no such concept",
    )]


def test_income_audit_finds_historical_translation_in_older_accession(
        monkeypatch):
    source = {
        "tag": "us-gaap:GrossProfit", "form": "20-F", "accn": "latest-k",
        "end": "2026-12-31",
    }
    row = {
        "cik": "1", "ticker": "TEST",
        "annual_gross_profit": {"2025": 100.0},
        "annual_ratios": {"2025": {"end": "2025-12-31"}},
        "sources": {"eps": source, "gross_profit": source},
    }
    facts = {"facts": {"us-gaap": {"GrossProfit": {"units": {
        "USD": [{"start": "2025-01-01", "end": "2025-12-31",
                 "val": 100, "accn": "older-k"}],
        "CNY": [{"start": "2025-01-01", "end": "2025-12-31",
                 "val": 700, "accn": "latest-k"}],
    }}}}}
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([], {"us-gaap_GrossProfit": [Decimal("700")]}),
                    [0], [date(2025, 12, 31)], None),
    )

    result = audit.against_income(row, object(), facts)

    assert result[0][0] == "FILING?"
    assert "another currency/unit" in result[0][4]


def test_income_audit_reads_the_annual_filing_that_supplied_an_older_year(
        monkeypatch):
    latest = {
        "tag": "us-gaap:GrossProfit", "form": "10-K",
        "accn": "latest-k", "end": "2013-12-31",
    }
    row = {
        "cik": "0001545236", "ticker": "AHII",
        "annual_gross_profit": {"2012": 16_375.0},
        "annual_ratios": {"2012": {"end": "2012-12-31"}},
        "sources": {"eps": latest, "gross_profit": latest},
    }
    facts = {"facts": {"us-gaap": {"GrossProfit": {"units": {
        "USD": [{
            "start": "2012-01-01", "end": "2012-12-31", "val": 16_375,
            "accn": "source-k", "form": "10-K", "filed": "2013-03-04",
        }],
    }}}}}
    calls = []

    def read(_row, _edgar, _kind, source_override=None):
        calls.append((source_override or {}).get("accn"))
        if source_override:
            return (([("Gross profit", [Decimal("16375")])],
                     {"us-gaap_GrossProfit": [Decimal("16375")]}),
                    [0], [date(2012, 12, 31)], None)
        return (([("Gross profit", [Decimal("13")])],
                 {"us-gaap_GrossProfit": [Decimal("13")]}),
                [0], [date(2012, 12, 31)], None)

    monkeypatch.setattr(audit, "_read_statement", read)

    result = audit.against_income(row, object(), facts)

    assert result == [(
        "FILING-OK", "FY2012 gross_profit", 16_375.0, 16_375.0,
        "printed in source accession source-k",
    )]
    assert calls == [None, "source-k"]


def test_balance_audit_does_not_index_a_one_cell_element_as_column_zero(
        monkeypatch):
    row = {
        "cik": "1", "ticker": "TEST", "intangibles": 121_590.0,
        "sources": {
            "total_assets": {"tag": "us-gaap:Assets", "accn": "k25",
                             "end": "2025-09-30"},
            "intangibles": {"tag": "us-gaap:OtherIntangibleAssetsNet",
                            "accn": "k25", "end": "2025-09-30"},
        },
    }
    monkeypatch.setattr(
        audit, "_read_statement",
        lambda *_: (([], {"us-gaap_OtherIntangibleAssetsNet": [
            Decimal("126913")]}), [0],
                    [date(2025, 9, 30), date(2024, 9, 30)], None),
    )

    result = audit.against_filing(row, object())

    assert result[0][0] == "FILING?"
    assert "covers fewer cells" in result[0][4]


def test_debt_identity_does_not_add_older_components_to_a_newer_rollup():
    row = {
        "debt": 10.0,
        "total_debt": 10.0,
        "long_term_debt": 100.0,
        "short_term_debt": 20.0,
        "sources": {
            "total_debt": {"end": "2026-06-30"},
            "long_term_debt": {"end": "2025-12-31"},
            "short_term_debt": {"end": "2025-12-31"},
        },
        "criteria": [],
    }

    debt = next(item for item in audit._identities(row) if item[0] == "debt")

    assert debt[1] == 10.0
    assert debt[2] == 10.0


def test_ratio_slack_includes_four_decimal_price_rounding():
    slack = audit._rounding_slack(0.0011, 0.0057)

    assert audit._close(0.20, round(0.0011 / 0.0057, 2), slack)


def test_ratio_slack_allows_either_side_of_a_half_cent_boundary():
    slack = audit._rounding_slack(5.3, 8.48)

    assert audit._close(0.63, round(5.3 / 8.48, 2), slack)


def test_current_valuation_audit_uses_the_statement_currency_price():
    row = {
        "price": 200.0,
        "price_reporting_currency": 30_000.0,
        "reporting_currency": "JPY",
        "quote_currency": "USD",
        "fx": {"base": "USD", "counter": "JPY", "rate": 150.0},
        "ttm_eps": 2_000.0,
        "tbvps": 20_000.0,
        "criteria": [
            {"n": 1, "status": "PASS", "value": 15.0},
            {"n": 7, "status": "PASS", "value": 1.5},
        ],
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._identities(row)}

    assert checks["price_reporting_currency"] == (30_000.0, 30_000.0)
    assert checks["pe"] == (15.0, 15.0)
    assert checks["ptbv"] == (1.5, 1.5)


def test_composite_source_keeps_candidates_from_each_filed_currency():
    facts = {"facts": {"us-gaap": {
        "MinorityInterest": {"units": {
            "CNY": [{"end": "2025-12-31", "val": 4_700, "accn": "a"}],
            "USD": [{"end": "2025-12-31", "val": 670, "accn": "a"}],
        }},
        "RedeemableNoncontrollingInterestEquityCarryingAmount": {"units": {
            "CNY": [{"end": "2025-12-31", "val": 107, "accn": "a"}],
            "USD": [{"end": "2025-12-31", "val": 17, "accn": "a"}],
        }},
    }}}
    source = {
        "tag": ("us-gaap:MinorityInterest + "
                "us-gaap:RedeemableNoncontrollingInterestEquityCarryingAmount"),
        "components": [
            {"tag": "us-gaap:MinorityInterest", "accn": "a", "end": "2025-12-31"},
            {"tag": "us-gaap:RedeemableNoncontrollingInterestEquityCarryingAmount",
             "accn": "a", "end": "2025-12-31"},
        ],
    }

    assert 687 in audit._source_values(facts, source)
    assert 4_807 in audit._source_values(facts, source)


def test_derived_source_subtracts_each_filed_currency_candidate():
    facts = {"facts": {"us-gaap": {
        "FiniteLivedIntangibleAssetsGross": {"units": {
            "CNY": [{"end": "2025-12-31", "val": 140, "accn": "a"}],
            "USD": [{"end": "2025-12-31", "val": 20, "accn": "a"}],
        }},
        "FiniteLivedIntangibleAssetsAccumulatedAmortization": {"units": {
            "CNY": [{"end": "2025-12-31", "val": 21, "accn": "a"}],
            "USD": [{"end": "2025-12-31", "val": 3, "accn": "a"}],
        }},
    }}}
    source = {
        "tag": ("us-gaap:FiniteLivedIntangibleAssetsGross - "
                "us-gaap:FiniteLivedIntangibleAssetsAccumulatedAmortization"),
        "accn": "a", "end": "2025-12-31",
        "components": [
            {"tag": "us-gaap:FiniteLivedIntangibleAssetsGross", "accn": "a",
             "end": "2025-12-31"},
            {"tag": "us-gaap:FiniteLivedIntangibleAssetsAccumulatedAmortization",
             "accn": "a", "end": "2025-12-31"},
        ],
    }

    assert 17 in audit._source_values(facts, source)
    assert 119 in audit._source_values(facts, source)
    source.pop("components")
    assert 17 in audit._source_values(facts, source)


def test_audit_checks_annualized_recurring_dividend_against_its_quarter(tmp_path):
    cik = "0000000001"
    facts = {"facts": {"us-gaap": {
        "CommonStockDividendsPerShareCashPaid": {"units": {"USD/shares": [{
            "start": "2026-01-01", "end": "2026-03-31", "val": 0.35,
            "accn": "q126", "form": "10-Q", "filed": "2026-05-05",
        }]}}
    }}}
    (tmp_path / f"companyfacts_{cik}.json").write_text(json.dumps(facts))
    row = {
        "cik": cik,
        "ticker": "TEST",
        "recurring_dividend_per_share": 1.40,
        "sources": {"recurring_dividend_per_share": {
            "tag": "us-gaap:CommonStockDividendsPerShareCashPaid",
            "form": "10-Q", "accn": "q126", "start": "2026-01-01",
            "end": "2026-03-31",
            "filed": "2026-05-05",
        }},
        "criteria": [],
    }

    result = audit.audit([row], tmp_path, quiet=True)

    assert result["totals"]["sourced_ok"] == 1
    assert result["totals"]["sourced_bad"] == 0


def test_duration_source_start_distinguishes_quarter_from_ytd_in_same_filing():
    facts = {"facts": {"us-gaap": {
        "CommonStockDividendsPerShareDeclared": {"units": {"USD/shares": [
            {"start": "2026-01-01", "end": "2026-06-30", "val": 0.44,
             "accn": "q226"},
            {"start": "2026-04-01", "end": "2026-06-30", "val": 0.22,
             "accn": "q226"},
        ]}}
    }}}
    source = {
        "tag": "us-gaap:CommonStockDividendsPerShareDeclared",
        "accn": "q226", "start": "2026-04-01", "end": "2026-06-30",
    }

    assert audit._source_values(facts, source) == [0.22]


def test_duration_source_unit_distinguishes_usd_rate_from_foreign_currency():
    facts = {"facts": {"us-gaap": {
        "CommonStockDividendsPerShareDeclared": {"units": {
            "CAD/shares": [{"start": "2026-04-01", "end": "2026-06-30",
                            "val": 0, "accn": "q226"}],
            "USD/shares": [{"start": "2026-04-01", "end": "2026-06-30",
                            "val": 0.03, "accn": "q226"}],
        }}
    }}}
    source = {
        "tag": "us-gaap:CommonStockDividendsPerShareDeclared", "accn": "q226",
        "start": "2026-04-01", "end": "2026-06-30", "unit": "USD/shares",
    }

    assert audit._source_values(facts, source) == [0.03]


def test_ch13_average_uses_decimal_inputs_before_display_rounding():
    row = {
        "annual_eps": {"2023": -0.085, "2024": 0.082, "2025": 0.108},
        "ch13": {"latest_fy": 2025, "avg_recent": 0.04,
                 "ten_year_present": 3, "ten_year_positive": 2},
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}
    assert checks["ch13.avg_recent"] == (0.04, 0.04)


def test_audit_recomputes_finkle_return_from_payload_evidence():
    row = {
        "total_assets": 200.0,
        "total_liabilities": 140.0,
        "long_term_debt": 30.0,
        "short_term_debt": 5.0,
        "debt": 35.0,
        "goodwill": 10.0,
        "intangibles": 5.0,
        "annual_net_income": {"2025": 12.0},
        "annual_revenue": {"2025": 100.0},
        "profitability": {
            "fiscal_year": 2025,
            "net_tangible_assets": 45.0,
            "on_net_tangible_assets": 26.67,
            "average_common_equity": 50.0,
            "on_equity": 24.0,
        },
        "debt_to_equity": 35 / 60,
        "criteria": [],
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._identities(row)}

    assert checks["profitability.net_tangible_assets"] == (45.0, 45.0)
    assert checks["return_on_net_tangible_assets"] == (26.67, 12 / 45 * 100)
    assert checks["return_on_equity"] == (24.0, 24.0)
    assert checks["debt_to_equity"] == (35 / 60, 35 / 60)


def test_audit_recomputes_historical_revenue_and_gross_margin():
    row = {
        "annual_revenue": {"2025": 100.0},
        "annual_gross_profit": {"2025": 40.0},
        "annual_ratios": {"2025": {"revenue": 100.0, "gross_margin": 40.0}},
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks["annual_ratios.2025.revenue"] == (100.0, 100.0)
    assert checks["annual_ratios.2025.gross_margin"] == (40.0, 40.0)


def test_audit_recomputes_historical_nopat_roic_and_ronta():
    row = {"annual_ratios": {"2025": {
        "operating_income_for_nopat": 100.0,
        "normalized_tax_rate": 25.0,
        "nopat": 75.0,
        "invested_capital": 250.0,
        "capital_including_cash": 300.0,
        "average_net_tangible_operating_assets": 200.0,
        "average_lease_neutral_net_tangible_operating_assets": 150.0,
        "nopat_roic": 30.0,
        "nopat_return_including_cash": 25.0,
        "ronta": 37.5,
        "lease_neutral_ronta": 50.0,
        "operating_return_evidence": {
            "invested_capital_beginning": {"value": 200.0},
            "invested_capital_ending": {"value": 300.0},
            "capital_including_cash_beginning": {"value": 250.0},
            "capital_including_cash_ending": {"value": 350.0},
            "net_tangible_operating_assets_beginning": {"value": 150.0},
            "net_tangible_operating_assets_ending": {"value": 250.0},
            "lease_neutral_net_tangible_operating_assets_beginning": {"value": 100.0},
            "lease_neutral_net_tangible_operating_assets_ending": {"value": 200.0},
        },
    }}}

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks["annual_ratios.2025.nopat"] == (75.0, 75.0)
    assert checks["annual_ratios.2025.invested_capital"] == (250.0, 250.0)
    assert checks["annual_ratios.2025.nopat_roic"] == (30.0, 30.0)
    assert checks["annual_ratios.2025.ronta"] == (37.5, 37.5)
    assert checks["annual_ratios.2025.lease_neutral_ronta"] == (50.0, 50.0)


def test_audit_recomputes_and_guards_conservative_operating_returns():
    row = {
        "operating_returns": {"nopat": 75.0, "nopat_roic": None, "ronta": None},
        "operating_returns_estimate": {
            "status": "CONSERVATIVE_LOWER_BOUND",
            "operating_income_for_nopat": 100.0,
            "normalized_tax_rate": 25.0,
            "nopat": 75.0,
            "invested_capital": 250.0,
            "capital_including_cash": 300.0,
            "average_net_tangible_operating_assets": 200.0,
            "nopat_roic": 30.0,
            "ronta": 37.5,
            "operating_return_assumptions": ["intangibles"],
        },
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}
    assert checks["operating_returns_estimate.nopat"] == (75.0, 75.0)
    assert checks["operating_returns_estimate.nopat_roic"] == (30.0, 30.0)
    assert checks["operating_returns_estimate.ronta"] == (37.5, 37.5)
    assert checks["operating_returns_estimate.denominator_floor_guard"] == (1, 1)
    assert checks["operating_returns_estimate.assumption_guard"] == (1, 1)
    assert checks["operating_returns_estimate.strict_gap_guard"] == (1, 1)
    assert checks["operating_returns_estimate.positive_guard"] == (1, 1)


def test_audit_guards_the_return_quality_assumption_overlay():
    row = {
        "profitability": {"on_equity": 30.0},
        "operating_returns": {"nopat_roic": None, "ronta": 40.0},
        "debt_to_equity": None,
        "return_quality_assumption": {
            "status": "APPLIED",
            "roe": 30.0,
            "nopat_roic": 20.0,
            "ronta": 40.0,
            "debt_to_equity": 0.0,
            "applied": ["debt", "intangibles"],
            "input_assumptions": {
                "roe": [],
                "nopat_roic": ["intangibles"],
                "ronta": [],
                "debt_to_equity": ["debt"],
            },
        },
    }

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks["return_quality_assumption.status_guard"] == (1, 1)
    assert checks["return_quality_assumption.disclosure_guard"] == (1, 1)
    assert checks["return_quality_assumption.roe_strict_guard"] == (1, 1)
    assert checks["return_quality_assumption.roe.strict_match"] == (30.0, 30.0)
    assert checks["return_quality_assumption.ronta.strict_match"] == (40.0, 40.0)


def test_audit_recomputes_each_displayed_fcf_method_and_spread():
    row = {"owner_earnings": {"fcf_reconciliation": {
        "spread": 2.0,
        "methods": {
            "cash_flow_statement": {
                "value": 8.0, "formula": "CFO - capex",
                "components": [["CFO", 10.0], ["- capex", -2.0]],
            },
            "nopat_less_investment": {
                "value": 10.0, "formula": "NOPAT - investment",
                "components": [["NOPAT", 12.0], ["- investment", -2.0]],
            },
            "revenue_less_costs_and_investment": {
                "value": 10.0, "formula": "revenue - costs - investment",
                "components": [["revenue", 20.0], ["- costs", -8.0],
                               ["- investment", -2.0]],
            },
        },
    }}}

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks["owner_earnings.fcf_reconciliation.cash_flow_statement"] == (8.0, 8.0)
    assert checks["owner_earnings.fcf_reconciliation.nopat_less_investment"] == (10.0, 10.0)
    assert checks["owner_earnings.fcf_reconciliation.spread"] == (2.0, 2.0)


def test_audit_recomputes_average_ntoa_and_ronta_from_payload_evidence():
    row = {"owner_earnings": {
        "nopat": 75.0,
        "average_net_tangible_operating_assets": 250.0,
        "net_tangible_operating_assets_evidence": {
            "beginning": {"value": 200.0},
            "ending": {"value": 300.0},
        },
        "ronta": 30.0,
        "average_lease_neutral_net_tangible_operating_assets": 200.0,
        "lease_neutral_net_tangible_operating_assets_evidence": {
            "beginning": {"value": 150.0},
            "ending": {"value": 250.0},
        },
        "lease_neutral_ronta": 37.5,
    }}

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks["owner_earnings.average_net_tangible_operating_assets"] == (250.0, 250.0)
    assert checks["owner_earnings.ronta"] == (30.0, 30.0)
    assert checks["owner_earnings.lease_neutral_ronta"] == (37.5, 37.5)


def test_audit_recomputes_the_annual_owner_cash_bridge():
    point = lambda value: {"value": value, "source": {"tag": "test"}}
    row = {"owner_earnings": {"annual_per_share": {"2025": {
        "cash_flow_bridge": {
            "net_income": point(70.0),
            "depreciation_and_amortisation": point(12.0),
            "stock_compensation": point(5.0),
            "other_operating_cash_flow_adjustments": point(-8.0),
            "working_capital_cash_effect": point(-4.0),
            "operating_cash_flow": point(75.0),
            "total_capital_expenditure": point(12.0),
            "free_cash_flow": point(63.0),
        },
    }}}}

    checks = {name: (shown, expected) for name, shown, expected, *_ in
              audit._derived_series(row)}

    assert checks[
        "owner_earnings.annual_per_share.2025.cash_flow_bridge.operating_cash_flow"
    ] == (75.0, 75.0)
    assert checks[
        "owner_earnings.annual_per_share.2025.cash_flow_bridge.free_cash_flow"
    ] == (63.0, 63.0)
