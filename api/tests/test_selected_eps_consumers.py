"""Synthetic emitted-row controls; no source admission or Snapshot construction."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess

import pytest

from screener import company_projection, portfolio


def row(annual=None, selected=2024):
    return {
        "price": 30, "price_asof": "2025-01-01", "shares": 100,
        "currency": "USD", "bvps": 10, "tbvps": 5, "ncavps": 20,
        "ttm_eps": 2, "recurring_dividend_per_share": 1.5,
        "criteria": [{"n": 1, "status": "PASS", "value": 15}],
        "annual_eps": {"2023": 3, "2022": 3, "2021": 3} if annual is None else annual,
        "ch13": {"latest_fy": selected},
    }


@pytest.mark.parametrize("mode", ["STRICT", "APPLIED"])
def test_selected_missing_current_year_reaches_projection_and_execution(monkeypatch, mode):
    value = row()
    value["assumption_mode"] = {"status": mode}
    before = deepcopy(value)
    projected = company_projection.project_companies([value])[0]
    assert projected["pe3"] is None
    assert projected["eps10"]["latest"] == 2024
    assert projected["eps10"]["values"][-1] is None
    assert projected["eps10"]["present"] == projected["eps10"]["positive"] == 3
    assert portfolio._pe3(value, Decimal("30")) is None
    monkeypatch.setattr(portfolio, "_price_on_reporting_basis", lambda _, price: price)
    execution = portfolio.valuation_at_price(value, "30")
    assert execution["pe3"] is None
    assert execution["defensive_product"] is None
    assert execution["defensive_valuation"] is None
    assert {key: execution[key] for key in ("price", "price_reporting_currency", "pe", "pb", "ptbv", "pncav")} == {
        "price": 30, "price_reporting_currency": 30, "pe": 15, "pb": 3, "ptbv": 6, "pncav": 1.5,
    }
    assert projected["mcap"] == 3000 and projected["pncav"] == 1.5
    assert projected["pe"] == 15 and projected["dividendYield"] == 5
    assert value == before


def test_empty_selected_year_is_a_labelled_missing_record():
    projected = company_projection.project_companies([row({}, 2024)])[0]
    assert projected["pe3"] is None
    assert projected["eps10"] == {
        "years": list(range(2015, 2025)), "values": [None] * 10,
        "present": 0, "positive": 0, "stable": False, "growth": None,
        "latest": 2024, "growthBaseYears": [2012, 2013, 2014],
        "growthRecentYears": [2022, 2023, 2024],
    }
    assert company_projection.eps_evidence({}) is None


def test_unavailable_current_year_does_not_certify_old_stability_or_growth():
    annual = {str(year): 3 for year in range(2011, 2024)}
    value = row(annual)
    evidence = company_projection.project_companies([value])[0]["eps10"]
    assert evidence["latest"] == 2024
    assert evidence["present"] == evidence["positive"] == 9
    assert evidence["stable"] is False and evidence["growth"] is None
    assert value["annual_eps"] == annual


@pytest.mark.parametrize("selected", [None, True, 2024.0, "2024", 2020, 2023])
def test_no_newer_accepted_label_preserves_current_arithmetic(selected):
    value = row(selected=selected)
    assert company_projection.pe3(value) == 10
    assert portfolio._pe3(value, Decimal("30")) == Decimal("10")
    assert company_projection.project_companies([value])[0]["eps10"]["latest"] == 2023


def test_sparse_and_explicit_null_definitions_remain_distinct():
    value = row({"2025": 6, "2022": 3, "2020": None}, selected=2025)
    assert company_projection.pe3(value) == 10
    assert portfolio._pe3(value, Decimal("30")) is None
    value["annual_eps"]["2020"] = 3
    assert company_projection.pe3(value) == 7.5
    assert portfolio._pe3(value, Decimal("30")) == Decimal("7.5")


@pytest.mark.parametrize("latest", [0, -1])
def test_present_current_zero_or_loss_remains_a_measured_observation(latest):
    value = row({"2024": latest, "2023": 3, "2022": 3})
    evidence = company_projection.project_companies([value])[0]["eps10"]
    assert evidence["latest"] == 2024 and evidence["values"][-1] == latest
    assert evidence["present"] == 3 and evidence["positive"] == 2
    assert company_projection.pe3(value) == 30 / ((latest + 6) / 3)
    assert portfolio._pe3(value, Decimal("30")) == Decimal("30") / ((Decimal(latest) + 6) / 3)


def test_historical_gap_does_not_hide_a_present_current_year():
    value = row({"2024": 3, "2023": 3, "2022": 3, "2020": None})
    assert company_projection.pe3(value) == 10
    assert portfolio._pe3(value, Decimal("30")) == Decimal("10")
    evidence = company_projection.project_companies([value])[0]["eps10"]
    assert evidence["latest"] == 2024 and evidence["values"][-1] == 3
    assert evidence["values"][5] is None


def test_current_python_and_js_owners_agree_on_selected_and_legacy_rows():
    rows = [row(), row({}), row(selected=2023),
            row({"2025": 6, "2022": 3, "2020": None}, 2025),
            row({"2024": 0, "2023": 3, "2022": 3})]
    javascript = """
import fs from 'node:fs';
import { pe3 } from './web/src/screen.js';
import { epsEvidence } from './web/src/earningsEvidence.js';
const rows = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(rows.map(row => ({
  pe3: pe3(row), eps10: epsEvidence(row.annual_eps, row.ch13?.latest_fy)
}))));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", javascript],
        input=json.dumps(rows), capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[2], check=True, timeout=20,
    )
    projected = company_projection.project_companies(rows)
    assert json.loads(result.stdout) == [
        {key: value[key] for key in ("pe3", "eps10")} for value in projected
    ]
