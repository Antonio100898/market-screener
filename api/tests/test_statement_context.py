"""Independent physical-context and public filing-audit caller checks."""
from decimal import Decimal
from datetime import date

import pytest

from screener import audit
from screener.sources import statements


class Edgar:
    def __init__(self, document, kind="Balance Sheets"):
        self.document = document
        self.kind = kind

    def _get_text(self, url):
        if url.endswith("FilingSummary.xml"):
            return ("<Reports><Report><HtmlFileName>R1.htm</HtmlFileName>"
                    f"<ShortName>{self.kind}</ShortName></Report></Reports>")
        return self.document


def table(concept, label, left="9", right="7", headers=None):
    headers = headers or ("Dec. 31, 2025 CNY", "Dec. 31, 2025 USD")
    return (f"<table><tr><th>Statement</th><th>{headers[0]}</th><th>{headers[1]}</th></tr>"
            f'<tr><td><a href="defref_{concept}">{label}</a></td>'
            f"<td>{left}</td><td>{right}</td></tr></table>")


def source(tag, unit="CNY", **extra):
    return dict(accn="a", end="2025-12-31", tag=tag, unit=unit,
                form="20-F", **extra)


def balance(amount, unit="CNY"):
    return dict(cik="123", total_assets=amount,
                sources=dict(total_assets=source("us-gaap:Assets", unit)))


def income(field, tag, amount, unit="CNY", **extra):
    selected = source(tag, unit, start="2025-01-01", **extra)
    return dict(cik="123", sources={"eps": selected, field: selected},
                annual_ratios={"2025": {"end": "2025-12-31"}},
                **{f"annual_{field}": {"2025": amount}})


def assert_no_ok(result):
    assert result and all(item[0] != "FILING-OK" for item in result), result


def test_balance_different_currency_equal_amount_cannot_corroborate():
    assert_no_ok(audit.against_filing(balance(7), Edgar(table("us-gaap_Assets", "Total assets"))))


def test_balance_nil_selected_currency_cannot_borrow_other_currency():
    assert_no_ok(audit.against_filing(balance(7), Edgar(table("us-gaap_Assets", "Total assets", left="nil"))))


@pytest.mark.parametrize("amount,unit", [(9, "CNY"), (7, "USD")])
def test_exact_balance_currency_positive_controls(amount, unit):
    actual = audit.against_filing(balance(amount, unit), Edgar(table("us-gaap_Assets", "Total assets")))
    assert actual and all(item[0] == "FILING-OK" for item in actual), actual


@pytest.mark.parametrize("field,tag,label,unit", [
    ("revenue", "us-gaap_Revenues", "Revenues", "CNY"),
    ("net_income", "us-gaap_NetIncomeLoss", "Net income", "CNY"),
    ("eps", "us-gaap_EarningsPerShareDiluted", "Diluted earnings per share", "CNY/shares"),
])
def test_income_different_currency_equal_amount_cannot_corroborate(field, tag, label, unit):
    actual = audit.against_income(income(field, tag.replace("_", ":", 1), 7, unit),
                                  Edgar(table(tag, label), "Statements of Income"))
    assert_no_ok(actual)


def test_income_annual_source_cannot_borrow_same_end_quarter():
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        "12 Months Ended Dec. 31, 2025 USD", "3 Months Ended Dec. 31, 2025 USD"))
    assert_no_ok(audit.against_income(income("revenue", "us-gaap:Revenues", 7, "USD"),
                                    Edgar(doc, "Statements of Income")))


def test_income_declared_class_dimension_cannot_borrow_equal_other_class():
    doc = ('<table><tr><th>USD</th><th>Dec. 31, 2025</th></tr>'
           '<tr><td><a href="defref_us-gaap_EarningsPerShareDiluted">Diluted earnings per share, Class A</a></td><td>9</td></tr>'
           '<tr><td><a href="defref_us-gaap_EarningsPerShareDiluted">Diluted earnings per share</a></td><td>7</td></tr></table>')
    selected = income("eps", "us-gaap:EarningsPerShareDiluted", 7, "USD/shares",
                      segments={"us-gaap:StatementClassOfStockAxis": "issuer:ClassAMember"})
    assert_no_ok(audit.against_income(selected, Edgar(doc, "Statements of Income")))


@pytest.mark.parametrize("name,concept,label", [
    ("operating cash flow", "us-gaap_NetCashProvidedByUsedInOperatingActivities", "Cash from operations"),
    ("+ depreciation & amortisation", "us-gaap_DepreciationDepletionAndAmortization", "Depreciation"),
    ("- total capital expenditure", "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capital payments"),
])
def test_cashflow_different_currency_equal_amount_cannot_corroborate(name, concept, label):
    source_key = {"operating cash flow": "operating_cash_flow",
                  "+ depreciation & amortisation": "depreciation_and_amortisation",
                  "- total capital expenditure": "total_capex"}[name]
    row = dict(cik="123", sources={"eps": source("us-gaap:EarningsPerShareDiluted", "CNY/shares")},
               annual_ratios={"2025": {"end": "2025-12-31"}},
               owner_earnings={"fiscal_year": 2025, "components": [[name, 7]],
                               "sources": {source_key: source(concept.replace("_", ":", 1))}})
    assert_no_ok(audit.against_cash_flow(row, Edgar(table(concept, label), "Statements of Cash Flows")))


def test_later_table_cannot_inherit_first_tables_external_currency_scale():
    doc = ('<p>USD in Millions</p><table><tr><th>Assets</th><th>Dec. 31, 2024</th></tr>'
           '<tr><td>Assets</td><td>1</td></tr></table><p>CNY in Thousands</p>'
           '<table><tr><th>Assets</th><th>Dec. 31, 2025</th></tr>'
           '<tr><td>Assets</td><td>9</td></tr></table>')
    parsed = statements.read(doc)
    cell = parsed.tables[1].rows[0].cells[0]
    assert cell.currency in ("CNY", None), cell
    assert cell.value in (Decimal("9000"), None), cell


def test_per_column_cents_cannot_scale_neighbor_dollar_eps():
    doc = table("us-gaap_EarningsPerShareDiluted", "Diluted earnings per share", "900", "7",
                headers=("Dec. 31, 2025 GBP (pence per share)", "Dec. 31, 2025 USD (dollars per share)"))
    cells = statements.read(doc).tables[0].rows[0].cells
    assert cells[0].value == Decimal("9"), cells
    assert cells[1].value == Decimal("7"), cells


def test_conflicting_column_currency_is_unknown_and_nil_stays_missing():
    doc = table("us-gaap_Assets", "Total assets", "nil", "7",
                headers=("Dec. 31, 2025 CNY USD", "Dec. 31, 2025 USD"))
    cells = statements.read(doc).tables[0].rows[0].cells
    assert cells[0].currency is None and cells[0].value is None
    assert cells[1].currency == "USD" and cells[1].value == Decimal("7")


def test_distinct_basis_same_currency_cannot_corroborate_receipt_by_equal_amount():
    doc = ('<table><tr><th>USD</th><th>Dec. 31, 2025</th></tr>'
           '<tr><td><a href="defref_us-gaap_EarningsPerShareDiluted">Diluted earnings per ordinary share</a></td><td>7</td></tr>'
           '<tr><td><a href="defref_us-gaap_EarningsPerShareDiluted">Diluted earnings per ADS</a></td><td>9</td></tr></table>')
    selected = income("eps", "us-gaap:EarningsPerShareDiluted", 7, "USD/shares")
    selected["receipt"] = {"ratio": 2}
    assert_no_ok(audit.against_income(selected, Edgar(doc, "Statements of Income")))


def test_nil_minority_component_does_not_crash_income_audit():
    doc = ('<table><tr><th>USD</th><th>Dec. 31, 2025</th><th>Dec. 31, 2024</th></tr>'
           '<tr><td><a href="defref_us-gaap_NetIncomeLoss">Net income</a></td><td>9</td><td>10</td></tr>'
           '<tr><td>Noncontrolling interest</td><td>nil</td><td>2</td></tr></table>')
    assert_no_ok(audit.against_income(income("net_income", "us-gaap:NetIncomeLoss", 7, "USD"),
                                    Edgar(doc, "Statements of Income")))


def test_fresh_start_legacy_identity_cannot_sum_distinct_currencies():
    doc = table("us-gaap_NetIncomeLoss", "Net income", "3", "4", headers=(
        "Mar. 12, 2025 CNY", "Dec. 31, 2025 USD"))
    assert_no_ok(audit.against_income(income("net_income", "us-gaap:NetIncomeLoss", 7, "USD"),
                                    Edgar(doc, "Statements of Income")))


@pytest.mark.parametrize("unit,amount", [("CNY", 9), ("USD", 7)])
def test_income_exact_currency_and_annual_duration_positive_controls(unit, amount):
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        "12 Months Ended Dec. 31, 2025 CNY", "12 Months Ended Dec. 31, 2025 USD"))
    result = audit.against_income(income("revenue", "us-gaap:Revenues", amount, unit),
                                  Edgar(doc, "Statements of Income"))
    assert result == [("FILING-OK", "FY2025 revenue", amount, amount, None)]


def test_annual_duration_positive_control_excludes_same_end_quarter():
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        "12 Months Ended Dec. 31, 2025 USD", "3 Months Ended Dec. 31, 2025 USD"))
    result = audit.against_income(income("revenue", "us-gaap:Revenues", 9, "USD"),
                                  Edgar(doc, "Statements of Income"))
    assert result == [("FILING-OK", "FY2025 revenue", 9, 9, None)]


def test_source_qualified_wrong_literal_sign_remains_a_failure():
    doc = table("us-gaap_NetIncomeLoss", "Net loss", left="(9)", right="7", headers=(
        "12 Months Ended Dec. 31, 2025 CNY", "12 Months Ended Dec. 31, 2025 USD"))
    result = audit.against_income(income("net_income", "us-gaap:NetIncomeLoss", 9),
                                  Edgar(doc, "Statements of Income"))
    assert result[0][0] == "FILING" and result[0][3] == -9


@pytest.mark.parametrize("name,key,tag,label", [
    ("operating cash flow", "operating_cash_flow", "us-gaap_NetCashProvidedByUsedInOperatingActivities", "Cash from operations"),
    ("+ depreciation & amortisation", "depreciation_and_amortisation", "us-gaap_DepreciationDepletionAndAmortization", "Depreciation"),
    ("- total capital expenditure", "total_capex", "us-gaap_PaymentsToAcquirePropertyPlantAndEquipment", "Capital payments"),
])
def test_cashflow_uses_own_exact_component_source_positive_controls(name, key, tag, label):
    doc = table(tag, label, headers=("12 Months Ended Dec. 31, 2025 CNY", "12 Months Ended Dec. 31, 2025 USD"))
    selected = source(tag.replace("_", ":", 1), "USD", start="2025-01-01")
    row = dict(cik="123", sources={"eps": {**source("us-gaap:EarningsPerShareDiluted", "CNY/shares"), "accn": "wrong-eps"}},
               annual_ratios={"2025": {"end": "2025-12-31"}},
               owner_earnings={"fiscal_year": 2025, "components": [[name, 7]], "sources": {key: selected}})
    requests = []
    class OwnSourceEdgar(Edgar):
        def _get_text(self, url):
            requests.append(url)
            return super()._get_text(url)
    result = audit.against_cash_flow(row, OwnSourceEdgar(doc, "Statements of Cash Flows"))
    assert result == [("FILING-OK", "all-capex evidence " + name, 7, 7, None)]
    assert all("wrong-eps" not in url for url in requests)


def test_subtraction_cannot_use_same_period_minoritys_other_currency():
    doc = ('<table><tr><th>Income</th><th>12 Months Ended Dec. 31, 2025 CNY</th>'
           '<th>12 Months Ended Dec. 31, 2025 USD</th></tr>'
           '<tr><td><a href="defref_us-gaap_NetIncomeLoss">Net income</a></td><td>9</td><td>20</td></tr>'
           '<tr><td>Noncontrolling interest</td><td>nil</td><td>2</td></tr></table>')
    assert_no_ok(audit.against_income(income("net_income", "us-gaap:NetIncomeLoss", 7),
                                    Edgar(doc, "Statements of Income")))


def test_balance_cache_retains_each_fields_selected_period_and_unit():
    doc = ('<table><tr><th>Balance</th><th>Dec. 31, 2025 CNY</th><th>Dec. 31, 2024 USD</th></tr>'
           '<tr><td><a href="defref_us-gaap_Assets">Total assets</a></td><td>9</td><td>7</td></tr>'
           '<tr><td><a href="defref_us-gaap_Goodwill">Goodwill</a></td><td>3</td><td>4</td></tr></table>')
    row = balance(9)
    row.update(goodwill=4)
    row["sources"]["goodwill"] = {**source("us-gaap:Goodwill", "USD"), "end": "2024-12-31"}
    result = audit.against_filing(row, Edgar(doc))
    assert all(check[0] == "FILING-OK" for check in result)
    assert {check[1]: check[3] for check in result} == {"total_assets": 9, "goodwill": 4}


def test_unqualified_operating_bridge_component_cannot_corroborate():
    doc = ('<table><tr><th>USD</th><th>12 Months Ended Dec. 31, 2025</th></tr>'
           '<tr><td><a href="defref_us-gaap_GrossProfit">Gross profit</a></td><td>9</td></tr>'
           '<tr><td><a href="defref_us-gaap_OperatingExpenses">Operating expenses</a></td><td>2</td></tr>'
           '<tr><td><a href="defref_us-gaap_IncomeLossFromContinuingOperationsBeforeIncomeTaxes">Pretax</a></td><td>7</td></tr>'
           '<tr><td><a href="defref_us-gaap_NonoperatingIncomeExpense">Nonoperating</a></td><td>0</td></tr></table>')
    row = income("operating_income", "derived expression", 7, "USD", concept="Operating income (derived and reconciled)")
    selected = row["sources"]["operating_income"]
    selected["components"] = [source("us-gaap:" + tag, "USD", start="2025-01-01")
                              for tag in ["GrossProfit", "OperatingExpenses", "IncomeLossFromContinuingOperationsBeforeIncomeTaxes", "NonoperatingIncomeExpense"]]
    selected["components"][1]["unit"] = "CNY"
    assert_no_ok(audit.against_income(row, Edgar(doc, "Statements of Income")))
    selected["components"][1]["unit"] = "USD"
    selected["components"][1]["accn"] = "other-document"
    assert_no_ok(audit.against_income(row, Edgar(doc, "Statements of Income")))


@pytest.mark.parametrize("earlier_end,earlier_unit,expected", [
    ("Mar. 12, 2025", "USD", True),
    ("Mar. 11, 2025", "USD", False),
    ("Mar. 13, 2025", "USD", False),
    ("Mar. 12, 2025", "CNY", False),
])
def test_fresh_start_requires_exact_contiguous_intervals_and_same_unit(earlier_end, earlier_unit, expected):
    doc = table("us-gaap_NetIncomeLoss", "Net income", left="3", right="4", headers=(
        f"Predecessor Jan. 1, 2025 to {earlier_end} {earlier_unit}",
        "Successor Mar. 13, 2025 to Dec. 31, 2025 USD"))
    parsed = statements.read(doc)
    printed = statements.lines(parsed)
    headings = statements.columns(parsed)
    selected = source("us-gaap:NetIncomeLoss", "USD", start="2025-01-01")
    assert audit._by_fresh_start_periods(printed, headings, 1, ("net income",), 7, selected) is expected


@pytest.mark.parametrize("band,expected", [
    ("Jan. 1, 2025 to Dec. 31, 2025 3 Months Ended USD", "FILING?"),
    ("Jan. 1, 2025 to Dec. 31, 2025 13 Weeks Ended USD", "FILING?"),
    ("Jan. 1, 2025 to Dec. 31, 2025 12 Months Ended 3 Months Ended USD", "FILING?"),
    ("12 Months Ended 3 Months Ended Dec. 31, 2025 USD", "FILING?"),
    ("12 Months Ended 13 Weeks Ended Dec. 31, 2025 USD", "FILING?"),
    ("Jan. 1, 2025 to Dec. 31, 2025 12 Months Ended USD", "FILING-OK"),
])
def test_period_claims_must_agree_before_annual_equality(band, expected):
    doc = table("us-gaap_Revenues", "Revenues", headers=(band, "12 Months Ended Dec. 31, 2024 USD"))
    checks = audit.against_income(income("revenue", "us-gaap:Revenues", 9, "USD"), Edgar(doc, "Statements of Income"))
    assert checks[0][0] == expected


def test_exact_range_and_week_claim_agree_on_actual_week_duration():
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        "Jan. 2, 2025 to Dec. 31, 2025 52 Weeks Ended USD", "12 Months Ended Dec. 31, 2024 USD"))
    row = income("revenue", "us-gaap:Revenues", 9, "USD")
    row["sources"]["revenue"]["start"] = "2025-01-02"
    assert audit.against_income(row, Edgar(doc, "Statements of Income"))[0][0] == "FILING-OK"


@pytest.mark.parametrize("change", [None, "period", "document", "unit", "nil"])
def test_explicit_subtraction_inputs_keep_actual_component_context(change):
    doc = ('<table><tr><th>USD</th><th>12 Months Ended Dec. 31, 2025</th></tr>'
           '<tr><td><a href="defref_us-gaap_NetIncomeLoss">Net income</a></td><td>9</td></tr>'
           '<tr><td><a href="defref_us-gaap_MinorityInterest">Noncontrolling interest</a></td><td>'
           + ("nil" if change == "nil" else "2") + '</td></tr></table>')
    row = income("net_income", "us-gaap:NetIncomeLoss - us-gaap:MinorityInterest", 7, "USD")
    parent = row["sources"]["net_income"]
    parent["document"] = "original.htm"
    parent["components"] = [source("us-gaap:NetIncomeLoss", "USD", start="2025-01-01", document="original.htm"),
                            source("us-gaap:MinorityInterest", "USD", start="2025-01-01", document="original.htm")]
    child = parent["components"][1]
    if change == "period":
        child["start"] = "2025-10-01"
    elif change == "document":
        child["document"] = "other.htm"
    elif change == "unit":
        child["unit"] = "CNY"
    checks = audit.against_income(row, Edgar(doc, "Statements of Income"))
    assert (checks[0][0] == "FILING-OK") is (change is None)


@pytest.mark.parametrize("duration", ["999999 Months", "999999 Weeks", "999999999999999999999 Weeks",
                                     "-1 Months", "−1 Weeks", "9" * 5000 + " Weeks"])
def test_invalid_numeric_duration_is_unknown_through_reader_and_income(duration):
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        duration + " Ended Dec. 31, 2025 USD", "12 Months Ended Dec. 31, 2024 USD"))
    assert_no_ok(audit.against_income(income("revenue", "us-gaap:Revenues", 9, "USD"),
                                    Edgar(doc, "Statements of Income")))


@pytest.mark.parametrize("caller", ["income", "balance", "cash_flow"])
@pytest.mark.parametrize("second", ["2", "nil", "absent"])
def test_compound_output_never_matches_its_first_operand(caller, second):
    tags, labels = {
        "income": (("NetIncomeLoss", "MinorityInterest"), ("Net income", "Noncontrolling interest")),
        "balance": (("Goodwill", "OtherAssets"), ("Goodwill", "Other assets")),
        "cash_flow": (("NetCashProvidedByUsedInOperatingActivities", "OtherOperatingActivitiesCashFlowStatement"),
                      ("Cash from operations", "Other operating cash flows")),
    }[caller]
    extra = {} if caller == "balance" else {"start": "2025-01-01"}
    parts = [source("us-gaap:" + tag, "USD", **extra) for tag in tags]
    if second == "absent":
        parts[1]["tag"] = "issuer:UnprintedSelectedInput"
    parent = source("us-gaap:" + tags[0] + " - us-gaap:" + tags[1], "USD", components=parts, **extra)
    band = "" if caller == "balance" else "12 Months Ended "
    doc = (f'<table><tr><th>USD</th><th>{band}Dec. 31, 2025</th></tr>'
           f'<tr><td><a href="defref_us-gaap_{tags[0]}">{labels[0]}</a></td><td>9</td></tr>'
           f'<tr><td><a href="defref_us-gaap_{tags[1]}">{labels[1]}</a></td><td>'
           + ("nil" if second == "nil" else "2") + '</td></tr></table>')
    if caller == "income":
        row = income("net_income", parent["tag"], 9, "USD")
        row["sources"]["net_income"] = parent
        checks = audit.against_income(row, Edgar(doc, "Statements of Income"))
    elif caller == "balance":
        row = dict(cik="123", goodwill=9, sources={"total_assets": parent, "goodwill": parent})
        checks = audit.against_filing(row, Edgar(doc))
    else:
        row = dict(cik="123", annual_ratios={"2025": {"end": "2025-12-31"}},
                   owner_earnings={"fiscal_year": 2025, "components": [["operating cash flow", 9]],
                                   "sources": {"operating_cash_flow": parent}})
        checks = audit.against_cash_flow(row, Edgar(doc, "Statements of Cash Flows"))
    assert_no_ok(checks)


def test_existing_balance_identity_uses_actual_selected_operands():
    doc = ('<table><tr><th>USD</th><th>Dec. 31, 2025</th></tr>'
           '<tr><td><a href="defref_us-gaap_Assets">Total assets</a></td><td>9</td></tr>'
           '<tr><td><a href="defref_us-gaap_StockholdersEquity">Total equity</a></td><td>2</td></tr>'
           '<tr><td><a href="defref_issuer_UnselectedEquity">Total equity</a></td><td>0</td></tr></table>')
    assets = source("us-gaap:Assets", "USD")
    liability = source("us-gaap:Assets - us-gaap:StockholdersEquity", "USD", components=[
        assets, source("us-gaap:StockholdersEquity", "USD")])
    row = dict(cik="123", total_assets=9, total_liabilities=7,
               sources={"total_assets": assets, "total_liabilities": liability})
    checks = audit.against_filing(row, Edgar(doc))
    assert all(check[0] == "FILING-OK" for check in checks)
    row["total_liabilities"] = 9
    assert_no_ok([check for check in audit.against_filing(row, Edgar(doc)) if check[1] == "total_liabilities"])


def test_compound_net_income_cannot_borrow_unadjusted_fresh_start_total():
    headers = ["12 Months Ended Dec. 31, 2025 USD",
               "Successor Mar. 13, 2025 to Dec. 31, 2025 USD",
               "Predecessor Jan. 1, 2025 to Mar. 12, 2025 USD"]
    doc = ('<table><tr><th>USD</th>' + ''.join('<th>' + value + '</th>' for value in headers) + '</tr>'
           '<tr><td><a href="defref_us-gaap_NetIncomeLoss">Net income</a></td><td>9</td><td>6</td><td>3</td></tr>'
           '<tr><td><a href="defref_us-gaap_MinorityInterest">Noncontrolling interest</a></td><td>2</td><td>1</td><td>1</td></tr></table>')
    row = income("net_income", "us-gaap:NetIncomeLoss - us-gaap:MinorityInterest", 9, "USD")
    row["sources"]["net_income"]["components"] = [
        source("us-gaap:NetIncomeLoss", "USD", start="2025-01-01"),
        source("us-gaap:MinorityInterest", "USD", start="2025-01-01")]
    assert_no_ok(audit.against_income(row, Edgar(doc, "Statements of Income")))
    row["annual_net_income"]["2025"] = 7
    assert audit.against_income(row, Edgar(doc, "Statements of Income"))[0][0] == "FILING-OK"


@pytest.mark.parametrize("duration", ["1.12 Months", "1.3 Months", "1.13 Weeks", "1,12 Months",
                                     "1e12 Months", "x12 Months", "12_0 Months", "١٢ Months", "Months"])
@pytest.mark.parametrize("explicit_range", [False, True])
def test_whole_duration_token_cannot_borrow_integer_suffix_or_disappear(duration, explicit_range):
    band = ("Jan. 1, 2025 to Dec. 31, 2025 " + duration + " Ended USD" if explicit_range
            else duration + " Ended Dec. 31, 2025 USD")
    doc = table("us-gaap_Revenues", "Revenues", headers=(band, "12 Months Ended Dec. 31, 2024 USD"))
    parsed = statements.read(doc)
    assert parsed.tables[0].headers[0].period_conflict
    for start in [None, date(2025, 1, 1)]:
        qualified = statements.qualify(statements.lines(parsed), statements.SourceContext(
            date(2025, 12, 31), start, "MONEY", "USD"))
        assert all(line[1][0] is None for line in qualified)
        row = income("revenue", "us-gaap:Revenues", 9, "USD")
        row["sources"]["revenue"]["start"] = start.isoformat() if start else None
        assert_no_ok(audit.against_income(row, Edgar(doc, "Statements of Income")))


@pytest.mark.parametrize("duration,start", [("12 Months", "2025-01-01"),
                                          ("+12 Months", "2025-01-01"),
                                          ("53 Weeks", "2024-12-26")])
def test_valid_whole_duration_tokens_keep_actual_period_admission(duration, start):
    doc = table("us-gaap_Revenues", "Revenues", headers=(
        duration + " Ended Dec. 31, 2025 USD", "12 Months Ended Dec. 31, 2024 USD"))
    row = income("revenue", "us-gaap:Revenues", 9, "USD")
    row["sources"]["revenue"]["start"] = start
    assert audit.against_income(row, Edgar(doc, "Statements of Income"))[0][0] == "FILING-OK"
