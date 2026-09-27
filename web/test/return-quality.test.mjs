import assert from "node:assert/strict";
import test from "node:test";

import { returnQuality, returnQualityPercentiles } from "../src/screen.js";

const row = (roe, roic, ronta, debtToEquity = 0, estimate = null, assumption = null) => ({
  profitability: { on_equity: roe },
  operating_returns: { nopat_roic: roic, ronta },
  operating_returns_estimate: estimate,
  return_quality_assumption: assumption,
  debt_to_equity: debtToEquity,
  owner_earnings: {
    fiscal_year: 2025,
    annual_per_share: { 2025: { free_cash_flow: 30,
      cash_flow_bridge: { total_capital_expenditure: [0], operating_cash_flow: [100] } } },
  },
  annual_revenue: { 2025: 100 },
});

test("four 30 percent returns give a score of 80", () => {
  const balanced = returnQuality(row(30, 30, 30));
  assert.equal(balanced.score, 80);
  assert.equal(balanced.inputsPresent, 6);
  assert.deepEqual(balanced.missingInputs, []);
  assert.equal(balanced.noReturnEvidence, false);
  assert.deepEqual(
    { roe: balanced.roe, roic: balanced.roic, ronta: balanced.ronta },
    { roe: 30, roic: 30, ronta: 30 },
  );
});

test("one denominator-driven outlier cannot dominate return quality", () => {
  const outlier = returnQuality(row(1600, 20, 40));
  const genuinelyStrong = returnQuality(row(80, 80, 80));

  assert.ok(outlier.score < 85);
  assert.ok(genuinelyStrong.score > outlier.score);
});

test("debt to equity penalizes returns by no more than half", () => {
  const debtFree = returnQuality(row(60, 40, 50, 0));
  const leveraged = returnQuality(row(60, 40, 50, 3));

  assert.equal(leveraged.returnScore, debtFree.returnScore);
  assert.equal(leveraged.score, debtFree.score / 2);
  assert.equal(returnQuality(row(60, 40, 50, 1)).score, leveraged.score);
});

test("sparse returns are scored from available inputs and missing penalties are disclosed", () => {
  const sparse = returnQuality(row(30, null, null, null));
  assert.equal(sparse.score, 80);
  assert.equal(sparse.inputsPresent, 3);
  assert.deepEqual(sparse.missingInputs, ["ROIC", "RONTA", "D/E"]);
  assert.equal(sparse.noReturnEvidence, false);
  const negativeDebt = returnQuality(row(30, 30, 30, -1));
  assert.equal(negativeDebt.score, 80);
  assert.equal(negativeDebt.debtToEquity, null);
  assert.deepEqual(negativeDebt.missingInputs, ["D/E"]);
});

test("a disclosed conservative estimate fills only strict blanks", () => {
  const estimate = {
    status: "CONSERVATIVE_LOWER_BOUND",
    nopat_roic: 20,
    ronta: 40,
    operating_return_assumptions: ["intangibles"],
  };
  const quality = returnQuality(row(30, null, null, 0, estimate));

  assert.ok(quality.score > 0);
  assert.equal(quality.estimated, true);
  assert.deepEqual(quality.estimatedInputs, { roic: true, ronta: true });
  assert.deepEqual(quality.assumptions, ["intangibles"]);
});

test("an exact reported return always wins over its estimate", () => {
  const estimate = {
    status: "CONSERVATIVE_LOWER_BOUND",
    nopat_roic: 99,
    ronta: 99,
  };
  const quality = returnQuality(row(30, 20, null, 0, estimate));

  assert.equal(quality.roic, 20);
  assert.equal(quality.ronta, 99);
  assert.deepEqual(quality.estimatedInputs, { roic: false, ronta: true });
});

test("an undisclosed number cannot masquerade as a conservative estimate", () => {
  const quality = returnQuality(row(30, null, null, 0, { nopat_roic: 20, ronta: 40 }));
  assert.equal(quality.score, 80);
  assert.equal(quality.roic, null);
  assert.equal(quality.ronta, null);
  assert.equal(quality.estimated, false);
});

test("the exported zero-assumption inputs drive Return Quality by default", () => {
  const assumption = {
    status: "APPLIED",
    roe: 30,
    nopat_roic: 20,
    ronta: 40,
    debt_to_equity: 0,
    applied: ["debt", "intangibles"],
    input_assumptions: {
      roe: [],
      nopat_roic: ["intangibles"],
      ronta: ["intangibles"],
      debt_to_equity: ["debt"],
    },
  };
  const quality = returnQuality(row(30, null, null, null, null, assumption));

  assert.ok(quality.score > 0);
  assert.equal(quality.assumptionMode, true);
  assert.equal(quality.assumptionApplied, true);
  assert.equal(quality.estimated, false);
  assert.deepEqual(quality.assumedInputs, {
    roe: false, roic: true, ronta: true, debtToEquity: true,
  });
  assert.deepEqual(quality.assumptions, ["debt", "intangibles"]);
});

test("operating income assumed absent is omitted instead of scored as measured zero", () => {
  const assumption = {
    status: "APPLIED",
    roe: 30,
    nopat_roic: 0,
    ronta: 0,
    debt_to_equity: 0,
    applied: ["operating_income"],
    input_assumptions: {
      nopat_roic: ["operating_income"],
      ronta: ["operating_income"],
    },
  };
  const quality = returnQuality(row(30, null, null, null, null, assumption));

  assert.equal(quality.score, 80);
  assert.equal(quality.returnScore, 80);
  assert.equal(quality.roic, null);
  assert.equal(quality.ronta, null);
  assert.deepEqual(quality.missingInputs, ["ROIC", "RONTA"]);
});

test("FCF margin carries equal weight and a higher margin raises the score", () => {
  const company = row(10, 20, 40, 1);
  company.owner_earnings.annual_per_share[2025].free_cash_flow = 80;
  const quality = returnQuality(company);
  assert.equal(quality.fcfRevenue, 80);
  assert.equal(quality.fcfRevenueYears, 1);
  assert.equal(quality.returnScore, 78.75);
  assert.equal(quality.score, quality.returnScore / 2);
  company.owner_earnings.annual_per_share[2025].free_cash_flow = 40;
  assert.ok(returnQuality(company).score < quality.score);
});

test("zero, negative and missing FCF margins stay distinct", () => {
  const company = row(30, 30, 30);
  company.owner_earnings.annual_per_share[2025].free_cash_flow = 0;
  assert.equal(returnQuality(company).score, 72.5);
  company.owner_earnings.annual_per_share[2025].free_cash_flow = -10;
  assert.ok(Math.abs(returnQuality(company).score - 68.33333333333333) < 1e-12);
  assert.equal(returnQuality(company).fcfRevenue, -10);
  delete company.owner_earnings;
  assert.equal(returnQuality(company).score, 0);
  assert.equal(returnQuality(company).fcfRevenue, null);
  assert.equal(returnQuality(company).fcfRevenueYears, 0);
});

test("the median CapEx/OCF penalty lowers the score without changing operating returns", () => {
  const company = row(30, 30, 30);
  const bridge = company.owner_earnings.annual_per_share[2025].cash_flow_bridge;
  const initial = returnQuality(company);
  bridge.total_capital_expenditure = [50];
  const penalized = returnQuality(company);
  assert.equal(penalized.score, 80 / 1.5);
  assert.equal(penalized.capexOcf, 50);
  assert.equal(penalized.capexOcfYears, 1);
  assert.equal(penalized.returnScore, initial.returnScore);
  bridge.total_capital_expenditure = [100];
  assert.equal(returnQuality(company).score, 40);
  bridge.operating_cash_flow = [0];
  assert.equal(returnQuality(company).score, 80);
  assert.deepEqual(returnQuality(company).missingInputs, ["CapEx/OCF"]);
  delete bridge.operating_cash_flow;
  assert.equal(returnQuality(company).score, 0);
  delete bridge.total_capital_expenditure;
  assert.equal(returnQuality(company).capexOcf, null);
});

test("positive, zero and negative measured returns have known bounded scores", () => {
  for (const [value, expected] of [[20, 75], [0, 50], [-20, 25]]) {
    const company = row(value, value, value);
    company.owner_earnings.annual_per_share[2025].free_cash_flow = value;
    const quality = returnQuality(company);
    assert.equal(quality.score, expected);
    assert.equal(quality.inputsPresent, 6);
    assert.equal(quality.noReturnEvidence, false);
  }
});

test("missing OCF or FCF makes Return Quality zero", () => {
  const quality = returnQuality({});
  assert.equal(quality.score, 0);
  assert.equal(quality.returnScore, 0);
  assert.equal(quality.noReturnEvidence, true);
  assert.equal(quality.cashFlowMissing, true);
  assert.equal(quality.inputsPresent, 0);
  assert.deepEqual(quality.missingInputs, ["ROE", "ROIC", "RONTA", "FCF/revenue", "D/E", "CapEx/OCF"]);
  const penalized = returnQuality({ debt_to_equity: 1 });
  assert.equal(penalized.score, 0);
  assert.equal(penalized.noReturnEvidence, true);

  const fcfMissing = row(30, 30, 30);
  delete fcfMissing.owner_earnings.annual_per_share[2025].free_cash_flow;
  assert.equal(returnQuality(fcfMissing).score, 0);
  assert.equal(returnQuality(fcfMissing).cashFlowMissing, true);
});

test("each return increases the score monotonically through negative and positive values", () => {
  for (const input of ["roe", "roic", "ronta", "fcf"]) {
    let previous = -1;
    for (const value of [-1000, -20, -8.8, 0, 20, 1000]) {
      const company = row(30, 30, 30);
      if (input === "roe") company.profitability.on_equity = value;
      if (input === "roic") company.operating_returns.nopat_roic = value;
      if (input === "ronta") company.operating_returns.ronta = value;
      if (input === "fcf") company.owner_earnings.annual_per_share[2025].free_cash_flow = value;
      const score = returnQuality(company).score;
      assert.ok(score > previous, `${input} must improve at ${value}`);
      previous = score;
    }
  }
});

test("extreme and unavailable inputs always produce a finite score from 0 to 100", () => {
  for (const value of [-Number.MAX_VALUE, -1e6, 0, 1e6, Number.MAX_VALUE, NaN, Infinity, null]) {
    for (const debt of [0, 1, Number.MAX_VALUE, null, -1, Infinity]) {
      const company = row(value, value, value, debt);
      delete company.owner_earnings;
      const quality = returnQuality(company);
      assert.ok(Number.isFinite(quality.score));
      assert.ok(quality.score >= 0 && quality.score <= 100);
    }
  }
});

const setCashFlow = (company, fcf, capex, ocf = 100) => {
  const cell = company.owner_earnings.annual_per_share[2025];
  cell.free_cash_flow = fcf;
  cell.cash_flow_bridge.total_capital_expenditure = [capex];
  cell.cash_flow_bridge.operating_cash_flow = [ocf];
  return company;
};

test("percentile Return Quality applies debt and CapEx divisors after the weighted base", () => {
  const target = setCashFlow(row(0, 10, 10, 1), 10, 50);
  const lower = setCashFlow(row(10, 0, 0, 0), 0, 100);
  const higher = setCashFlow(row(20, 20, 20, 2), 20, 0);

  const quality = returnQualityPercentiles([target, lower, higher])[0];

  assert.ok(Math.abs(quality.returnScore - 47.083333333315) < 1e-9);
  assert.ok(Math.abs(quality.score - quality.returnScore / 2 / 1.5) < 1e-12);
  assert.equal(quality.highDebtWeight, true);
});

test("percentile Return Quality leaves unavailable penalties unassessed", () => {
  const company = row(10, 10, 10, null);
  delete company.owner_earnings.annual_per_share[2025].cash_flow_bridge.total_capital_expenditure;

  const quality = returnQualityPercentiles([company])[0];

  assert.equal(quality.debtToEquity, null);
  assert.equal(quality.capexOcf, null);
  assert.equal(quality.score, quality.returnScore);
});

test("percentile Return Quality is zero when OCF or FCF is missing", () => {
  const missingFcf = row(30, 30, 30);
  delete missingFcf.owner_earnings.annual_per_share[2025].free_cash_flow;
  const missingOcf = row(30, 30, 30);
  delete missingOcf.owner_earnings.annual_per_share[2025].cash_flow_bridge.operating_cash_flow;

  for (const quality of returnQualityPercentiles([missingFcf, missingOcf])) {
    assert.equal(quality.returnScore, 0);
    assert.equal(quality.score, 0);
  }
});

test("cash-flow evidence outside the latest ten fiscal years cannot avoid zero", () => {
  const company = row(30, 30, 30);
  company.owner_earnings.annual_per_share = {
    2015: company.owner_earnings.annual_per_share[2025],
  };
  company.annual_revenue = { 2015: 100 };

  const quality = returnQualityPercentiles([company])[0];

  assert.equal(quality.cashFlowMissing, true);
  assert.equal(quality.returnScore, 0);
  assert.equal(quality.score, 0);
});

test("percentile Return Quality always remains within 0 to 100", () => {
  const companies = [
    setCashFlow(row(-1e6, -1e6, -1e6, 0), -1e6, 0),
    setCashFlow(row(0, 0, 0, 1), 0, 100),
    setCashFlow(row(1e6, 1e6, 1e6, Number.MAX_VALUE), 1e6, Number.MAX_VALUE),
    {},
  ];

  for (const quality of returnQualityPercentiles(companies)) {
    assert.ok(Number.isFinite(quality.returnScore));
    assert.ok(Number.isFinite(quality.score));
    assert.ok(quality.score >= 0 && quality.score <= 100);
  }
});
