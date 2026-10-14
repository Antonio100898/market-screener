import EarningsEvidence from "./EarningsEvidence.jsx";
import { below } from "./format.js";
import { byN, valuationPrice } from "./screen.js";
import { quoteIcon, quoteStatus, quoteTitle, quoteTone } from "./quote.js";

// These columns sort best-first by negating their value, so ascending order puts the
// largest at the top. The arrow must describe what the reader sees, not the sign.
const DESCENDING_BY_DEFAULT = new Set([
  "n_pass", "mcap", "ni", "trend", "offhigh", "eps10",
  "returns",
]);
function Th({ id, sort, onSort, children, className = "" }) {
  const priority = sort.findIndex((item) => item.key === id);
  const active = priority !== -1;
  const direction = active ? sort[priority].dir : 1;
  const up = DESCENDING_BY_DEFAULT.has(id) ? direction !== 1 : direction === 1;
  return (
    <th
      className={`${className} sortable ${active ? "sorted" : ""}`}
      onClick={(event) => onSort(id, event.shiftKey)}
      title="Click for primary sort; Shift-click to add or toggle another column"
    >
      {children}
      <span className="arrow">{active ? (up ? "▲" : "▼") : "↕"}</span>
      {active && sort.length > 1 && <span className="sort-priority">{priority + 1}</span>}
    </th>
  );
}

const DEFAULT_COLUMNS = ['ticker', 'name', 'sector', 'eps10', 'returns', 'mcap', 'price', 'offhigh', 'pe', 'pe3', 'dividend'];

export default function CompanyTable({ rows, sort, onSort, onSelect, tracked = new Set(), portfolioCiks = new Set(), onToggleTracked, columns = DEFAULT_COLUMNS }) {
  const headers = {
    ticker: <Th key='ticker' id="ticker" sort={sort} onSort={onSort}>Ticker</Th>,
    name: <Th key='name' id="name" sort={sort} onSort={onSort}>Company</Th>,
    sector: <Th key='sector' id="sector" sort={sort} onSort={onSort}>Sector</Th>,
    eps10: <Th key='eps10' id="eps10" sort={sort} onSort={onSort}>10Y EPS evidence<em className="sub2">positive years · FCF</em></Th>,
    returns: <Th key='returns' id="returns" sort={sort} onSort={onSort} className="num returns-col">
              Return quality<em className="sub2">score · ROE · ROIC · RONTA · D/E</em>
              <em className="sub2">FCF/revenue · CapEx/OCF · 10Y medians</em>
            </Th>,
    mcap: <Th key='mcap' id="mcap" sort={sort} onSort={onSort} className="num">Mkt cap</Th>,
    price: <Th key='price' id="price" sort={sort} onSort={onSort} className="num">Price</Th>,
    offhigh: <Th key='offhigh' id="offhigh" sort={sort} onSort={onSort} className="num offhigh-col">
              Off high<em className="sub2">52w · 3y</em>
            </Th>,
    pe: <Th key='pe' id="pe" sort={sort} onSort={onSort} className="num">P/E</Th>,
    pe3: <Th key='pe3' id="pe3" sort={sort} onSort={onSort} className="num">
              P/E 3y<em className="sub2">avg EPS</em>
            </Th>,
    dividend: <Th key='dividend' id="dividend" sort={sort} onSort={onSort} className="num">Dividend yield</Th>,
  };
  return (
      <table className="grid">
        <thead>
          <tr>
            {onToggleTracked && <th className="starcol"></th>}
            {columns.map((id) => headers[id])}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const cells = {
                ticker: <td key='ticker' className="tick" data-label="Ticker">
                {r.ticker}
                {portfolioCiks.has(r.cik) && (
                  <span className="portfolio-mark" title="Open position in your portfolio"
                        aria-label="In portfolio">◆</span>
                )}
                {r.netNet && (
                  <span className="netnetmark" title="trading at or below net current asset value">
                    net-net
                  </span>
                )}
                {r.prose_gaps?.length > 0 && (
                  <span className="verifymark"
                        title={`Some data cannot be extracted and must be read in the filing:\n\n`
                               + r.prose_gaps.map((g) => `• ${g.what}\n  affects ${g.affects}`).join("\n\n")}>
                    check filing
                  </span>
                )}
                {r.warnings.length > 0 && (
                  <span className="warningmark"
                        title={r.warnings.map((warning) => `${warning.kind}: ${warning.text}`).join("\n\n")}>
                    data warning
                  </span>
                )}
              </td>,
                name: <td key='name' className="name" data-label="Company">{r.name}</td>,
                sector: <td key='sector' className="sector" data-label="Sector" title={r.industry ?? ""}>
                <span className={`profile-mini ${r.profile.toLowerCase()}`}>{r.graham_profile_meta?.short ?? r.profile}</span>
                <span className="sector-name">{r.sector ?? "Unclassified"}</span>
                {r.exchange === "OTC" && <span className="otc">OTC</span>}
              </td>,
                eps10: <td key='eps10' data-label="10Y EPS evidence"><EarningsEvidence annual={r.annual_eps} fcf={annualFcf(r)} latestFy={r.ch13?.latest_fy} /></td>,
                returns: <td key='returns' className="num return-quality" data-label="Return quality"
                  title={`0–100 percentile score: ROE, ROIC, RONTA, FCF/revenue, CapEx/OCF and debt/equity are compared with companies that have the same input. FCF/revenue has 22.5% weight; CapEx/OCF has 20%; debt/equity has 10%, or 20% when D/E is at least 1, taking the extra 10% from ROE. Lower CapEx/OCF and debt/equity score higher. After percentile weighting, the final score is divided by 1 + min(D/E, 1), then by 1 + median CapEx/OCF. Missing penalties are unassessed. Missing OCF or FCF makes the score 0. ${r.returnQuality.missingInputs.length ? `Missing: ${r.returnQuality.missingInputs.join(", ")}.` : "All six inputs available."} ${r.returnQuality.assumptionMode
                    ? `Zero-assumption Return Quality. ${r.returnQuality.estimateNote ?? ""} ${r.returnQuality.assumptions.length ? `Assumed absent: ${r.returnQuality.assumptions.join(", ")}.` : "No eligible absent input required substitution."}`
                    : r.returnQuality.estimated
                    ? `Uses conservative return estimates. ${r.returnQuality.estimateNote ?? ""} Assumed absent: ${r.returnQuality.assumptions.join(", ")}.`
                    : "A missing or irrelevant operating return is omitted rather than treated as zero."}`}>
                <b>{r.returnQuality.assumptionApplied || r.returnQuality.estimated || r.returnQuality.missingInputs.length
                  ? "≈ " : ""}{fmtScore(r.returnQuality.score)}</b>
                <span className="return-quality-parts">
                  {fmtQualityRate(r.returnQuality.roe, r.returnQuality.estimatedInputs?.roe, r.returnQuality.assumedInputs?.roe)} · {fmtQualityRate(r.returnQuality.roic, r.returnQuality.estimatedInputs.roic, r.returnQuality.assumedInputs?.roic)} · {fmtQualityRate(r.returnQuality.ronta, r.returnQuality.estimatedInputs.ronta, r.returnQuality.assumedInputs?.ronta)} · {fmtQualityMultiple(r.returnQuality.debtToEquity, r.returnQuality.assumedInputs?.debtToEquity)}
                </span>
                <span className="return-quality-parts">
                  FCF/revenue {fmtQualityRate(r.returnQuality.fcfRevenue)} · {r.returnQuality.fcfRevenueYears}/10 years
                </span>
                <span className="return-quality-parts"
                      title="Score divided by 1 + median annual Total CapEx / operating cash flow. CapEx uses its positive magnitude as shown in the detail table, which can include accrual-based investment. Only years with positive OCF count. No usable pairs means this penalty is unassessed.">
                  CapEx/OCF {fmtQualityRate(r.returnQuality.capexOcf)} · {r.returnQuality.capexOcfYears}/10 years
                </span>
                <span className="return-quality-parts return-quality-evidence">
                  {r.returnQuality.cashFlowMissing ? "Missing OCF/FCF · " : r.returnQuality.noReturnEvidence ? "No return evidence · " : ""}{r.returnQuality.inputsPresent}/6 inputs
                  {r.returnQuality.missingInputs.length > 0 && ` · missing ${r.returnQuality.missingInputs.join(", ")}`}
                </span>
                {r.returnQuality.assumptionMode
                  ? <span className="return-quality-estimate">zero-assumption mode</span>
                  : r.returnQuality.estimated && <span className="return-quality-estimate">uses estimated returns</span>}
              </td>,
                mcap: <td key='mcap' className="num" data-label="Mkt cap">{fmtCap(r.mcap, r.quote_currency ?? r.currency)}</td>,
                price: <td key='price' className="num" data-label="Price">
                {fmtPrice(r.price, r.quote_currency ?? r.currency)}
                <span className={`quote-icon ${quoteTone(r)}`} title={quoteTitle(r)}
                      aria-label={quoteStatus(r)}>{quoteIcon(r)}</span>
              </td>,
                offhigh: <td key='offhigh' className="num offhigh" data-label="Off high" title={drawdownTitle(r)}>
                {r.price_stats?.pct_below_52w_high == null ? (
                  <span className="dim">—</span>
                ) : (
                  <>
                    <b>{below(r.price_stats.pct_below_52w_high)}</b>
                    <span className="dim"> · {below(r.price_stats.pct_below_3y_high)}</span>
                  </>
                )}
              </td>,
                pe: <td key='pe' className="num" data-label="P/E">{fmt(byN(r, 1).value)}</td>,
                pe3: <td key='pe3' className="num" data-label="P/E 3y"
                  title="current price over the average of the three latest annual EPS — one lucky or disastrous year moves it a third as much as it moves the TTM P/E">
                {fmt(r.pe3)}
              </td>,
                dividend: <td key='dividend' className="num" data-label="Dividend yield" title={dividendTitle(r)}>
                {fmtPercent(r.dividendYield)}
              </td>,
            };
            return (
            <tr key={r.security_id ?? r.cik} onClick={() => onSelect(r)}
                className={`${tracked.has(r.cik) ? "istracked" : ""}${portfolioCiks.has(r.cik) ? " inportfolio" : ""}`.trim()}>
              {onToggleTracked && <td className="starcol" data-label="">
                <button className={`star-btn ${tracked.has(r.cik) ? "on" : ""}`}
                        title={tracked.has(r.cik) ? "Remove from tracked" : "Track this company"}
                        onClick={(e) => onToggleTracked(r, e)}>
                  {tracked.has(r.cik) ? "★" : "☆"}
                </button>
              </td>}
              {columns.map((id) => cells[id])}
            </tr>
            );
          })}
        </tbody>
      </table>
  );
}

const fmt = (v) => (v == null ? "—" : v.toLocaleString(undefined, { maximumFractionDigits: 2 }));
const annualFcf = (r) => Object.fromEntries(Object.entries(r.owner_earnings?.annual_per_share ?? {})
  .filter(([, v]) => v?.free_cash_flow_per_share != null)
  .map(([year, v]) => [year, v.free_cash_flow_per_share]));
const fmtPercent = (v) => (v == null ? "—" : `${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`);
function dividendTitle(r) {
  const dps = r.recurring_dividend_per_share ?? r.dividend_per_share;
  if (dps == null || r.dividendYield == null) return "No filing-backed dividend per share and current price pair is available";
  const mode = r.recurring_dividend_per_share != null ? "Normal recurring annualized" : "Trailing twelve-month cash";
  const special = r.recurring_dividend_per_share != null && r.dividend_per_share != null
    && Math.abs(r.dividend_per_share - r.recurring_dividend_per_share) > 0.005
    ? `; trailing cash including specials was ${fmtPrice(r.dividend_per_share, r.currency)}` : "";
  return `${mode} dividend ${fmtPrice(dps, r.currency)} ÷ current price ${fmtPrice(valuationPrice(r), r.quote_currency ?? r.currency)}${special}`;
}
const fmtScore = (v) => (v == null ? "—" : Number(v).toLocaleString(undefined, {
  minimumFractionDigits: 1, maximumFractionDigits: 1,
}));
const fmtRate = (v) => (v == null ? "—" : `${Number(v).toLocaleString(undefined, {
  minimumFractionDigits: 1, maximumFractionDigits: 1,
})}%`);
const fmtQualityRate = (v, estimated, assumed) => `${assumed && v != null ? "≈ " : estimated && v != null ? "≥ " : ""}${fmtRate(v)}`;
export const fmtMultiple = (v) => (v == null ? "—" : `${v.toLocaleString(undefined, {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
})}×`);
const fmtQualityMultiple = (v, assumed) => `${assumed && v != null ? "≈ " : ""}${fmtMultiple(v)}`;

// penny stocks rounded to 2dp all read "$0.00", which looks like missing data
const currencySymbol = (currency) => ({ EUR: "€", GBP: "£", JPY: "¥" }[currency] ?? "$");
const fmtPrice = (v, currency = "USD") => {
  if (v == null) return "—";
  const symbol = currencySymbol(currency);
  if (v === 0) return `${symbol}0`;
  if (v < 0.01) return `${symbol}${v.toPrecision(2)}`;
  if (v < 1) return `${symbol}${v.toFixed(3)}`;
  return `${symbol}${v.toFixed(2)}`;
};

// EPS can rise on buybacks alone, so flag when the two move differently
function divergence(r) {
  const e = r.trend?.change, n = r.niTrend?.change;
  if (e == null || n == null) return null;
  if (e > 0.05 && n < 0) return "▲EPS ▼earnings";
  if (e < 0 && n > 0.05) return "▼EPS ▲earnings";
  return null;
}

function niTitle(r) {
  const n = r.niTrend;
  const ttm = r.ttm_net_income != null
    ? `TTM net income ${fmtCap(r.ttm_net_income, r.currency)}` : "";
  if (!n) return ttm || "net income";
  const pct = n.change == null ? "n/m" : `${n.change >= 0 ? "+" : ""}${Math.round(n.change * 100)}%`;
  return `${ttm}\nchange over the window ${pct}\n` +
         n.labels.map((l, i) => `${l} ${(n.values[i] / 1e6).toFixed(0)}M`).join("  ");
}

/** How long the market has kept the company down, and how far off its own range. */
export function drawdownTitle(r) {
  const p = r.price_stats;
  if (!p) return "no price history available";
  return [
    `${p.pct_below_52w_high}% below the 52-week high`,
    `${p.pct_above_52w_low}% above the 52-week low`,
    `${p.pct_below_3y_high}% below the 3-year high`,
    `${p.pct_vs_3y_average >= 0 ? "+" : ""}${p.pct_vs_3y_average}% versus the 3-year average (${fmtPrice(p.average_3y, r.quote_currency ?? r.currency)})`,
    `${p.price_to_3y_median}× the 3-year median price`,
    "\nweekly closing prices — a high here is the best weekly close, not an intraday spike",
  ].join("\n");
}

const fmtCap = (v, currency = "USD") => {
  if (!v) return "—";
  const symbol = currencySymbol(currency);
  for (const [d, s] of [[1e12, "T"], [1e9, "B"], [1e6, "M"]])
    if (v >= d) {
      const scaled = v / d;
      return `${symbol}${scaled.toFixed(s === "B" ? 2 : (scaled < 10 ? 1 : 0))}${s}`;
    }
  return `${symbol}${(v / 1e3).toFixed(0)}K`;
};
