/** Pure Graham-oriented earnings-history calculations shared by the table and tests. */
export function latestEpsLabel(annual, latestFy) {
  const labels = Object.keys(annual ?? {}).map(Number).filter(Number.isFinite);
  if (Number.isInteger(latestFy)) labels.push(latestFy);
  return labels.length ? Math.max(...labels) : null;
}

export function epsEvidence(annual, latestFy) {
  const byYear = annual ?? {};
  const latest = latestEpsLabel(byYear, latestFy);
  if (latest == null) return null;

  // Defensive stability is a ten-completed-year record, FY(L-9) through FY(L).
  const years = Array.from({ length: 10 }, (_, i) => latest - 9 + i);
  const values = years.map((year) => (Object.hasOwn(byYear, year) ? byYear[year] : null));
  const present = values.filter((value) => value != null);
  const positive = present.filter((value) => value > 0).length;
  const complete = present.length === 10;
  const stable = complete && positive === 10;

  // Graham's separate defensive growth test is not a ten-observation window.
  // It compares the three-year average ending ten years ago (FY L-12..L-10)
  // with the latest three-year average (FY L-2..L), requiring 13 fiscal years.
  const growthBaseYears = [latest - 12, latest - 11, latest - 10];
  const growthRecentYears = [latest - 2, latest - 1, latest];
  const growthBase = growthBaseYears.map((year) => (Object.hasOwn(byYear, year) ? byYear[year] : null));
  const growthRecent = growthRecentYears.map((year) => (Object.hasOwn(byYear, year) ? byYear[year] : null));
  const hasGrowthInputs = [...growthBase, ...growthRecent].every((value) => value != null);
  const firstAverage = hasGrowthInputs ? average(growthBase) : null;
  const lastAverage = hasGrowthInputs ? average(growthRecent) : null;
  const growth = firstAverage != null && firstAverage > 0 && lastAverage != null
    ? (lastAverage / firstAverage - 1) * 100 : null;

  return {
    years, values, present: present.length, positive, stable, growth, latest,
    growthBaseYears, growthRecentYears,
  };
}

/** Net income is only an exception signal when it materially disagrees with EPS. */
export function earningsQuality(annualEps, annualNetIncome) {
  const eps = annualEps ?? {};
  const income = annualNetIncome ?? {};
  const years = Object.keys(eps).map(Number).filter((year) => Number.isFinite(year) && income[year] != null)
    .sort((a, b) => a - b).slice(-5);
  if (years.length < 3) return null;
  const first = years[0], last = years.at(-1);
  const epsMove = fractionalMove(eps[first], eps[last]);
  const incomeMove = fractionalMove(income[first], income[last]);
  if (epsMove == null || incomeMove == null) return null;
  if (epsMove >= 0.15 && incomeMove <= -0.15)
    return { label: "EPS ↑ · NI ↓", title: `EPS rose while net income fell from FY${first} to FY${last}; inspect share count and earnings quality.` };
  if (epsMove <= -0.15 && incomeMove >= 0.15)
    return { label: "EPS ↓ · NI ↑", title: `EPS fell while net income rose from FY${first} to FY${last}; inspect dilution and per-share economics.` };
  return null;
}

function average(values) { return values.reduce((sum, value) => sum + value, 0) / values.length; }
function fractionalMove(first, last) {
  const base = Math.abs(first);
  return base >= 0.05 ? (last - first) / base : null;
}
