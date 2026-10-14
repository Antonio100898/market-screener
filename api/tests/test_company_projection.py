import copy
import json
import subprocess
from pathlib import Path

import pytest

from screener.company_projection import (
    criterion, eps_evidence, pe3, project_companies, return_quality_percentiles,
)


_ORACLE = r"""
import fs from "node:fs";
import { returnQualityPercentiles, pe3, valuationPrice, byN } from "./web/src/screen.js";
import { epsEvidence } from "./web/src/earningsEvidence.js";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const rows = Array.isArray(payload) ? payload : payload.rows;
const qualities = returnQualityPercentiles(rows);
const projected = rows.map((r, index) => {
  const price = valuationPrice(r);
  const dps = r.recurring_dividend_per_share ?? r.dividend_per_share;
  return {
    mcap: r.price && r.shares ? r.price * r.shares : null,
    eps10: epsEvidence(r.annual_eps, r.ch13?.latest_fy),
    unjudged: r.criteria.some(c => c.status !== "PASS" && c.status !== "FAIL"),
    inapplicable: r.criteria.some(c => c.status === "NOT_APPLICABLE"),
    netNet: r.ncavps != null && price != null && r.ncavps > 0 && price <= r.ncavps,
    pncav: r.ncavps != null && price != null && r.ncavps > 0 ? price / r.ncavps : null,
    idx: r.index_memberships ?? [],
    pe3: pe3(r), pe: byN(r, 1)?.value ?? null,
    returnQuality: qualities[index],
    dividendYield: dps != null && price > 0 ? dps / price * 100 : null,
    roic: r.owner_earnings?.all_capex_return ?? null,
    profile: r.graham_profile ?? "REVIEW",
    hay: [r.ticker, r.name, r.sector, r.industry, r.exchange, r.graham_profile,
      r.incorporation?.split("|")[1]].filter(Boolean).join(" ").toLowerCase(),
  };
});
process.stdout.write(JSON.stringify(projected));

"""


def company(value=10, debt=0):
    return {
        "ticker": "A", "criteria": [{"n": 7, "status": "PASS", "value": 3},
                                     {"n": 1, "status": "FAIL", "value": 11}],
        "price": 30, "shares": 100, "ncavps": 30, "price_asof": "2026-01-01",
        "annual_eps": {"2025": 5, "2024": 5, "2023": 5},
        "profitability": {"on_equity": value},
        "operating_returns": {"nopat_roic": value, "ronta": value},
        "debt_to_equity": debt,
        "owner_earnings": {"fiscal_year": 2025, "annual_per_share": {
            "2025": {"free_cash_flow": value, "cash_flow_bridge": {
                "operating_cash_flow": [100], "total_capital_expenditure": [0]}}}},
        "annual_revenue": {"2025": 100},
    }


def test_projection_keeps_criteria_numbered_strict_and_source_unchanged():
    row = company()
    row["criteria"].append({"n": 2, "status": "NOT_APPLICABLE", "value": None})
    before = copy.deepcopy(row)
    result = project_companies([row])[0]
    assert criterion(row, 1)["value"] == result["pe"] == 11
    assert result["unjudged"] and result["inapplicable"]
    assert result["netNet"] and result["pncav"] == 1
    assert row == before
    row["shares"] = 0
    assert project_companies([row])[0]["mcap"] is None


def test_pe3_uses_available_years_and_refuses_stale_quote_basis():
    row = company()
    row["annual_eps"] = {"2025": 6, "2022": 3, "2020": None}
    assert pe3(row) == 10
    row["price_asof"] = "2028-01-01"
    assert pe3(row) is None
    row["price_asof"] = "2027-01-01"
    assert pe3(row) == 10
    row["reporting_currency"], row["quote_currency"] = "JPY", "USD"
    assert pe3(row) is None
    row["price_reporting_currency"] = 3000
    assert pe3(row) == 1000


def test_annual_window_preserves_missing_zero_negative_and_growth_basis():
    result = eps_evidence({"2025": 2, "2024": 0, "2023": -1, "2015": 30})
    assert result["positive"] == 1 and result["present"] == 3
    assert result["years"] == list(range(2016, 2026))
    assert result["growth"] is None and not result["stable"]
    annual = {str(year): 1 for year in range(2013, 2026)}
    annual.update({"2023": 2, "2024": 2, "2025": 2})
    assert eps_evidence(annual)["growth"] == 100


def test_nearly_cancelling_eps_retains_existing_binary_arithmetic():
    row = company()
    row.update(price=0.8254, annual_eps={"2023": -0.09, "2024": 0.07, "2025": 0.02})
    assert pe3(row) == 1.784290143567172e17
    annual = {"2013": 0.02, "2014": -0.09, "2015": 0.07,
              "2023": -0.24, "2024": -0.34, "2025": -0.02}
    assert eps_evidence(annual)["growth"] == -4.3234556422756777e18


def test_nonfinite_and_boolean_inputs_are_missing_not_measured_zero():
    row = company(float("inf"), debt=-1)
    row["profitability"]["on_equity"] = True
    row["owner_earnings"]["fiscal_year"] = 2025.0
    quality = return_quality_percentiles([row])[0]
    assert quality["roe"] is None and quality["roic"] is None and quality["debtToEquity"] is None
    assert quality["score"] == 0
    row["owner_earnings"]["annual_per_share"]["2025"]["free_cash_flow"] = 0
    assert not return_quality_percentiles([row])[0]["cashFlowMissing"]


def test_overflowing_median_is_unavailable_without_removing_cash_evidence():
    row = company()
    cash = row["owner_earnings"]["annual_per_share"]
    cash["2024"] = copy.deepcopy(cash["2025"])
    for cell in cash.values():
        cell["free_cash_flow"] = 1e308
        cell["cash_flow_bridge"]["total_capital_expenditure"] = [1e308]
    row["annual_revenue"]["2024"] = 100
    quality = return_quality_percentiles([row])[0]
    assert quality["fcfRevenue"] is None and quality["capexOcf"] is None
    assert quality["fcfRevenueYears"] == quality["capexOcfYears"] == 2
    assert quality["missingInputs"] == ["FCF/revenue", "CapEx/OCF"]
    assert quality["score"] == pytest.approx(82.608695652159)


def test_ties_and_singletons_use_upper_rank_and_lower_is_better():
    one = return_quality_percentiles([company()])[0]
    tied = return_quality_percentiles([company(), company()])
    assert one["returnScore"] == pytest.approx(70)
    assert one["score"] == pytest.approx(70)
    assert tied[0] == tied[1] == one
    low, high = return_quality_percentiles([company(-10), company(10)])
    assert low["returnScore"] == 0
    assert high["returnScore"] == pytest.approx(70)


def test_missing_cash_rows_still_contribute_available_peer_inputs():
    good, missing = company(), company(20)
    del missing["owner_earnings"]["annual_per_share"]["2025"]["free_cash_flow"]
    good_quality, missing_quality = return_quality_percentiles([good, missing])
    assert missing_quality["score"] == 0 and missing_quality["returnScore"] == 0
    assert "highDebtWeight" not in missing_quality
    assert good_quality["returnScore"] == pytest.approx(22.5)
    assert good_quality["score"] < return_quality_percentiles([good])[0]["score"]


def test_cash_evidence_and_margins_use_ten_fixed_slots_and_disclose_assumptions():
    row = company(debt=1)
    cash = row["owner_earnings"]["annual_per_share"]
    cash["2015"] = cash.pop("2025")
    assert return_quality_percentiles([row])[0]["score"] == 0
    cash["2025"] = {"free_cash_flow": -10}
    cash["2024"] = {"cash_flow_bridge": {"operating_cash_flow": {"value": 0}}}
    quality = return_quality_percentiles([row])[0]
    assert not quality["cashFlowMissing"]
    assert quality["fcfRevenue"] == -10 and quality["capexOcf"] is None
    row["return_quality_assumption"] = {
        "status": "APPLIED", "roe": 5, "nopat_roic": 0, "ronta": 0,
        "debt_to_equity": 0, "applied": ["operating_income"],
        "input_assumptions": {"nopat_roic": ["operating_income"], "ronta": ["operating_income"]},
    }
    quality = return_quality_percentiles([row])[0]
    assert quality["roic"] is None and quality["ronta"] is None
    assert quality["assumedInputs"]["roic"] and not quality["assumedInputs"]["ronta"]


def assert_parity(actual, expected, path="projection"):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), path
        for key in expected:
            assert_parity(actual[key], expected[key], f"{path}.{key}")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), path
        for index, (left, right) in enumerate(zip(actual, expected)):
            assert_parity(left, right, f"{path}[{index}]")
    elif type(expected) in (int, float):
        assert actual == pytest.approx(expected, abs=1e-9, rel=1e-15), path
    else:
        assert actual == expected, path


def test_javascript_oracle_for_adversarial_cohort():
    rows = [company(value, debt) for value, debt in ((-10, 0), (0, 1), (20, 3), (20, None))]
    rows.append({"criteria": [], "ticker": "無", "sector": "Énergie", "price": 0})
    rows[0]["return_quality_assumption"] = {
        "status": "APPLIED", "roe": 0, "nopat_roic": 0, "ronta": 5,
        "debt_to_equity": 0, "applied": ["operating_income"],
        "input_assumptions": {"nopat_roic": ["operating_income"]},
    }
    rows[1]["owner_earnings"]["annual_per_share"]["2025"]["cash_flow_bridge"] = {
        "operating_cash_flow": {"value": 100}, "total_capital_expenditure": -50}
    rows[2]["annual_eps"] = {"2025": 5, "2023": None, "2020": 3}
    rows[2]["price_asof"] = "2028-01-01"
    rows[3]["operating_returns"] = {}
    rows[3]["operating_returns_estimate"] = {
        "status": "CONSERVATIVE_LOWER_BOUND", "nopat_roic": 5,
        "operating_return_assumptions": ["intangibles"], "note": "Estimated"}
    extreme = company(1e308)
    cash = extreme["owner_earnings"]["annual_per_share"]
    cash["2024"] = copy.deepcopy(cash["2025"])
    extreme["annual_revenue"]["2024"] = 100
    rows.append(extreme)
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(["node", "--input-type=module", "-e", _ORACLE], cwd=root, input=json.dumps(rows),
                            capture_output=True, text=True, check=True, encoding="utf-8")
    assert_parity(project_companies(rows), json.loads(result.stdout))
