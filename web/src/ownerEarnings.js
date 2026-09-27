const finite = (value) => Number.isFinite(value) ? value : null;

function median(values) {
  const ordered = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!ordered.length) return null;
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
}

/** Annual ratios in the cash table's latest ten fiscal-year slots. */
function annualWindow(row) {
  const oe = row.owner_earnings;
  const latestFiscalYear = oe?.fiscal_year;
  if (!Number.isInteger(latestFiscalYear)) return [];
  return Array.from({ length: 10 }, (_, index) => {
    const year = latestFiscalYear - 9 + index;
    return [oe.annual_per_share?.[year], year];
  });
}

function annualRatioHistory(row, inputs) {
  const margins = [];
  for (const [cell, year] of annualWindow(row)) {
    const [numerator, denominator] = inputs(cell, year);
    if (Number.isFinite(numerator) && Number.isFinite(denominator) && denominator > 0) {
      const margin = numerator / denominator * 100;
      if (Number.isFinite(margin)) margins.push(margin);
    }
  }
  return { median: median(margins), yearsPresent: margins.length };
}

export function fcfRevenueHistory(row) {
  return annualRatioHistory(row, (cell, year) => [cell?.free_cash_flow, row.annual_revenue?.[year]]);
}

export function cashFlowEvidence(row) {
  const value = (point) => Array.isArray(point) ? point[0]
    : point && typeof point === "object" ? point.value : point;
  const annual = annualWindow(row).map(([cell]) => cell);
  return {
    fcf: annual.some((cell) => Number.isFinite(cell?.free_cash_flow)),
    ocf: annual.some((cell) => Number.isFinite(value(cell?.cash_flow_bridge?.operating_cash_flow))),
  };
}

export function capexOcfHistory(row) {
  const value = (point) => Array.isArray(point) ? point[0]
    : point && typeof point === "object" ? point.value : point;
  return annualRatioHistory(row, (cell) => {
    const bridge = cell?.cash_flow_bridge;
    const capex = value(bridge?.total_capital_expenditure);
    return [Number.isFinite(capex) ? Math.abs(capex) : null, value(bridge?.operating_cash_flow)];
  });
}

function fixedWindowCagr(rows, slots, valueKey = "perShare") {
  if (rows.length < slots) return null;
  const window = rows.slice(-slots);
  const base = window[0][valueKey];
  const latest = window[window.length - 1][valueKey];
  const span = slots - 1;
  return span > 0 && base > 0 && latest > 0
    ? (Math.pow(latest / base, 1 / span) - 1) * 100
    : null;
}

/** CAGR from a three-year average centred on the nominal starting slot.
 * A five-slot measure uses slots 4/5/6; a ten-slot measure uses 9/10/11. */
function averagedStartCagr(rows, slots, valueKey = "perShare") {
  if (slots < 2 || rows.length < slots + 1) return { value: null, basis: null };
  const window = rows.slice(-(slots + 1));
  const basisRows = window.slice(0, 3);
  const basisValues = basisRows.map((row) => row[valueKey]);
  const latestRow = window[window.length - 1];
  const basis = {
    firstFiscalYear: basisRows[0].fiscalYear,
    lastFiscalYear: basisRows[2].fiscalYear,
    latestFiscalYear: latestRow.fiscalYear,
  };
  if (!basisValues.every((value) => Number.isFinite(value))
      || !Number.isFinite(latestRow[valueKey])) return { value: null, basis };
  const start = basisValues.reduce((sum, value) => sum + value, 0) / 3;
  const latest = latestRow[valueKey];
  return {
    value: start > 0 && latest > 0
      ? (Math.pow(latest / start, 1 / (slots - 1)) - 1) * 100
      : null,
    basis,
  };
}

/** Build one explicit fiscal-year trend without bridging missing years.
 * `perShareKey` names the filing-backed lens; aliases preserve older payloads. */
export function ownerMetricTrend(
  oe,
  perShareKey = "reported_earnings_assumption_per_share",
  totalKey = "reported_earnings_assumption",
  length = 10,
) {
  const annual = oe?.annual_per_share ?? {};
  const latestFiscalYear = Number(oe?.fiscal_year);
  if (!Number.isFinite(latestFiscalYear) || length < 1) return null;

  const firstFiscalYear = latestFiscalYear - length + 1;
  const rowFor = (fiscalYear) => {
    const cell = annual[fiscalYear] ?? annual[String(fiscalYear)] ?? null;
    let value = finite(cell?.[perShareKey]);
    let total = finite(cell?.[totalKey]);
    if (perShareKey === "reported_earnings_assumption_per_share") {
      value ??= finite(cell?.maintenance_estimate_per_share);
      total ??= finite(cell?.maintenance_estimate);
    }
    const shares = finite(cell?.diluted_shares);
    if (total == null && value != null && shares != null) total = value * shares;
    return {
      fiscalYear,
      cell,
      perShare: value,
      total,
      shares,
      yoy: null,
      totalYoy: null,
      sharesYoy: null,
    };
  };
  const ascending = [];
  for (let fiscalYear = firstFiscalYear; fiscalYear <= latestFiscalYear; fiscalYear += 1) {
    ascending.push(rowFor(fiscalYear));
  }
  const cagrRows = [rowFor(firstFiscalYear - 1), ...ascending];

  let comparableSteps = 0;
  let rateSteps = 0;
  let yearsIncreased = 0;
  let yearsAtLeastSix = 0;
  for (let i = 1; i < ascending.length; i += 1) {
    const previous = ascending[i - 1];
    const current = ascending[i];
    if (previous.perShare != null && current.perShare != null) {
      comparableSteps += 1;
      if (current.perShare > previous.perShare) yearsIncreased += 1;
      if (previous.perShare > 0) {
        current.yoy = (current.perShare / previous.perShare - 1) * 100;
        rateSteps += 1;
        if (current.yoy >= 6) yearsAtLeastSix += 1;
      }
    }
    if (previous.total > 0 && current.total != null)
      current.totalYoy = (current.total / previous.total - 1) * 100;
    if (previous.shares > 0 && current.shares != null)
      current.sharesYoy = (current.shares / previous.shares - 1) * 100;
  }

  const present = ascending.filter((row) => row.perShare != null);
  const values = present.map((row) => row.perShare);
  const latest = ascending[ascending.length - 1].perShare;
  const median10 = median(values);
  const median5 = median(ascending.slice(-5).map((row) => row.perShare));
  const cagr = averagedStartCagr(cagrRows, length);
  const cagr5 = averagedStartCagr(cagrRows, Math.min(5, length));
  const totalCagr = averagedStartCagr(cagrRows, length, "total");
  const shareCountCagr = averagedStartCagr(cagrRows, length, "shares");

  return {
    firstFiscalYear,
    latestFiscalYear,
    rows: [...ascending].reverse(),
    yearsPresent: present.length,
    yearsExpected: length,
    yearsMissing: length - present.length,
    yearsProfitable: values.filter((value) => value > 0).length,
    comparableSteps,
    rateSteps,
    yearsIncreased,
    yearsAtLeastSix,
    cagr: cagr.value,
    cagrBasis: cagr.basis,
    cagr5: cagr5.value,
    cagr5Basis: cagr5.basis,
    cagr3: fixedWindowCagr(ascending, Math.min(3, length)),
    totalCagr: totalCagr.value,
    shareCountCagr: shareCountCagr.value,
    median5,
    median10,
    latestVsMedian10: latest != null && median10 > 0
      ? (latest / median10 - 1) * 100
      : null,
    buybackDriven: shareCountCagr.value != null && shareCountCagr.value < 0
      && totalCagr.value != null && totalCagr.value < 0
      && cagr.value != null && cagr.value > totalCagr.value,
  };
}

/** Default owner-evidence trend: reported earnings under maintenance capex = D&A. */
export function ownerEarningsTrend(oe, length = 10) {
  return ownerMetricTrend(
    oe,
    "reported_earnings_assumption_per_share",
    "reported_earnings_assumption",
    length,
  );
}
