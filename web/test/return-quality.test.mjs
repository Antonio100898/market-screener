import assert from "node:assert/strict";
import test from "node:test";

import { returnQuality, returnQualityPercentiles } from "../src/screen.js";

const row = (roe, roic, ronta, debtToEquity = 0, estimate = null, assumption = null) => ({
  profitability: { on_equity: roe },
  operating_returns: { nopat_roic: roic, ronta },
  operating_returns_estimate: estimate,
  return_quality_assumption: assumption ? { invalid_inputs: {}, ...assumption } : null,
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

test("sparse returns retain missing weights and the maximum unknown debt penalty", () => {
  const sparse = returnQuality(row(30, null, null, null));
  assert.equal(sparse.returnScore, 40);
  assert.equal(sparse.score, 20);
  assert.equal(sparse.inputsPresent, 3);
  assert.deepEqual(sparse.missingInputs, ["ROIC", "RONTA", "D/E"]);
  assert.equal(sparse.noReturnEvidence, false);
  const negativeDebt = returnQuality(row(30, 30, 30, -1));
  assert.equal(negativeDebt.score, 40);
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
  assert.equal(quality.score, 40);
  assert.equal(quality.roic, null);
  assert.equal(quality.ronta, null);
  assert.equal(quality.estimated, false);
});

test("validated zero-assumption return estimates receive 75 percent credit", () => {
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
  assert.equal(quality.score, 69.6875);
  assert.equal(quality.roic, 20);
  assert.equal(quality.ronta, 40);
  assert.equal(quality.debtToEquity, 0);
  assert.equal(quality.debtAssumed, true);
  assert.equal(quality.assumptionMode, true);
  assert.equal(quality.assumptionApplied, true);
  assert.equal(quality.estimated, true);
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

  assert.equal(quality.score, 40);
  assert.equal(quality.returnScore, 40);
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
  assert.equal(returnQuality(company).score, 0);
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

test("ranking rejects invalid capital returns while keeping measured losses", () => {
  const company = row(-20, -10, null, 0, null, {
    status: "APPLIED", roe: 999, nopat_roic: 844, ronta: 500,
    debt_to_equity: 0, applied: ["intangibles"],
    input_assumptions: { nopat_roic: [], ronta: ["intangibles"] },
    invalid_inputs: { nopat_roic: "Capital is negative" },
  });
  const quality = returnQuality(company);
  assert.equal(quality.roe, -20);
  assert.equal(quality.roic, -10);
  assert.equal(quality.ronta, 500);
  assert.equal(quality.estimatedInputs.ronta, true);
  company.operating_returns.nopat_roic = null;
  assert.equal(returnQuality(company).roic, null);
});

test("a legacy overlay without capital validation cannot supply ranked returns", () => {
  const company = row(30, null, null, 0, null, {
    status: "APPLIED", roe: 30, nopat_roic: 844, ronta: 600,
    debt_to_equity: 0, input_assumptions: { nopat_roic: ["short_term_debt"] },
  });
  delete company.return_quality_assumption.invalid_inputs;
  const quality = returnQuality(company);
  assert.equal(quality.roic, null);
  assert.equal(quality.ronta, null);
  assert.equal(quality.estimated, false);
});

test("estimated returns keep their weights but receive exactly 75 percent of percentile credit", () => {
  const peers = [row(10, 10, 10), row(50, 50, 50)];
  const measured = row(30, 30, 30);
  const estimated = row(30, null, null, 0, null, {
    status: "APPLIED", roe: 30, nopat_roic: 30, ronta: 30,
    debt_to_equity: 0, input_assumptions: {
      nopat_roic: ["short_term_investments"], ronta: ["intangibles"],
    },
  });
  const before = returnQualityPercentiles([...peers, measured]).at(-1);
  const after = returnQualityPercentiles([...peers, estimated]).at(-1);
  // Both returns rank at 50. Their combined weight is 31 2/3 percent.
  assert.ok(Math.abs((before.returnScore - after.returnScore) - 3.95833333333) < 1e-9);
  assert.ok(after.score < before.score);
  assert.equal(after.inputsPresent, 6);
  assert.deepEqual(after.estimatedInputs, { roic: true, ronta: true });
});

test("measured inputs do not inherit warnings from an unused assumption overlay", () => {
  const company = row(30, 20, 40, 0, null, {
    status: "APPLIED", roe: null, nopat_roic: null, ronta: 500,
    debt_to_equity: 0, applied: ["debt", "intangibles"],
    input_assumptions: { roe: ["equity"], debt_to_equity: ["debt"], ronta: ["intangibles"] },
    invalid_inputs: { roe: "Invalid equity", nopat_roic: "Invalid capital" },
  });
  const quality = returnQuality(company);
  assert.equal(quality.inputsPresent, 6);
  assert.equal(quality.assumptionApplied, false);
  assert.deepEqual(quality.invalidInputs, {});
  assert.ok(Object.values(quality.assumedInputs).every((value) => !value));
});

test("removing any scoring input cannot improve the company rank score", () => {
  const remove = {
    ROE: (r) => { r.profitability.on_equity = null; },
    ROIC: (r) => { r.operating_returns.nopat_roic = null; },
    RONTA: (r) => { r.operating_returns.ronta = null; },
    "FCF/revenue": (r) => { r.annual_revenue = {}; },
    "D/E": (r) => { r.debt_to_equity = null; },
    "CapEx/OCF": (r) => { r.owner_earnings.annual_per_share[2025].cash_flow_bridge.total_capital_expenditure = null; },
  };
  for (const debt of [0, 0.8, 1, 3]) {
    for (const returns of [-30, 30, 100]) {
      const peers = [setCashFlow(row(-50, -50, -50, 0), -50, 0),
        setCashFlow(row(10, 10, 10, 0.5), 10, 30),
        setCashFlow(row(200, 200, 200, 4), 50, 200)];
      const target = setCashFlow(row(returns, returns, returns, debt), 20, 80);
      const before = returnQualityPercentiles([...peers, target]).at(-1).score;
      for (const [name, erase] of Object.entries(remove)) {
        const missing = structuredClone(target);
        erase(missing);
        const after = returnQualityPercentiles([...peers, missing]).at(-1);
        assert.ok(after.score <= before + 1e-10,
          `${name} missing at D/E ${debt}: ${after.score} > ${before}`);
        assert.ok(after.missingInputs.includes(name));
      }
    }
  }
});

test("unreported debt uses the disclosed zero-debt assumption", () => {
  const company = row(30, 30, 30, null, null, {
    status: "APPLIED", roe: 30, nopat_roic: 30, ronta: 30,
    debt_to_equity: 0, applied: ["debt"],
    input_assumptions: { debt_to_equity: ["debt"] },
  });
  const quality = returnQualityPercentiles([company])[0];
  assert.equal(quality.debtToEquity, 0);
  assert.equal(quality.debtAssumed, true);
  assert.ok(!quality.missingInputs.includes("D/E"));
  assert.equal(quality.highDebtWeight, false);
  assert.equal(quality.score, quality.returnScore);
});

test("debt-only assumptions can support valid capital returns but missing equity cannot imply zero leverage", () => {
  const company = row(30, null, null, null, null, {
    status: "APPLIED", roe: 30, nopat_roic: 20, ronta: 40,
    debt_to_equity: 0, applied: ["short_term_debt"],
    input_assumptions: { nopat_roic: ["short_term_debt"], ronta: ["short_term_debt"], debt_to_equity: ["short_term_debt"] },
  });
  const quality = returnQuality(company);
  assert.equal(quality.roic, 20);
  assert.equal(quality.ronta, 40);
  assert.equal(quality.debtToEquity, 0);
  company.return_quality_assumption.debt_to_equity = null;
  const unavailable = returnQualityPercentiles([company])[0];
  assert.equal(unavailable.debtToEquity, null);
  assert.equal(unavailable.debtAssumed, false);
  assert.equal(unavailable.highDebtWeight, true);
  assert.equal(unavailable.score, unavailable.returnScore / 2);
});

test("missing capital-spending evidence cannot remove its unbounded penalty", () => {
  const company = row(30, 30, 30);
  delete company.owner_earnings.annual_per_share[2025].cash_flow_bridge.total_capital_expenditure;
  const quality = returnQualityPercentiles([company])[0];
  assert.equal(quality.cashFlowMissing, false);
  assert.equal(quality.capexOcf, null);
  assert.equal(quality.score, 0);
  assert.equal(returnQuality(company).score, 0);
});

test("percentile Return Quality applies debt and CapEx divisors after the weighted base", () => {
  const target = setCashFlow(row(0, 10, 10, 1), 10, 50);
  const lower = setCashFlow(row(10, 0, 0, 0), 0, 100);
  const higher = setCashFlow(row(20, 20, 20, 2), 20, 0);

  const quality = returnQualityPercentiles([target, lower, higher])[0];

  assert.ok(Math.abs(quality.returnScore - 47.083333333315) < 1e-9);
  assert.ok(Math.abs(quality.score - quality.returnScore / 2 / 1.5) < 1e-12);
  assert.equal(quality.highDebtWeight, true);
});

test("percentile Return Quality cannot ignore unavailable penalties", () => {
  const company = row(10, 10, 10, null);
  delete company.owner_earnings.annual_per_share[2025].cash_flow_bridge.total_capital_expenditure;

  const quality = returnQualityPercentiles([company])[0];

  assert.equal(quality.debtToEquity, null);
  assert.equal(quality.capexOcf, null);
  assert.equal(quality.score, 0);
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
