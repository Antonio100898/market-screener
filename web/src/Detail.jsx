import { useEffect, useState } from "react";
import { fetchJson } from "./api.js";
import { awardOverhang, totalCapitalisation, workingCapitalToDebt } from "./capital.js";
import { byN, currentRatio as currentRatioOf, pe3, priceToBook as priceToBookOf,
  recurringDividendPresentation, reportedRate, valuationPrice, valuationFx } from "./screen.js";
import { profileMeta } from "./Alignment.jsx";
import FigureHistory from "./FigureHistory.jsx";
import PriorAccountingHistory from "./PriorAccountingHistory.jsx";
import EpsCurve from "./EpsCurve.jsx";
import { ownerEarningsTrend, ownerMetricTrend } from "./ownerEarnings.js";
import { payloadWarnings } from "./warnings.js";
import { quoteStatus, quoteTitle } from "./quote.js";
import { companyIntrinsicValuePrefill, openIntrinsicValueCalculator } from "./intrinsicValue.js";
import { filingUrl as edgarUrl } from "./sourceLink.js";

const ENTERPRISING = {
  1: { label: "Earnings valuation", rule: "P/E < 10.0" },
  2: { label: "Liquidity", rule: "Current ratio ≥ 1.50" },
  3: { label: "Debt", rule: "Total debt ≤ 1.10 × NCA" },
  4: { label: "Earnings stability", rule: "No EPS deficit in 5 FY" },
  5: { label: "Dividend", rule: "Currently pays a dividend" },
  7: { label: "Tangible-asset valuation", rule: "Price < 1.20 × TBVPS" },
};

/** A compact investment snapshot: current market and financial facts first,
 * then the two Graham frameworks without trend charts or audit-trail clutter. */
export default function Detail({ row: baseRow, onClose, tracked = false, onToggleTracked, onRecordTrade, loadAssumedRow, historyContext }) {
  const [assumedRow, setAssumedRow] = useState(null);
  const [assumptionEnabled, setAssumptionEnabled] = useState(true);
  const [assumptionLoading, setAssumptionLoading] = useState(true);
  const [assumptionError, setAssumptionError] = useState(null);
  const [assumptionAttempt, setAssumptionAttempt] = useState(0);

  useEffect(() => {
    const esc = (event) => event.key === "Escape" && !event.defaultPrevented && onClose();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);

  useEffect(() => {
    if (!assumptionEnabled) {
      setAssumedRow(null);
      setAssumptionError(null);
      setAssumptionLoading(false);
      return undefined;
    }
    let cancelled = false;
    const controller = new AbortController();
    setAssumedRow(null);
    setAssumptionError(null);
    setAssumptionLoading(true);
    const pending = loadAssumedRow ? loadAssumedRow(baseRow, controller.signal) : fetchJson(
      `/company/${encodeURIComponent(baseRow.ticker)}/dashboard?assume_absent_zero=true`,
      { timeoutMs: 30_000 },
    );
    pending.then((next) => {
      if (!cancelled) setAssumedRow(next);
    }).catch((error) => {
      if (!cancelled) setAssumptionError(error.message);
    }).finally(() => {
      if (!cancelled) setAssumptionLoading(false);
    });
    return () => { cancelled = true; controller.abort(); };
  }, [baseRow.cik, baseRow.ticker, assumptionAttempt, assumptionEnabled, loadAssumedRow]);

  const row = assumedRow ?? baseRow;
  const currency = row.reporting_currency ?? row.currency ?? "USD";
  const quoteCurrency = row.quote_currency ?? row.currency ?? "USD";
  const financialPrice = valuationPrice(row);
  const currencyBridge = valuationFx(row);
  const assumptionActive = (assumedRow != null
    && assumedRow.assumption_mode?.status !== "UNAVAILABLE");

  const profile = profileMeta(row);
  const ca = row.current_assets;
  const cl = row.current_liabilities;
  const ltd = row.long_term_debt;
  // criterion 3's "net current assets" and working capital are one quantity; the
  // panel used to show it twice, one row apart, under two names
  const workingCapital = ca != null && cl != null ? ca - cl : null;
  const currentRatio = currentRatioOf(row);
  const marketCap = row.price != null && row.shares != null ? row.price * row.shares : null;
  const priceToBook = priceToBookOf(row);
  const pe3Value = pe3(row);
  const defensive = row.alignment?.defensive;
  const enterprising = row.alignment?.enterprising;
  const ch13 = row.ch13 ?? {};
  // Profit against sales and the shareholders' book/tangible capital. These are
  // descriptive ratios, not enterprising-screen criteria.
  const prof = row.profitability ?? {};
  // Graham's chapter-18 comparisons carry two figures this panel did not: what the
  // whole enterprise costs (the common's market value plus the debt ahead of it),
  // and how far the working capital covers that debt — "no debt" being an answer
  // in its own right, as it is for National Presto.
  const capitalisation = totalCapitalisation(row);
  const awards = awardOverhang(row);
  const wcToDebt = workingCapitalToDebt(row);
  const dividend = row.dividend_record;
  const intrinsicPrefill = companyIntrinsicValuePrefill(row);

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <aside className="detail clean-detail">
        <button className="close-btn" onClick={onClose} aria-label="Close company details">×</button>
        <header className="detail-head">
          <div>
            <h2>{row.ticker}</h2>
            <p className="sub">{row.name}</p>
          </div>
          <div className="detail-head-actions">
            <button type="button" className="intrinsic-detail-button"
                    disabled={!intrinsicPrefill.ready}
                    onClick={() => openIntrinsicValueCalculator(intrinsicPrefill)}
                    title={intrinsicPrefill.ready
                      ? `Open IV with FY${intrinsicPrefill.fiscalYear} FCF and current shares`
                      : intrinsicPrefill.currentIncome == null
                        ? "No annual free cash flow is available in the displayed cash bridge"
                        : "Current shares outstanding are unavailable"}>
              Calculate IV
            </button>
            {onRecordTrade && (
              <button className="trade-button" onClick={() => onRecordTrade(row)}>Record trade</button>
            )}
            {onToggleTracked && (
              <button className={`track-btn ${tracked ? "on" : ""}`} onClick={onToggleTracked}
                      aria-pressed={tracked}
                      title={tracked ? "Remove from tracked" : "Track this company"}>
                {tracked ? "★ Tracked" : "☆ Track"}
              </button>
            )}
            <span className={`profile-pill ${String(row.graham_profile ?? "REVIEW").toLowerCase()}`}>{profile.short}</span>
          </div>
        </header>

        <DataWarnings row={row} />
        {tracked && <LatestQuarterlyFiling filing={row.latest_quarterly_filing} />}
        <AnalysisRoutes routes={row.analysis_routes} />
        <AssumptionControl row={row} enabled={assumptionEnabled} active={assumptionActive}
                           loading={assumptionLoading} error={assumptionError}
                           onEnable={() => setAssumptionEnabled(true)}
                           onDisable={() => setAssumptionEnabled(false)}
                           onRetry={() => setAssumptionAttempt((attempt) => attempt + 1)} />

        <section className="snapshot-section" aria-label="Market and financial snapshot">
          <h3>Market &amp; financial snapshot</h3>
          <div className="snapshot-grid">
            <Metric label="Price" sub={quoteStatus(row)} value={moneyPrice(row.price, quoteCurrency)}
                    title={quoteTitle(row)} />
            <Metric label="TTM EPS" sub={row.ttm_basis ?? "trailing twelve months"}
                    value={moneyPrice(row.ttm_eps, currency)} />
            <Metric label="52-week high" sub="weekly close" value={moneyPrice(row.price_stats?.high_52w, quoteCurrency)} />
            <Metric label="Current vs 3Y avg" sub={row.price_stats?.average_3y == null ? undefined : `3Y avg ${moneyPrice(row.price_stats.average_3y, quoteCurrency)}`} value={signedPercent(row.price_stats?.pct_vs_3y_average)} />
            <Metric label="Market cap" value={money(marketCap, quoteCurrency)} />
            {quoteCurrency !== currency && <Metric label="Price on statement basis"
                    sub={`${quoteCurrency} converted to ${currency}`}
                    value={moneyPrice(financialPrice, currency)} />}
            <Metric label="Working capital" sub="= net current assets, criterion 3's base"
                    value={money(workingCapital, currency)} />
            {row.equity_evidence && <>
              <Metric label="Common equity (derived)" sub={row.equity_evidence.current?.source?.end}
                      value={money(row.equity_evidence.current?.value, currency)} />
              <Metric label="Parent equity (derived)" sub={row.equity_evidence.parent?.source?.end}
                      value={money(row.equity_evidence.parent?.value, currency)} />
            </>}
            <Metric label="Total capitalisation" sub="market value of the common plus its debt"
                    value={money(capitalisation, currency)} />
            <Metric label="Equity awards" sub={row.awards_basis ? `${row.awards_basis}, % of shares` : "options and restricted stock, % of shares"}
                    value={rateOrDash(awards)} />
            <Metric label="Long-term debt" value={money(ltd, currency)} />
            <Metric label="Short-term debt" sub="due within a year" value={money(row.short_term_debt, currency)} />
            <Metric label="Operating-lease liabilities" sub="outside Graham's debt test"
                    value={money(row.operating_lease_liability, currency)} />
            <Metric label="Lease-adjusted debt" sub="reported debt plus operating leases"
                    value={money(row.lease_adjusted_debt, currency)} />
            <Metric label="Fixed-charge coverage" sub="reported operating-income proxy"
                    value={multiple(row.fixed_charge_coverage)} />
            <Metric label="NCAV / share" value={moneyPrice(row.ncavps, currency)} emphasis={row.ncavps != null && financialPrice != null && financialPrice <= row.ncavps} />
          </div>
          {currencyBridge && <p className="criteria-note"><b>Currency bridge:</b>{" "}
            1 {currencyBridge.base} = {number(currencyBridge.rate)} {currencyBridge.counter} as of {String(currencyBridge.asof).slice(0, 10)}
            {currencyBridge.source ? ` (${currencyBridge.source})` : ""}. The traded quote and market cap remain in {quoteCurrency};
            filing totals stay in {currency}; only cross-currency valuation uses this rate.</p>}
          {row.equity_evidence && <p className="criteria-note"><b>Book-value basis:</b>{" "}
            {row.equity_evidence.current?.value != null
              ? "Parent equity includes other comprehensive income and belongs to ordinary shareholders. The reported component lines are shown in Data provenance."
              : `Common equity is unavailable. ${row.equity_evidence.unavailable ?? "The retained filing does not establish the common-shareholder basis."} Parent equity is shown separately when available.`}
          </p>}
        </section>

        <Ratios row={row} currentRatio={currentRatio} priceToBook={priceToBook}
                pe3Value={pe3Value} prof={prof} wcToDebt={wcToDebt} awards={awards} />

        <EpsCurve annualEps={row.annual_eps} ttmEps={row.ttm_eps} currency={currency} latestFy={row.ch13?.latest_fy} />

        <CriteriaSection
          title="Enterprising criteria"
          verdict={enterprising?.verdict}
          rows={row.criteria.map(enterprisingRow)}
          extra={enterprising?.growth_modern_4fy && <ModernGrowth growth={enterprising.growth_modern_4fy} />}
        />

        <CriteriaSection
          title="Defensive criteria"
          verdict={defensive?.verdict}
          rows={defensiveRows({ row, defensive, ch13, dividend, currentRatio, workingCapital, ltd, marketCap, pe3Value, priceToBook })}
        />

        <OwnerEarnings oe={row.owner_earnings} cik={row.cik}
                       annualEps={row.annual_eps}
                       annualWeightedShares={row.annual_weighted_shares}
                       currency={currency} />
        <PriorAccountingHistory history={row.prior_accounting_history}
                                formatMoney={money} formatEps={moneyPrice}
                                renderSource={(source) => <SourceCell cik={row.cik} source={source} />} />
        <Notes title="What the multiples do not say"
               subtitle="Cash conversion, dilution, interest cover, leases, receivables and inventory against sales, untaxed profits, tax charged but not paid, valuation allowances, an eroding margin, a foreign listing whose ratio is only on the cover page, a book value made of acquisitions, the shape of the ten-year record, peer efficiency, warrants, debt discount, and the events the company's own filing index proves — context, never part of a grade"
               notes={row.context_notes} />
        <Notes title="What the trailing earnings are made of"
               subtitle="Non-recurring lines large enough to decide criterion 1 on their own"
               notes={row.earnings_quality} />
        <ProseGaps row={row} />
        <SeriesMix mix={row.series_mix} />
        <FigureHistory key={`${historyContext?.security_id}:${historyContext?.release_id}:${assumedRow?.assumption_mode?.status ?? "STRICT"}`} context={historyContext} servedView={assumedRow == null || assumedRow.assumption_mode?.status === "UNAVAILABLE" ? "STRICT" : assumedRow.assumption_mode?.status === "APPLIED" ? "ASSUMED" : "UNKNOWN"} />
        <Provenance row={row} />
      </aside>
    </>
  );
}

function AssumptionControl({ row, enabled, active, loading, error, onEnable, onDisable, onRetry }) {
  const applied = row.assumption_mode?.applied ?? row.assumptions ?? [];
  const details = row.assumption_mode?.details ?? [];
  const labels = applied.map((value) => (
    value === "debt" ? "long- and short-term debt"
      : value === "short_term_debt" ? "short-term debt"
      : value === "short_term_investments" ? "short-term investments"
      : value === "noncurrent_investments" ? "noncurrent investments"
      : value === "intangibles" ? "other intangible assets"
      : value
  ));
  const unavailable = row.assumption_mode?.status === "UNAVAILABLE";
  return (
    <section className={`assumption-control ${active ? "active" : ""}`}
             aria-label="Filing-silence assumptions">
      <div>
        <b>Filing-silence assumptions</b>
        <p>{loading
          ? "Checking the current annual report and later structured filings before applying any zero."
          : unavailable
          ? "Assumption-enhanced calculations were unavailable; strict filing values remain in use."
          : active
          ? (labels.length
            ? `Zero assumptions applied: ${labels.join(", ")}. Open the exact list below for every affected year and ratio.`
            : "No value qualified: the filing history contains contrary evidence or the required classified balance sheet is unavailable.")
          : error
          ? "Could not reach the local assumption service; strict filing values remain in use."
          : "Strict filing values are in use. Missing evidence stays blank unless you explicitly apply zero assumptions."}</p>
        {row.assumption_mode?.note && <small>{row.assumption_mode.note}</small>}
        {error && <small className="err">{error}</small>}
        {active && details.length > 0 && (
          <details className="assumption-details">
            <summary>Exactly what was not reported ({details.length})</summary>
            <ul>
              {details.map((detail, index) => (
                <li key={`${detail.scope}-${detail.fiscal_year ?? "current"}-${index}`}>
                  {detail.fiscal_year != null && <b>FY{detail.fiscal_year}: </b>}
                  {detail.message}
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
      <div className="assumption-actions">
        {error && <button onClick={onRetry}>Retry assumption check</button>}
        {enabled
          ? <button onClick={onDisable}>Use strict filing values</button>
          : <button onClick={onEnable}>Apply zero assumptions</button>}
      </div>
    </section>
  );
}

function DataWarnings({ row }) {
  const warnings = payloadWarnings(row);
  if (!warnings.length) return null;
  return (
    <section className="data-warnings" aria-label="Data freshness warnings">
      {warnings.map((warning) => {
        const filingUrl = warning.accession && edgarUrl(row.cik, warning.accession);
        return (
          <p key={`${warning.kind}-${warning.accession ?? "history"}`}>
            <b>{warning.kind}</b>
            <span>{warning.text}</span>
            {filingUrl && (
              <a href={filingUrl} target="_blank" rel="noreferrer">
                Open filing{warning.filed ? ` filed ${warning.filed}` : ""}
              </a>
            )}
          </p>
        );
      })}
    </section>
  );
}

function LatestQuarterlyFiling({ filing }) {
  return (
    <section className="criteria-section latest-filing-section" aria-label="Latest quarterly filing">
      <div className="criteria-title">
        <div>
          <h3>Latest quarterly filing</h3>
          <p>The newest interim report currently listed by the SEC. Read the company’s official update.</p>
        </div>
      </div>
      {filing ? (
        <div className="latest-filing-card">
          <div>
            <b>{filing.form}</b>
            <span> filed {filing.filed}</span>
            {filing.period && <small>Reporting period ended {filing.period}</small>}
          </div>
          {filing.url && (
            <a href={filing.url} target="_blank" rel="noreferrer">
              Read the SEC filing{filing.document ? ` (${filing.document})` : ""}
            </a>
          )}
        </div>
      ) : (
        <p className="criteria-note">No recent quarterly or interim filing was found in the SEC submissions index.</p>
      )}
    </section>
  );
}

function AnalysisRoutes({ routes }) {
  if (!routes?.length) return null;
  return (
    <section className="criteria-section analysis-routes" aria-label="Business-model comparability">
      <div className="criteria-title"><div><h3>Business-model routing</h3>
        <p>These are comparability warnings, not grades. The screener does not invent
           sector measures that the filing does not report.</p></div></div>
      <div className="analysis-route-grid">
        {routes.map((route) => (
          <article key={route.id}>
            <b>{route.label}</b>
            <p><strong>Prefer:</strong> {route.preferred}.</p>
            <p><strong>De-emphasize:</strong> {route.deemphasize}.</p>
          </article>
        ))}
      </div>
    </section>
  );
}

/** Every ratio the panel shows, at today's price and at each of the last ten
 * fiscal year ends. Chapter 13 compares companies by laying the same handful of
 * ratios side by side, and a single current column says how a business looks today
 * while saying nothing about how it got there. Each past column is struck on its own
 * year: that year's report, and the price as it stood then, over trailing earnings
 * computed from what had been filed by then — no column mixes eras. */
function Ratios({ row, currentRatio, priceToBook, pe3Value, prof, wcToDebt, awards }) {
  const currency = row.currency ?? "USD";
  const history = row.annual_ratios ?? {};
  const assumptionActive = row.assumption_mode?.status === "APPLIED";
  const ownerReturns = row.operating_returns ?? row.owner_earnings ?? {};
  const estimateReturns = row.operating_returns_estimate?.status === "CONSERVATIVE_LOWER_BOUND"
    ? row.operating_returns_estimate : {};
  const estimateFor = (key) => ownerReturns[key] == null
    && Number.isFinite(estimateReturns[key]) ? estimateReturns[key] : null;
  const returnCaveats = ownerReturns.operating_return_caveats
    ?? ownerReturns.caveats ?? [];
  const nopatMissing = returnCaveats.find((text) => /NOPAT.*withheld|NOPAT and .*withheld/i.test(text))
    ?? "NOPAT needs same-period operating income and at least one aligned filing-reported effective tax rate from the fiscal year or its prior two years.";
  const capitalInputMissing = returnCaveats.find((text) =>
    /non-interest-bearing current liabilities and invested capital|cash-excluded invested capital|beginning-and-ending cash-excluded capital/i.test(text));
  const roicMissing = ownerReturns.nopat == null ? nopatMissing
    : capitalInputMissing
      ?? "NOPAT ROIC needs exact beginning and ending cash-excluded invested capital.";
  const cashReturnMissing = ownerReturns.nopat == null ? nopatMissing
    : returnCaveats.find((text) => /non-interest-bearing current liabilities and invested capital/i.test(text))
      ?? "This return needs exact beginning and ending capital including cash.";
  const rontaMissing = ownerReturns.nopat == null ? nopatMissing
    : returnCaveats.find((text) => /RONTA/i.test(text)) ?? capitalInputMissing
      ?? "RONTA needs exact beginning and ending net tangible operating assets.";
  const leaseNeutralRontaMissing = ownerReturns.nopat == null ? nopatMissing
    : returnCaveats.find((text) => /lease-neutral RONTA.*withheld/i.test(text))
      ?? "Lease-neutral RONTA needs exact beginning and ending ROU assets and current lease liabilities after lease recognition.";
  // Ten consecutive fiscal-year slots, newest first. Young or sparse filers keep
  // visible gaps instead of silently reaching farther back or shrinking the table.
  const observedYears = Object.keys(history).map(Number).filter(Number.isFinite);
  const fallbackYear = row.balance_sheet_date
    ? Number(row.balance_sheet_date.slice(0, 4)) : null;
  const latestYear = observedYears.length ? Math.max(...observedYears) : fallbackYear;
  const years = latestYear == null
    ? [] : Array.from({ length: 10 }, (_, index) => latestYear - index);
  const hasLeaseNeutral = ownerReturns.lease_neutral_ronta != null
    || estimateFor("lease_neutral_ronta") != null
    || ownerReturns.lease_neutral_ronta_undefined
    || years.some((year) => history[year]?.lease_neutral_ronta != null
      || history[year]?.lease_neutral_ronta_undefined);
  const lines = [
    { label: "P/E", sub: row.ttm_basis ?? "trailing", now: byN(row, 1).value, key: "pe", fmt: multiple },
    { label: "P/E", sub: "3-year average EPS", now: pe3Value, key: "pe3", fmt: multiple },
    { label: "P/B", now: priceToBook, key: "pb", fmt: multiple },
    { label: "P/TBV", sub: "criterion 7 \u00b7 under 1.20\u00d7", now: byN(row, 7).value, key: "ptbv", fmt: multiple },
    { label: "Current ratio", sub: "criterion 2 \u00b7 at least 1.50\u00d7", now: currentRatio, key: "current_ratio", fmt: multiple },
    { label: "Debt / equity", sub: "combined interest-bearing debt / common equity · generally under 1.00×",
      now: row.debt_to_equity, key: "debt_to_equity", fmt: multiple },
    { label: "Working capital / debt", sub: "Graham's chapter-18 comparison", now: wcToDebt,
      key: "working_capital_to_debt",
      fmt: (v) => (typeof v === "string" ? v : multiple(v)) },
    { label: "Equity awards", sub: `${row.awards_basis ?? "options and restricted stock"} \u00b7 % of shares`,
      now: awards, key: "award_pct", fmt: rateOrDash, assumeZero: true },
    { label: "Revenue", sub: "same fiscal-year top line", now: prof.revenue,
      key: "revenue", fmt: (value) => money(value, currency), assumeZero: true },
    { label: "Gross margin", sub: "gross profit / revenue", now: prof.gross,
      key: "gross_margin", fmt: rateOrDash,
      missing: "No same-period, filing-reported gross profit and revenue pair is available." },
    { label: "Net margin", sub: "profit per $ of sales", now: prof.net, key: "net_margin", fmt: rateOrDash },
    { label: "Operating margin", sub: "reported operating income / sales", now: prof.operating,
      key: "operating_margin", fmt: reportedRate,
      missing: "No same-period, filing-reported operating income and revenue pair is available; the screener does not invent an operating-profit subtotal from differently scoped lines." },
    { label: "Return on equity (ROE)", sub: "Finkle · net income / average common equity", now: prof.on_equity,
      key: "return_on_equity", fmt: rateOrDash },
    { label: "NOPAT ROIC",
      sub: `${ownerReturns.nopat == null ? "normalized NOPAT" : `${money(ownerReturns.nopat, currency)} normalized NOPAT`} / average invested capital excluding cash`,
      now: ownerReturns.nopat_roic, key: "nopat_roic", fmt: rateOrDash, missing: roicMissing,
      estimated: estimateFor("nopat_roic"),
      undefined: ownerReturns.nopat_roic_undefined,
      operatingReturn: true },
    { label: "NOPAT return including cash", sub: "normalized NOPAT / average capital including cash",
      now: ownerReturns.nopat_return_including_cash, key: "nopat_return_including_cash", fmt: rateOrDash,
      missing: cashReturnMissing, undefined: ownerReturns.nopat_return_including_cash_undefined,
      operatingReturn: true },
    { label: "RONTA", sub: "NOPAT / average net tangible operating assets",
      now: ownerReturns.ronta, key: "ronta", fmt: rateOrDash, missing: rontaMissing,
      estimated: estimateFor("ronta"),
      undefined: ownerReturns.ronta_undefined,
      operatingReturn: true },
    ...(hasLeaseNeutral ? [{
      label: "Lease-neutral RONTA",
      sub: "historical comparison · removes recognized operating-lease ROU assets",
      now: ownerReturns.lease_neutral_ronta, key: "lease_neutral_ronta", fmt: rateOrDash,
      estimated: estimateFor("lease_neutral_ronta"),
      missing: leaseNeutralRontaMissing,
      undefined: ownerReturns.lease_neutral_ronta_undefined,
      operatingReturn: true,
    }] : []),
  ];
  if (!years.length && lines.every((line) => line.now == null)) return null;
  return (
    <section className="annual-history ratios-section">
      <div className="criteria-title">
        <div>
          <h3>Ratios</h3>
        </div>
      </div>
      <div className="annual-history-scroll">
        <table className="annual-history-table ratios">
          <thead>
            <tr>
              <th>Ratio</th>
              <th className="num current">Current</th>
              {years.map((y) => (
                <th key={y} className="num" title={history[y]?.end ? `fiscal year ended ${history[y].end}` : undefined}>
                  FY{y}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {lines.map((line) => (
              <tr key={line.label + (line.sub ?? "")}>
                <td><b>{line.label}</b>{line.sub && <small>{line.sub}</small>}</td>
                <td className="num current"
                    title={line.undefined
                      ?? (line.estimated != null
                        ? [...(estimateReturns.operating_return_caveats ?? []), estimateReturns.note]
                          .filter(Boolean).join(" ")
                        : line.now == null ? line.missing : undefined)}>
                  {line.undefined
                    ? "N/M"
                    : line.estimated != null
                      ? <span className="estimated-return">≥ {line.fmt(line.estimated)}</span>
                    : assumptionActive && line.now == null
                      ? (line.assumeZero ? line.fmt(0) : "N/M")
                      : line.fmt(line.now)}
                </td>
                {years.map((y) => (
                  <td key={y} className="num"
                    title={line.key && history[y]?.[`${line.key}_undefined`]
                      ? history[y][`${line.key}_undefined`]
                      : line.key && history[y]?.[line.key] == null
                        ? (line.operatingReturn
                          ? (history[y]?.operating_return_caveats?.join(" ") ?? line.missing)
                          : line.missing)
                        : undefined}>
                    {line.key
                      ? (history[y]?.[`${line.key}_undefined`] ? "N/M" : line.fmt(history[y]?.[line.key]))
                      : <span className="dim">{"—"}</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {Object.keys(estimateReturns).length > 0 && <p className="criteria-note estimate-note">
        <b>Conservative discovery estimate:</b> values marked ≥ are lower bounds used
        only for ranking. Missing optional deductions were bounded at zero
        ({(estimateReturns.operating_return_assumptions ?? []).join(", ")}); exact
        reported ratios and Graham verdicts are unchanged.
      </p>}
      {hasLeaseNeutral && <p className="criteria-note"><b>Lease treatment:</b> RONTA
        keeps both current and noncurrent operating-lease obligations with financing.
        Lease-neutral RONTA also removes the reported ROU asset, matching the old
        off-balance-sheet presentation for historical comparison. It does not estimate
        or capitalize leases from pre-ASC 842 commitments.</p>}
    </section>
  );
}


function Metric({ label, sub, value, emphasis = false, title }) {
  return <div className="metric" title={title}><span>{label}{sub && <em>{sub}</em>}</span><b className={emphasis ? "ok" : ""}>{value}</b></div>;
}

function CriteriaSection({ title, verdict, rows, extra }) {
  return (
    <section className="criteria-section">
      <div className="criteria-title"><div><h3>{title}</h3></div><Status value={verdict} /></div>
      <table className="criteria-clean">
        <thead><tr><th>Test</th><th>Rule</th><th className="num">Current</th><th>Status</th></tr></thead>
        <tbody>{rows.map((item) => <CriteriaRow key={item.label} {...item} />)}</tbody>
      </table>
      {extra}
    </section>
  );
}

function CriteriaRow({ label, rule, value, status, note }) {
  return <tr className={String(status ?? "INSUFFICIENT_DATA").toLowerCase()} title={note ?? undefined}>
    <td><b>{label}</b>{note && <small>{note}</small>}</td>
    <td className="rule">{rule}</td>
    <td className="num">{value}</td>
    <td><Status value={status} /></td>
  </tr>;
}

function ModernGrowth({ growth }) {
  const value = growth.status === "INSUFFICIENT_DATA"
    ? "Insufficient annual EPS history"
    : `FY${growth.latest_fy} ${number(growth.latest_eps)} vs FY${growth.base_fy} ${number(growth.base_eps)}`;
  return <p className="criteria-note"><b>Modern four-fiscal-year EPS analogue:</b> {value} <Status value={growth.status} /></p>;
}

function defensiveRows({ row, defensive, ch13, dividend, currentRatio, workingCapital, ltd, marketCap, pe3Value, priceToBook }) {
  const currency = row.currency ?? "USD";
  const test = (name) => defensive?.tests?.[name] ?? "INSUFFICIENT_DATA";
  const dividendEvidence = dividend?.latest != null && dividend?.streak_from != null
    ? `${dividend.latest - dividend.streak_from + 1} years (from ${dividend.streak_from})`
    : "No verified record";
  const sizeValue = row.graham_profile === "UTILITY" ? money(row.total_assets, currency) : money(row.ttm_revenue, currency);
  const financialValue = row.graham_profile === "UTILITY"
    ? `LT debt ${money(ltd, currency)} · total equity ${money(row.total_assets != null && row.total_liabilities != null ? row.total_assets - row.total_liabilities : null, currency)}`
    : `CR ${multiple(currentRatio)} · WC ${money(workingCapital, currency)} · LT debt ${money(ltd, currency)}`;
  const valuationProduct = pe3Value != null && priceToBook != null ? pe3Value * priceToBook : null;
  return [
    { label: "Adequate size", rule: row.graham_profile === "UTILITY" ? "Assets ≥ $50M" : "Revenue ≥ $100M", value: sizeValue, status: test("size") },
    { label: "Financial position", rule: row.graham_profile === "UTILITY" ? "LT debt ≤ 2× equity" : "CR ≥ 2 and LT debt ≤ working capital", value: financialValue, status: test("financial_position") },
    { label: "Earnings stability", rule: "No deficit in 10 FY", value: `${ch13.ten_year_positive ?? "—"} of ${ch13.ten_year_present ?? "—"} positive years`, status: test("stability_10y"), note: defensive?.windowed?.stability_10y },
    { label: "Dividend record", rule: "20 uninterrupted years", value: dividendEvidence, status: test("dividend_20y"), note: defensive?.windowed?.dividend_20y },
    { label: "Earnings growth", rule: "≥ 33⅓% over 10 years", value: ch13.growth_10y == null ? "—" : `${signed(ch13.growth_10y)}%`, status: test("growth_10y"), note: defensive?.windowed?.growth_10y },
    { label: "Valuation", rule: "P/E3 ≤ 15 and P/E3 × P/B ≤ 22.5", value: `P/E3 ${multiple(pe3Value)} · product ${multiple(valuationProduct)}`, status: test("valuation") },
  ];
}

// Where two decimals would land exactly on the threshold while the criterion was
// decided on the unrounded figure, the reader sees "1.50× · FAIL" under "≥ 1.50".
// Those nine rows get a third decimal rather than a contradiction.
const THRESHOLDS = { 1: 10.0, 2: 1.5, 3: 1.1, 7: 1.2 };

function shown(criterion) {
  const v = criterion.value;
  if (v == null) return null;
  const limit = THRESHOLDS[criterion.n];
  const rounded = Math.round(v * 100) / 100;
  return limit != null && rounded === limit && v !== limit
    ? Number(v).toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 })
    : number(v);
}

function enterprisingValue(criterion) {
  if (criterion.n === 1 || criterion.n === 2 || criterion.n === 3 || criterion.n === 7) {
    const v = shown(criterion);
    return v == null ? "—" : `${v}×`;
  }
  if (criterion.n === 5) return recurringDividendPresentation(criterion.value, criterion.note).value;
  return criterion.value == null ? "—" : number(criterion.value);
}

function enterprisingRow(criterion) {
  const cash = criterion.n === 5
    ? recurringDividendPresentation(criterion.value, criterion.note)
    : null;
  return {
    label: ENTERPRISING[criterion.n]?.label ?? `Criterion ${criterion.n}`,
    rule: ENTERPRISING[criterion.n]?.rule ?? "—",
    value: cash?.value ?? enterprisingValue(criterion),
    status: criterion.status,
    note: cash?.note ?? criterion.note,
  };
}

/** Separately labelled evidence around Buffett owner earnings. Not a Graham criterion. */
function OwnerEarnings({ oe, cik, annualEps, annualWeightedShares, currency = "USD" }) {
  if (!oe) return null;
  const rate = (value) => (value == null ? "—" : `${number(value)}%`);
  const trend = ownerEarningsTrend(oe);
  const fcfTrend = ownerMetricTrend(
    oe,
    "free_cash_flow_per_share",
    "free_cash_flow",
  );
  const cagrBasisLabel = (basis) => basis
    ? `FY${basis.firstFiscalYear}–FY${basis.lastFiscalYear} average → FY${basis.latestFiscalYear}`
    : undefined;
  const latest = trend?.rows.find((row) => row.fiscalYear === oe.fiscal_year);
  const years = trend?.rows ?? [];
  const bridgePoint = (cell, key) => {
    const point = cell?.cash_flow_bridge?.[key];
    if (Array.isArray(point)) {
      const [value, tag, form, accn, end, unit, document, fullSource] = point;
      return { value, source: fullSource ?? { tag, form, accn, end, unit, document } };
    }
    if (typeof point === "number") return { value: point, source: null };
    return point ?? null; // engine <=117 object compatibility
  };
  const hasBridgePoint = (key) => years.some(({ cell }) => bridgePoint(cell, key)?.value != null);
  const bridgeRows = [
    {
      key: "eps",
      label: "Diluted EPS",
      note: "The filing-reported per-share result; it can differ from net income divided by shares because of allocation and dilution rules.",
      value: (_, fiscalYear) => annualEps?.[fiscalYear] ?? annualEps?.[String(fiscalYear)],
      format: (value) => moneyPrice(value, currency),
    },
    {
      key: "shares",
      label: "Diluted weighted shares",
      note: "The average diluted security count behind EPS, restated for later splits and the priced receipt where applicable.",
      value: (cell, fiscalYear) => (
        annualWeightedShares?.[fiscalYear]
        ?? annualWeightedShares?.[String(fiscalYear)]
        ?? cell?.diluted_shares
      ),
      format: shareCount,
    },
    {
      key: "net_income",
      label: "Net income available to common",
      note: "After corporate income tax and financing costs; the owner-earnings starting profit.",
    },
    {
      key: "depreciation_and_amortisation",
      label: "+ Depreciation & amortisation",
      note: "A noncash expense added back in the operating cash-flow reconciliation.",
    },
    {
      key: "stock_compensation",
      label: "+ Stock compensation",
      note: "A noncash employee-pay expense added back inside OCF when it is separately filed.",
    },
    {
      key: "other_operating_cash_flow_adjustments",
      label: "+/− Other OCF adjustments",
      note: "Residual needed to reconcile the separately shown rows to OCF; it absorbs all other adjustments plus SBC or working capital when either cannot be isolated.",
    },
    {
      key: "working_capital_cash_effect",
      label: "+/− Working-capital cash effect",
      note: "The filed cash-flow effect of Δ operating working capital: positive supplies cash and negative consumes cash.",
    },
    {
      key: "operating_cash_flow",
      label: "= Operating cash flow",
      note: "Net cash provided by operating activities; after cash taxes, with interest classification following the filing.",
    },
    {
      key: "total_capital_expenditure",
      label: "− Total CapEx",
      note: "Shown as a cash use. Primary XBRL normally does not identify maintenance versus growth CapEx; FCF stays blank if only an accrual-based CapEx fact is available.",
      transform: (value) => -Math.abs(value),
    },
    ...(hasBridgePoint("maintenance_capital_expenditure") ? [{
      key: "maintenance_capital_expenditure",
      label: "  Maintenance CapEx",
      note: "Displayed only when the filing separately identifies maintenance spending.",
      transform: (value) => -Math.abs(value),
    }] : []),
    ...(hasBridgePoint("growth_capital_expenditure") ? [{
      key: "growth_capital_expenditure",
      label: "  Growth CapEx",
      note: "Displayed only when the filing separately identifies growth spending.",
      transform: (value) => -Math.abs(value),
    }] : []),
    {
      key: "free_cash_flow",
      label: "= Free cash flow",
      note: "Operating cash flow less cash CapEx; cash available before acquisitions, debt repayment, dividends, and buybacks.",
    },
    ...(hasBridgePoint("share_repurchases") ? [{
      key: "share_repurchases",
      label: "− Share repurchases (buybacks)",
      note: "Reported financing cash used to reacquire shares. It is shown as a use of FCF and is not deducted when calculating FCF.",
      transform: (value) => -Math.abs(value),
    }] : []),
  ];
  return (
    <section className="criteria-section">
      <div className="criteria-title">
        <div><h3>Owner earnings &amp; cash-generation evidence</h3>
          <p>Latest ten completed fiscal-year slots. The bridge shows how reported profit
             becomes operating cash flow, then deducts cash CapEx to reach FCF. Missing
             filing evidence remains a dash.</p></div>
        <b>10-year record</b>
      </div>
      {trend && (
        <div className="snapshot-grid owner-earnings-summary">
          <Metric label={`FY${oe.fiscal_year} reported earnings / share`}
                  sub="maintenance capex assumed equal to D&A; equals earnings by construction"
                  value={moneyPrice(latest?.perShare, currency)} />
          <Metric label="Earnings/share 10-slot CAGR" sub={cagrBasisLabel(trend.cagrBasis)}
                  value={rate(trend.cagr)} />
          <Metric label="Earnings/share 5-slot CAGR" sub={cagrBasisLabel(trend.cagr5Basis)}
                  value={rate(trend.cagr5)} />
          <Metric label="Earnings/share 3-slot CAGR" value={rate(trend.cagr3)} />
          <Metric label="FCF/share 10-slot CAGR"
                  sub={cagrBasisLabel(fcfTrend.cagrBasis)}
                  value={rate(fcfTrend.cagr)} />
          <Metric label="FCF/share 5-slot CAGR" sub={cagrBasisLabel(fcfTrend.cagr5Basis)}
                  value={rate(fcfTrend.cagr5)} />
          <Metric label="Total earnings CAGR" value={rate(trend.totalCagr)} />
          <Metric label="Diluted-share CAGR" value={rate(trend.shareCountCagr)} />
          <Metric label="10-year median / share" value={moneyPrice(trend.median10, currency)} />
          <Metric label="Latest vs 10-year median" value={signedPercent(trend.latestVsMedian10)} />
          <Metric label="History" value={`${trend.yearsPresent}/${trend.yearsExpected} years · ${trend.yearsProfitable} positive`} />
        </div>
      )}
      {trend?.buybackDriven && (
        <p className="criteria-note warning"><b>Buyback-driven growth:</b> per-share results
          improved while total reported earnings declined because diluted shares fell.</p>
      )}
      <p className="criteria-note"><b>Cash bridge:</b> net income + D&amp;A + separately
        reported stock compensation + other adjustments + the working-capital cash effect
        = OCF; OCF − cash CapEx = FCF. Unseparated SBC or working-capital effects remain
        inside the residual instead of becoming zero. Maintenance and growth CapEx appear only
        when the filing identifies them.</p>
      {!!years.length && <div className="annual-history-scroll owner-earnings-history">
        <table className="annual-history-table owner-cash-bridge">
          <thead><tr><th>Measurement</th>{years.map(({ fiscalYear }) => (
            <th key={fiscalYear} className="num">FY{fiscalYear}</th>
          ))}</tr></thead>
          <tbody>{bridgeRows.map((row) => (
            <tr key={row.key}>
              <td><b>{row.label}</b><small>{row.note}</small></td>
              {years.map(({ fiscalYear, cell }) => {
                const point = bridgePoint(cell, row.key);
                const rawValue = row.value ? row.value(cell, fiscalYear) : point?.value;
                const value = rawValue == null ? null : (row.transform?.(rawValue) ?? rawValue);
                return <td key={fiscalYear} className={`num ${value < 0 ? "negative" : ""}`}
                  title={point?.source?.tag}>
                  <b>{row.format ? row.format(value) : money(value, currency)}</b>
                  {point?.source && <span className="bridge-source">
                    <SourceCell cik={cik} source={point.source} />
                  </span>}
                </td>;
              })}
            </tr>
          ))}</tbody>
        </table>
      </div>}
      {!!oe.caveats?.length && <ul className="disclosure-notes compact">
        {oe.caveats.map((caveat) => <li key={caveat}>{caveat}</li>)}
      </ul>}
    </section>
  );
}

/** Disclosure paragraphs: read alongside the grades, never folded into them. */
function Notes({ title, subtitle, notes }) {
  if (!notes || notes.length === 0) return null;
  return (
    <section className="criteria-section notes-section">
      <div className="criteria-title"><div><h3>{title}</h3><p>{subtitle}</p></div></div>
      <ul className="disclosure-notes">
        {notes.map((note, i) => (
          <li key={i}>
            {note.kind && <b className="note-kind">{note.kind}</b>}
            {note.text ?? note}
          </li>
        ))}
      </ul>
    </section>
  );
}

const SOURCE_LABELS = {
  total_assets: "Total assets", total_liabilities: "Total liabilities",
  current_assets: "Current assets", current_liabilities: "Current liabilities",
  long_term_debt: "Long-term debt", short_term_debt: "Short-term debt",
  total_debt: "Total debt (rollup)", goodwill: "Goodwill", intangibles: "Intangibles",
  operating_lease_liability: "Operating-lease liabilities", lease_cost: "Lease cost",
  fixed_charge_coverage: "Fixed-charge coverage proxy",
  inventory: "Inventory", receivables: "Receivables", cash: "Cash",
  short_term_investments: "Short-term investments",
  preferred_stock: "Preferred stock", temporary_equity: "Temporary equity",
  noncontrolling_interest: "Noncontrolling interest", shares: "Shares outstanding",
  common_equity: "Common equity (derived)", parent_equity: "Parent equity (derived)",
  dividend: "Dividend paid (tagged period)",
  recurring_dividend_per_share: "Recurring dividend / share (annualized)",
};

/** The filing's index page, which names its primary document — not the bare
 * archive folder, which leaves the reader to guess which file is the filing. */
function SourceCell({ cik, source }) {
  const url = edgarUrl(cik, source.accn);
  const label = `${source.form} · ${source.end ?? "—"}`;
  return (
    <>
      {url ? <a href={url} target="_blank" rel="noreferrer" title={`Open filing ${source.accn}`}>{label}</a>
           : label}
      <small className="accn">{source.document ?? source.accn}</small>
    </>
  );
}

/** Every figure names its tag, filing and date — the audit trail behind the screen. */
function Provenance({ row }) {
  const equity = row.equity_evidence;
  const sources = {
    ...row.sources,
    ...(equity?.current?.source ? { common_equity: equity.current.source } : {}),
    ...(equity?.parent?.source ? { parent_equity: equity.parent.source } : {}),
  };
  if (!sources || Object.keys(sources).length === 0) return null;
  return (
    <section className="criteria-section provenance-section">
      <div className="criteria-title"><div><h3>Data provenance</h3>
        <p>Exact source element or workbook row, document, and reporting date.</p></div></div>
      <div className="provenance-scroll">
        <table className="criteria-clean">
          <thead><tr><th>Figure</th><th>Value</th><th>Tag</th><th>Filing</th></tr></thead>
          <tbody>
            {Object.entries(SOURCE_LABELS).filter(([key]) => sources[key]).flatMap(([key, label]) => {
              const s = sources[key];
              const stored = key === "common_equity" ? equity?.current?.value
                : key === "parent_equity" ? equity?.parent?.value
                : row[key] ?? row.asset_quality?.[key];
              const raw = key === "dividend" && stored == null ? undefined : stored;
              const value = key === "shares" ? number(raw)
                : key === "fixed_charge_coverage" ? multiple(raw)
                  : money(raw, key === "common_equity" || key === "parent_equity"
                    ? row.reporting_currency ?? row.currency : row.currency);
              const out = [(
                <tr key={key}>
                  <td><b>{label}</b></td>
                  <td className="num">{value}</td>
                  <td className="rule"><code>{s.tag}</code></td>
                  <td><SourceCell cik={row.cik} source={s} /></td>
                </tr>
              )];
              // a summed figure's own filing is whichever component was newest,
              // so each component states its own rather than borrowing that one
              (s.components ?? []).forEach((part, i) => out.push(
                <tr key={`${key}-${i}`} className="component-row">
                  <td><span className="component-mark">↳ component</span></td>
                  <td className="num"></td>
                  <td className="rule"><code>{part.tag}</code></td>
                  <td><SourceCell cik={row.cik} source={part} /></td>
                </tr>
              ));
              return out;
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** A series stitched from more than one tag can change scope mid-history. */
/** The questions this company's filings answer and its XBRL does not. Every other
 * figure on the page carries a tag and an accession; these carry a document to read. */
function ProseGaps({ row }) {
  const gaps = row.prose_gaps ?? [];
  if (!gaps.length) return null;
  return (
    <section className="criteria-section prose-gaps">
      <div className="criteria-title">
        <div>
          <h3>Check the filing</h3>
          <p>Answers that exist only as prose in a document — not tagged, so not extractable.
             Nothing here has been guessed at.</p>
        </div>
      </div>
      <ul className="disclosure-notes">
        {gaps.map((gap, i) => (
          <li key={i}>
            <b className="note-kind">Unverified</b>
            {gap.what}. <em>Affects:</em> {gap.affects}. <em>Where:</em> {gap.where}
            {gap.accn && <> — <a href={edgarUrl(row.cik, gap.accn)} target="_blank" rel="noreferrer">open the filing</a></>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function SeriesMix({ mix }) {
  if (!mix) return null;
  const NAMES = { eps: "EPS", net_income: "Net income", revenue: "Revenue" };
  return (
    <div className="series-mix">
      {Object.entries(mix).map(([series, tags]) => (
        <p key={series} className="criteria-note">
          <b>{NAMES[series] ?? series} series is stitched from {Object.keys(tags).length} tags:</b>{" "}
          {Object.entries(tags).map(([tag, years]) =>
            `${tag.replace("us-gaap:", "")} (${years[0]}–${years[years.length - 1]})`).join(" · ")}
          {" — "}scope can differ between tags; judge year-over-year steps that cross a boundary accordingly.
        </p>
      ))}
    </div>
  );
}

function Status({ value }) {
  const label = {
    PASS: "Pass", ALIGNED: "Aligned", FAIL: "Fail", BLOCKED: "Blocked",
    INSUFFICIENT_DATA: "Incomplete", EVIDENCE_INCOMPLETE: "Incomplete",
    NOT_APPLICABLE: "N/A", OUT_OF_SCOPE: "Out of scope",
  }[value] ?? "Incomplete";
  return <span className={`criterion-status ${String(value ?? "INSUFFICIENT_DATA").toLowerCase()}`}>{label}</span>;
}

function number(value) {
  return value == null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 2 });
}
function multiple(value) { return value == null ? "—" : `${number(value)}×`; }
function signedPercent(value) { return value == null ? "—" : `${value >= 0 ? "+" : ""}${number(value)}%`; }
function signed(value) { return value >= 0 ? `+${number(value)}` : number(value); }
function money(value, currency = "USD") {
  if (value == null) return "—";
  // the minus sign belongs in front of the amount, not between the currency and
  // the digits: Walmart's working capital rendered as "$-26.2B"
  const sign = value < 0 ? "−" : "";
  const symbol = ({ EUR: "€", GBP: "£", JPY: "¥" }[currency] ?? "$");
  const absolute = Math.abs(value);
  for (const [divisor, suffix] of [[1e12, "T"], [1e9, "B"], [1e6, "M"], [1e3, "K"]])
    if (absolute >= divisor) {
      const scaled = absolute / divisor;
      const precision = suffix === "B" ? 2 : (scaled < 10 ? 1 : 0);
      return `${sign}${symbol}${scaled.toFixed(precision)}${suffix}`;
    }
  return `${sign}${symbol}${number(absolute)}`;
}
function rateOrDash(value) {
  return value == null ? "—" : `${Number(value).toFixed(1)}%`;
}
function moneyPrice(value, currency = "USD") {
  if (value == null) return "—";
  const sign = value < 0 ? "−" : "";
  const symbol = ({ EUR: "€", GBP: "£", JPY: "¥" }[currency] ?? "$");
  const absolute = Math.abs(value);
  if (absolute < 1) return `${sign}${symbol}${absolute.toFixed(3)}`;
  return `${sign}${symbol}${absolute.toFixed(2)}`;
}
function shareCount(value) {
  if (!(value > 0)) return "—";
  for (const [divisor, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]])
    if (value >= divisor) {
      const scaled = value / divisor;
      return `${suffix === "B" ? scaled.toFixed(2) : number(scaled)}${suffix}`;
    }
  return number(value);
}
