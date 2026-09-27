import assert from "node:assert/strict";
import test from "node:test";
import { ownerEarningsTrend, ownerMetricTrend, fcfRevenueHistory, capexOcfHistory } from "../src/ownerEarnings.js";

test("CapEx/OCF takes the median of annual ratios in ten fixed slots", () => {
  const annual = Object.fromEntries(Array.from({ length: 12 }, (_, i) => [2015 + i, {
    cash_flow_bridge: { total_capital_expenditure: [(i + 1) ** 2], operating_cash_flow: [(i + 1) * 100] },
  }]));
  assert.deepEqual(capexOcfHistory({owner_earnings: {fiscal_year: 2025, annual_per_share: annual}}),
    {median: 6.5, yearsPresent: 10});
});

test("CapEx/OCF keeps absent and nonpositive OCF out, normalizes CapEx cash-use signs", () => {
  const pair = (capex, ocf) => ({cash_flow_bridge: {total_capital_expenditure: capex, operating_cash_flow: ocf}});
  const company = {owner_earnings: {fiscal_year: 2025, annual_per_share: {
    2016: pair([-20], [100]), 2017: pair({value: 0}, {value: 100}), 2018: pair(120, 200),
    2019: pair(null, 100), 2020: pair(40, 0), 2021: pair(40, -100), 2022: pair(40, null),
    2023: pair(Infinity, 100), 2024: pair(40, Infinity), 2025: pair(40, undefined),
  }}};
  assert.deepEqual(capexOcfHistory(company), {median: 20, yearsPresent: 3});
  assert.deepEqual(capexOcfHistory({}), {median: null, yearsPresent: 0});
});

test("FCF/revenue uses the median of annual margins, not a ratio of totals", () => {
  const annual = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [
    2016 + i, { free_cash_flow: (i + 1) ** 2 },
  ]));
  const revenue = Object.fromEntries(Array.from({ length: 10 }, (_, i) => [2016 + i, (i + 1) * 100]));
  annual[2015] = { free_cash_flow: 10000 };
  revenue[2015] = 100;
  annual[2026] = { free_cash_flow: 10000 };
  revenue[2026] = 100;
  assert.deepEqual(fcfRevenueHistory({
    owner_earnings: { fiscal_year: 2025, annual_per_share: annual },
    annual_revenue: revenue,
  }), { median: 5.5, yearsPresent: 10 });
});

test("FCF margin preserves gaps and includes zero and negative years", () => {
  const company = {
    owner_earnings: {
      fiscal_year: 2025,
      annual_per_share: {
        2015: { free_cash_flow: 1000 },
        2016: { free_cash_flow: -20 },
        2017: { free_cash_flow: 0 },
        2018: { free_cash_flow: 90 },
        2019: { free_cash_flow: null },
        2020: { free_cash_flow: 40 },
        2021: { free_cash_flow: 40 },
        2022: { free_cash_flow: 40 },
        2023: { free_cash_flow: Infinity },
        2024: { free_cash_flow: 40 },
        2025: { free_cash_flow: 40 },
      },
    },
    annual_revenue: { 2015: 100, 2016: 100, 2017: 100, 2018: 300, 2019: 100,
      2020: 0, 2021: -100, 2022: null, 2023: 100, 2024: Infinity },
  };
  assert.deepEqual(fcfRevenueHistory(company), { median: 0, yearsPresent: 3 });
  assert.deepEqual(fcfRevenueHistory({}), { median: null, yearsPresent: 0 });
  company.owner_earnings.fiscal_year = null;
  assert.deepEqual(fcfRevenueHistory(company), { median: null, yearsPresent: 0 });
});

function series(values, latest = 2025) {
  return {
    fiscal_year: latest,
    annual_per_share: Object.fromEntries(
      Object.entries(values).map(([year, value]) => [
        year, { maintenance_estimate_per_share: value },
      ]),
    ),
  };
}

test("shows ten years and uses an eleventh as the averaged CAGR start support", () => {
  const values = {};
  for (let year = 2015; year <= 2025; year += 1)
    values[year] = 2 * Math.pow(1.07, year - 2015);

  const trend = ownerEarningsTrend(series(values));
  const averagedStart = (values[2015] + values[2016] + values[2017]) / 3;
  const expected = (Math.pow(values[2025] / averagedStart, 1 / 9) - 1) * 100;

  assert.deepEqual(trend.rows.map((row) => row.fiscalYear),
    [2025, 2024, 2023, 2022, 2021, 2020, 2019, 2018, 2017, 2016]);
  assert.equal(trend.yearsPresent, 10);
  assert.equal(trend.comparableSteps, 9);
  assert.equal(trend.yearsIncreased, 9);
  assert.equal(trend.yearsAtLeastSix, 9);
  assert.ok(Math.abs(trend.cagr - expected) < 1e-9);
  assert.deepEqual(trend.cagrBasis, {
    firstFiscalYear: 2015,
    lastFiscalYear: 2017,
    latestFiscalYear: 2025,
  });
});

test("keeps a missing filing year blank and does not bridge it with a fake YoY", () => {
  const values = Object.fromEntries(
    Array.from({ length: 10 }, (_, i) => [2016 + i, 10 + i]),
  );
  delete values[2021];

  const trend = ownerEarningsTrend(series(values));
  const byYear = Object.fromEntries(trend.rows.map((row) => [row.fiscalYear, row]));

  assert.equal(trend.yearsPresent, 9);
  assert.equal(trend.comparableSteps, 7);
  assert.equal(byYear[2021].perShare, null);
  assert.equal(byYear[2022].yoy, null);
});

test("withholds percentage growth and CAGR when the averaged base is non-positive", () => {
  const trend = ownerEarningsTrend(series({ 2015: -3, 2016: -2, 2017: 1, 2025: 3 }));

  assert.equal(trend.cagr, null);
  assert.equal(trend.rateSteps, 0);
  assert.equal(trend.rows.find((row) => row.fiscalYear === 2017).yoy, null);
});

test("supplements averaged-start CAGR with medians and shorter windows", () => {
  const values = [8, 10, 12, 9, 15, 18, 16, 20, 22, 19, 24];
  const trend = ownerEarningsTrend(series(
    Object.fromEntries(values.map((value, index) => [2015 + index, value])),
  ));

  assert.equal(trend.median10, 17);
  assert.equal(trend.median5, 20);
  assert.equal(trend.yearsProfitable, 10);
  assert.equal(trend.yearsMissing, 0);
  assert.ok(trend.cagr3 > 0);
  assert.ok(trend.cagr5 > 0);
  assert.deepEqual(trend.cagr5Basis, {
    firstFiscalYear: 2020,
    lastFiscalYear: 2022,
    latestFiscalYear: 2025,
  });
  assert.ok(trend.latestVsMedian10 > 40);
});

test("tracks total values and warns when declining totals are hidden by buybacks", () => {
  const annual = {};
  for (let year = 2015; year <= 2025; year += 1) {
    const index = year - 2015;
    const total = 100 - index * 2;
    const shares = 100 - index * 4;
    annual[year] = {
      reported_earnings_assumption: total,
      reported_earnings_assumption_per_share: total / shares,
      diluted_shares: shares,
    };
  }
  const trend = ownerEarningsTrend({ fiscal_year: 2025, annual_per_share: annual });
  assert.ok(trend.cagr > 0);
  assert.ok(trend.totalCagr < 0);
  assert.ok(trend.shareCountCagr < 0);
  assert.equal(trend.buybackDriven, true);
});

test("builds the same stability record for conservative FCF variants", () => {
  const oe = {
    fiscal_year: 2025,
    annual_per_share: {
      2024: { free_cash_flow_after_stock_compensation_per_share: 2 },
      2025: { free_cash_flow_after_stock_compensation_per_share: 3 },
    },
  };
  const trend = ownerMetricTrend(
    oe,
    "free_cash_flow_after_stock_compensation_per_share",
    "free_cash_flow_after_stock_compensation",
  );
  assert.equal(trend.rows[0].perShare, 3);
  assert.equal(trend.rows[1].perShare, 2);
});

test("uses the requested three-year starts for both earnings and FCF CAGRs", () => {
  const annual = {};
  for (let year = 2016; year <= 2026; year += 1) {
    const earningsPerShare = 2 + (year - 2016) * 1.1;
    const fcfPerShare = 3 + Math.pow(year - 2015, 1.4);
    annual[year] = {
      reported_earnings_assumption_per_share: earningsPerShare,
      reported_earnings_assumption: earningsPerShare * 100,
      free_cash_flow_per_share: fcfPerShare,
      free_cash_flow: fcfPerShare * 100,
      diluted_shares: 100,
    };
  }

  const oe = { fiscal_year: 2026, annual_per_share: annual };
  const earningsTrend = ownerEarningsTrend(oe);
  const fcfTrend = ownerMetricTrend(
    oe,
    "free_cash_flow_per_share",
    "free_cash_flow",
  );
  const expected = (key, basisYears, span) => {
    const start = basisYears.reduce((sum, year) => sum + annual[year][key], 0) / 3;
    return (Math.pow(annual[2026][key] / start, 1 / span) - 1) * 100;
  };

  for (const [trend, key] of [
    [earningsTrend, "reported_earnings_assumption_per_share"],
    [fcfTrend, "free_cash_flow_per_share"],
  ]) {
    assert.ok(Math.abs(trend.cagr5 - expected(key, [2021, 2022, 2023], 4)) < 1e-9);
    assert.ok(Math.abs(trend.cagr - expected(key, [2016, 2017, 2018], 9)) < 1e-9);
    assert.deepEqual(trend.cagr5Basis, {
      firstFiscalYear: 2021,
      lastFiscalYear: 2023,
      latestFiscalYear: 2026,
    });
    assert.deepEqual(trend.cagrBasis, {
      firstFiscalYear: 2016,
      lastFiscalYear: 2018,
      latestFiscalYear: 2026,
    });
  }
});

test("withholds a smoothed CAGR when any basis year is missing", () => {
  const values = Object.fromEntries(
    Array.from({ length: 11 }, (_, index) => [2015 + index, 10 + index]),
  );
  delete values[2016];

  const trend = ownerEarningsTrend(series(values));

  assert.equal(trend.cagr, null);
  assert.notEqual(trend.cagr5, null);
});
