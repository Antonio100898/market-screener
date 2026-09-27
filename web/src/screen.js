import { cashFlowEvidence, fcfRevenueHistory, capexOcfHistory } from "./ownerEarnings.js";

// Screen semantics shared by the table and the detail panel. The criteria
// themselves are settled once, at export, by sync.apply_price: nothing here
// recomputes a status from a price. priceToPass() asks the opposite question —
// what price would clear the tests — and is the only price arithmetic in the app.

export const CRITERIA = {
  1: "Earnings valuation — P/E < 10",
  2: "Liquidity — current ratio ≥ 1.5",
  3: "Debt ≤ 1.1 × net current assets",
  4: "EPS >= 0 in each of the past 5 years",
  5: "Currently pays a dividend",
  7: "Price ≤ 1.2 × tangible book value",
};

// Number 6 is missing on purpose: Graham's earnings-growth requirement measured
// the latest year against a fixed 1966 base, and no modern base year can be
// attributed to him. The comparison is reported in the detail panel instead.
export const TOTAL_CRITERIA = Object.keys(CRITERIA).length;

export const PLAIN = {
  1: "too expensive vs earnings",
  2: "not enough working capital",
  3: "too much debt",
  4: "a loss year in the last five",
  5: "pays no dividend",
  7: "too expensive vs tangible assets",
};

const PRICE_CRITERIA = new Set([1, 7]);

/** Criterion by its number — the array is no longer numbered 1..n positionally. */
export const byN = (row, n) => row.criteria.find((c) => c.n === n);

/** Balance-sheet and valuation ratios shared by the Research table and detail
 * panel. Missing or non-positive denominators stay unavailable, never zero. */
export function currentRatio(row) {
  return row.current_assets != null && row.current_liabilities > 0
    ? row.current_assets / row.current_liabilities
    : null;
}

export function valuationPrice(row) {
  const reporting = row.reporting_currency ?? row.currency ?? "USD";
  const quote = row.quote_currency ?? row.currency ?? "USD";
  return reporting === quote ? row.price : row.price_reporting_currency;
}

export function priceToBook(row) {
  const price = valuationPrice(row);
  return price != null && row.bvps > 0 ? price / row.bvps : null;
}

/** Equal-weight bounded returns, reduced by known debt and CapEx penalties.
 * Missing inputs are disclosed and omitted, never represented as reported zero.
 * A return whose numerator was assumed absent is unavailable. With no return
 * Missing OCF or FCF makes the score zero; other missing inputs remain disclosed.
 * Shared facts and Graham criteria remain untouched. */
export function returnQuality(row) {
  const assumption = row.return_quality_assumption?.status === "APPLIED"
    ? row.return_quality_assumption : null;
  const estimate = !assumption
    && row.operating_returns_estimate?.status === "CONSERVATIVE_LOWER_BOUND"
    ? row.operating_returns_estimate : null;
  const inputAssumptions = assumption?.input_assumptions ?? {};
  const roe = assumption ? assumption.roe : row.profitability?.on_equity;
  const metric = (name) => {
    if (assumption) {
      const value = inputAssumptions[name]?.includes("operating_income")
        ? null : assumption[name];
      return {
        value: Number.isFinite(value) ? value : null,
        assumed: (inputAssumptions[name]?.length ?? 0) > 0,
        estimated: false,
      };
    }
    const exact = row.operating_returns?.[name];
    if (Number.isFinite(exact)) return { value: exact, assumed: false, estimated: false };
    const lowerBound = estimate?.[name];
    return Number.isFinite(lowerBound)
      ? { value: lowerBound, assumed: false, estimated: true }
      : { value: null, assumed: false, estimated: false };
  };
  const roicMetric = metric("nopat_roic");
  const rontaMetric = metric("ronta");
  const roic = roicMetric.value;
  const ronta = rontaMetric.value;
  const fcfRevenue = fcfRevenueHistory(row);
  const capexOcf = capexOcfHistory(row);
  const cashFlow = cashFlowEvidence(row);
  const cashFlowMissing = !cashFlow.fcf || !cashFlow.ocf;
  const debt = assumption ? assumption.debt_to_equity : row.debt_to_equity;
  const debtToEquity = Number.isFinite(debt) && debt >= 0 ? debt : null;
  const values = [roe, roic, ronta, fcfRevenue.median].filter(Number.isFinite);
  const returnScore = cashFlowMissing ? 0 : values.length
    ? values.reduce((sum, value) => sum + 50 + 50 * (value / (20 + Math.abs(value))), 0) / values.length
    : 0;
  const missingInputs = Object.entries({
    ROE: roe, ROIC: roic, RONTA: ronta, "FCF/revenue": fcfRevenue.median,
    "D/E": debtToEquity, "CapEx/OCF": capexOcf.median,
  }).filter(([, value]) => !Number.isFinite(value)).map(([name]) => name);
  return {
    score: returnScore / (1 + Math.min(debtToEquity ?? 0, 1)) / (1 + (capexOcf.median ?? 0) / 100),
    returnScore,
    missingInputs,
    inputsPresent: 6 - missingInputs.length,
    noReturnEvidence: values.length === 0,
    cashFlowMissing,
    roe: Number.isFinite(roe) ? roe : null,
    roic: Number.isFinite(roic) ? roic : null,
    ronta: Number.isFinite(ronta) ? ronta : null,
    fcfRevenue: fcfRevenue.median,
    fcfRevenueYears: fcfRevenue.yearsPresent,
    capexOcf: capexOcf.median,
    capexOcfYears: capexOcf.yearsPresent,
    debtToEquity: Number.isFinite(debtToEquity) && debtToEquity >= 0 ? debtToEquity : null,
    assumptionMode: assumption != null,
    assumptionApplied: (assumption?.applied?.length ?? 0) > 0,
    assumedInputs: {
      roe: (inputAssumptions.roe?.length ?? 0) > 0,
      roic: roicMetric.assumed,
      ronta: rontaMetric.assumed && Number.isFinite(ronta),
      debtToEquity: (inputAssumptions.debt_to_equity?.length ?? 0) > 0,
    },
    estimated: roicMetric.estimated || rontaMetric.estimated,
    estimatedInputs: {
      roic: roicMetric.estimated,
      ronta: rontaMetric.estimated,
    },
    assumptions: assumption?.applied ?? estimate?.operating_return_assumptions ?? [],
    estimateNote: assumption?.note ?? estimate?.note ?? null,
  };
}

export function returnQualityPercentiles(rows) {
  const qualities = rows.map(returnQuality);
  const metrics = [["roe", false], ["roic", false], ["ronta", false], ["fcfRevenue", false],
    ["capexOcf", true], ["debtToEquity", true]];
  const cohorts = Object.fromEntries(metrics.map(([key]) => [key,
    qualities.map((quality) => quality[key]).filter(Number.isFinite).sort((a, b) => a - b)]));
  const percentile = (key, value, lowerBetter) => {
    const cohort = cohorts[key]; let low = 0; let high = cohort.length;
    while (low < high) { const middle = (low + high) >> 1; if (cohort[middle] <= value) low = middle + 1; else high = middle; }
    const rank = cohort.length < 2 ? 1 : (low - 1) / (cohort.length - 1);
    return 100 * (lowerBetter ? 1 - rank : rank);
  };
  return qualities.map((quality) => {
    if (quality.cashFlowMissing) return { ...quality, score: 0, returnScore: 0 };
    const highDebt = quality.debtToEquity >= 1;
    const weights = { roe: highDebt ? 5.8333333333 : 15.8333333333, roic: 15.8333333333,
      ronta: 15.8333333333, fcfRevenue: 22.5, capexOcf: 20, debtToEquity: highDebt ? 20 : 10 };
    let weighted = 0; let presentWeight = 0;
    for (const [key, lowerBetter] of metrics) if (Number.isFinite(quality[key])) {
      weighted += weights[key] * percentile(key, quality[key], lowerBetter); presentWeight += weights[key];
    }
    const returnScore = presentWeight ? weighted / presentWeight : 0;
    const debtDivisor = Number.isFinite(quality.debtToEquity)
      ? 1 + Math.min(quality.debtToEquity, 1) : 1;
    const capexDivisor = Number.isFinite(quality.capexOcf)
      ? 1 + quality.capexOcf / 100 : 1;
    return {
      ...quality,
      score: returnScore / debtDivisor / capexDivisor,
      returnScore,
      highDebtWeight: highDebt,
    };
  });
}

/** A missing operating margin is materially different from a zero margin.
 * The filing must provide a same-period operating-income/revenue basis; the UI
 * says when it did not instead of leaving an unexplained dash. */
export function reportedRate(value) {
  return value == null ? "Not available" : `${Number(value).toFixed(1)}%`;
}

/** Criterion 5 shows only the filing-backed recurring rate. The engine keeps
 * special-inclusive trailing cash in the note, never in the headline yield. */
export function recurringDividendPresentation(value, note) {
  if (value == null) return { value: "—", note };
  const rate = Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
  return { value: `${rate}% recurring`, note };
}

export function closeness(row) {
  const unmet = row.criteria.filter((c) => c.status !== "PASS");
  if (unmet.length === 0)
    return { label: "PASSES", note: `meets all ${TOTAL_CRITERIA} criteria` };

  // A definitive failure settles the company whatever else is unknown, so it is
  // reported ahead of the unknowns. Checking unknowns first would file a known
  // failure under "we cannot say", which throws away a fact we actually have.
  const business = unmet.filter((c) => c.status === "FAIL" && !PRICE_CRITERIA.has(c.n));
  const priceFails = unmet.filter((c) => c.status === "FAIL" && PRICE_CRITERIA.has(c.n));
  const unknown = unmet.filter((c) => c.status !== "FAIL");
  const alsoUnknown = unknown.length
    ? `; ${unknown.length} further criteri${unknown.length === 1 ? "on cannot" : "a cannot"} be measured`
    : "";

  // Anything actually measured and failed outranks what could not be measured: an
  // unknown never rescues a company that already failed a test it did take.
  // Order of precedence:
  //  1. a definitive failure the price cannot fix — we know why it fails
  //  2. anything we could not compute — no verdict is available at any price
  //  3. valuation alone, with every criterion measured — genuinely near
  // Step 2 sits above step 3 deliberately: calling a company "near passing" while
  // a criterion is unknown claims a closeness the data does not support.
  if (business.length)
    return { label: "BLOCKED", unknown: unknown.length,
             note: "a business result, not the price, is in the way" + alsoUnknown };
  if (unknown.length)
    return {
      label: "UNGRADEABLE",
      unknown: unknown.length,
      note: `${unknown.length} criteri${unknown.length === 1 ? "on" : "a"} cannot be computed from its filings` +
        (priceFails.length
          ? `; it also fails ${priceFails.length} valuation test${priceFails.length === 1 ? "" : "s"}, but no price makes the unknown ones pass`
          : " — nothing definitive is known"),
    };
  return {
    label: priceFails.length === 1 ? "NEAR-PASS" : "CLOSE",
    unknown: 0,
    note: `${priceFails.length} valuation test${priceFails.length === 1 ? "" : "s"} unmet, everything else measured and passing — a lower price would complete the screen`,
  };
}

export function whyBlocked(row) {
  const unmet = row.criteria.filter((c) => c.status !== "PASS");
  if (!unmet.length) return "Nothing";
  const price = unmet.filter((c) => c.status === "FAIL" && PRICE_CRITERIA.has(c.n));
  const business = unmet.filter((c) => c.status === "FAIL" && !PRICE_CRITERIA.has(c.n));
  const unknown = unmet.filter((c) => c.status !== "FAIL");
  const parts = [];
  if (business.length) parts.push(business.map((c) => PLAIN[c.n]).join(" and "));
  if (price.length) parts.push(price.map((c) => PLAIN[c.n]).join(" and "));
  if (unknown.length) {
    const ns = new Set(unknown.map((c) => c.n));
    parts.push(
      ns.has(2) && ns.has(3)
        ? "liquidity and debt cannot be measured from its filings"
        : `${unknown.map((c) => PLAIN[c.n] ?? `criterion ${c.n}`).join(" and ")} cannot be measured`,
    );
  }
  const s = parts.join("; ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Price at which every price-dependent criterion would pass; null if price is not the issue. */
export function priceToPass(row) {
  if (!row.price) return null;
  const limits = [];
  const c1 = byN(row, 1);
  const c7 = byN(row, 7);
  if (c1.status === "FAIL" && row.ttm_eps > 0) limits.push(9.99 * row.ttm_eps);
  // criterion 7 is strict too — price < 1.20 x tbvps — so 1.20 x itself fails,
  // the same reason the line above uses 9.99 rather than 10
  if (c7.status === "FAIL" && row.tbvps > 0) limits.push(1.1999 * row.tbvps);
  if (!limits.length) return null;
  // only meaningful when nothing else blocks it
  const other = row.criteria.filter((c) => c.status !== "PASS" && !PRICE_CRITERIA.has(c.n));
  if (other.length) return null;
  const reportingLimit = Math.min(...limits);
  const reporting = row.reporting_currency ?? row.currency ?? "USD";
  const quote = row.quote_currency ?? row.currency ?? "USD";
  if (reporting === quote) return reportingLimit;
  const rate = row.fx?.base === quote && row.fx?.counter === reporting ? row.fx.rate : null;
  return rate > 0 ? reportingLimit / rate : null;
}

/** Graham smoothed a lucky or disastrous single year by pricing the average of
 * several: current price over the mean of the three latest annual EPS. TTM is
 * deliberately not mixed in — the annual series is the only dated one we have.
 * Null when years are missing, the average is not positive, or the newest year
 * is too old to price (dormant filers keep tickers and stale earnings). */
export function pe3(row) {
  const eps = row.annual_eps;
  const price = valuationPrice(row);
  if (!eps || !price) return null;
  const years = Object.keys(eps).map(Number).sort((a, b) => b - a).slice(0, 3);
  if (years.length < 3) return null;
  const priceYear = Number((row.price_asof ?? "").slice(0, 4));
  if (priceYear && years[0] < priceYear - 2) return null;
  const avg = (eps[years[0]] + eps[years[1]] + eps[years[2]]) / 3;
  return avg > 0 ? price / avg : null;
}

/** Median of usable valuation multiples. A loss-making company has no meaningful
 * positive P/E, and missing evidence stays missing rather than becoming zero. */
export function medianPositive(values) {
  const usable = values
    .filter((value) => Number.isFinite(value) && value > 0)
    .sort((a, b) => a - b);
  if (!usable.length) return null;
  const middle = Math.floor(usable.length / 2);
  return usable.length % 2
    ? usable[middle]
    : (usable[middle - 1] + usable[middle]) / 2;
}

/** Valuation reference for one index over the complete, unfiltered UI universe.
 * Current P/E is read from settled criterion 1; it is never recomputed here. */
export function indexValuation(rows, indexName) {
  const members = rows.filter((row) => row.idx?.includes(indexName));
  const peValues = members
    .map((row) => byN(row, 1)?.value)
    .filter((value) => Number.isFinite(value) && value > 0);
  const pe3Values = members
    .map((row) => row.pe3)
    .filter((value) => Number.isFinite(value) && value > 0);
  return {
    members: members.length,
    pe: { median: medianPositive(peValues), count: peValues.length },
    pe3: { median: medianPositive(pe3Values), count: pe3Values.length },
  };
}
