import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import Detail from "./Detail.jsx";
import LoadBar from "./LoadBar.jsx";
import DashboardFilters, { EPS_POSITIVE_FLOORS } from "./DashboardFilters.jsx";
import MarketReference from "./MarketReference.jsx";
import { epsEvidence } from "./EarningsEvidence.jsx";
import CompanyTable from "./CompanyTable.jsx";
import { fetchJson, send } from "./api.js";
import { hasActiveFilters, legacyViewSort, loadView, saveView, takeOverScrollRestoration, unfilteredView } from "./view.js";
import { TOTAL_CRITERIA, byN, indexValuation, pe3,
         returnQualityPercentiles, valuationPrice } from "./screen.js";
import { compareRows, normalizeSort, updateSort } from "./sort.js";
import { payloadWarnings } from "./warnings.js";
import Portfolio from "./Portfolio.jsx";
import TradeModal from "./TradeModal.jsx";
import { matchesPortfolio, matchesPortfolioAsset, openPortfolioCiks, portfolioHoldingCount } from "./portfolio.js";


// OR semantics: any typed word matching any field keeps the row. Short and
// common words are dropped first, or "pays no dividend" would match half the
// market on "no". The whole phrase is always tried too, so exact wording wins.
const STOPWORDS = new Set(["the", "and", "for", "with", "not", "are", "was", "its", "inc", "corp"]);

function matcher(query) {
  const phrase = query.trim().toLowerCase();
  if (!phrase) return null;
  const words = phrase
    .split(/[\s,]+/)
    .filter((w) => w.length > 2 && !STOPWORDS.has(w));
  const needles = words.length ? [phrase, ...words] : [phrase];
  return (rows) => rows.filter((r) => needles.some((n) => r.hay.includes(n)));
}


function enterprisingGap(row) {
  const screen = row.alignment?.enterprising;
  if (!screen || row.profile !== "OPERATING") return "OUT_OF_SCOPE";
  if (screen.unknown > 0 || screen.verdict === "EVIDENCE_INCOMPLETE") return "INCOMPLETE";
  const gaps = (screen.total ?? 0) - (screen.passed ?? 0);
  if (gaps === 0) return "ALL_PASS";
  if (gaps === 1) return "ONE_GAP";
  if (gaps === 2) return "TWO_GAPS";
  return "THREE_PLUS";
}

// narrowest first: 30 companies beat 500 beat ~100 beat every Nasdaq listing
const INDEX_RANK = { "DJIA": 0, "S&P 500": 1, "Nasdaq 100": 2, "Nasdaq Comp": 3 };
const saved = loadView();
takeOverScrollRestoration();

const initialSort = legacyViewSort(saved.sort);

export default function App() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [q, setQ] = useState(saved.q);
  const [lens, setLens] = useState(saved.lens);
  const [fit, setFit] = useState(saved.fit);
  const [gaps, setGaps] = useState(saved.gaps);
  const [sort, setSort] = useState(() => normalizeSort(initialSort));
  const [visibleCount, setVisibleCount] = useState(300);
  const loadMoreRef = useRef(null);
  const [sectors_, setSectors] = useState(new Set(saved.sectors));
  const [profiles, setProfiles] = useState(new Set(saved.profiles));
  const [idxSel, setIdxSel] = useState(new Set(saved.indexes));
  const [venues, setVenues] = useState(new Set(saved.venues));
  const [minCap, setMinCap] = useState(saved.minCap);   // micro caps are mostly noise here
  // criteria satisfied, 0 = no floor; a floor saved when the screen had more
  // criteria would now match nothing, so it is clamped to what exists today
  const [minMet, setMinMet] = useState(Math.min(saved.minMet, TOTAL_CRITERIA));
  const [minPositiveEps, setMinPositiveEps] = useState(saved.minPositiveEps);
  const [maxPe, setMaxPe] = useState(saved.maxPe);       // 0 = no ceiling
  const [maxPe3, setMaxPe3] = useState(saved.maxPe3);    // 0 = no ceiling
  const [selected, setSelected] = useState(null);
  const [tracked, setTracked] = useState(new Set());
  const [trackingError, setTrackingError] = useState(null);
  const [trackedOnly, setTrackedOnly] = useState(saved.trackedOnly);
  const [hideNA, setHideNA] = useState(saved.hideNA);
  const [hideNoApply, setHideNoApply] = useState(saved.hideNoApply);
  const [belowNcav, setBelowNcav] = useState(saved.belowNcav);
  const [page, setPage] = useState(() => window.location.hash === "#portfolio" ? "portfolio" : "screener");
  const [portfolioData, setPortfolioData] = useState(null);
  const [portfolioError, setPortfolioError] = useState(null);
  const [portfolioLoading, setPortfolioLoading] = useState(true);
  const [portfolioQ, setPortfolioQ] = useState("");
  const [tradeTarget, setTradeTarget] = useState(null);

  const load = useCallback(() => {
    // cache-bust so a finished sync shows immediately rather than the stale payload
    fetch(`/dashboard.json?t=${Date.now()}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  const loadPortfolio = useCallback((silent = false) => {
    if (!silent) setPortfolioLoading(true);
    fetchJson(`/portfolio?t=${Date.now()}`, { timeoutMs: 15_000 })
      .then((result) => { setPortfolioData(result); setPortfolioError(null); })
      .catch((err) => setPortfolioError(err.message))
      .finally(() => { if (!silent) setPortfolioLoading(false); });
  }, []);

  useEffect(() => {
    loadPortfolio();
    if (page !== "portfolio") return undefined;
    const timer = window.setInterval(() => loadPortfolio(true), 30_000);
    return () => window.clearInterval(timer);
  }, [loadPortfolio, page]);

  useEffect(() => {
    const changed = () => setPage(window.location.hash === "#portfolio" ? "portfolio" : "screener");
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);

  useEffect(() => {
    saveView({
      q, lens, fit, gaps, sectors: [...sectors_], profiles: [...profiles], venues: [...venues],
      indexes: [...idxSel],
      minCap, minMet, minPositiveEps, maxPe, maxPe3,
      hideNA, hideNoApply, belowNcav, trackedOnly, sort,
    });
  }, [q, lens, fit, gaps, sectors_, profiles, venues, idxSel, minCap, minMet,
    minPositiveEps, maxPe, maxPe3, hideNA, hideNoApply, belowNcav, trackedOnly, sort]);

  useEffect(() => {
    const onScroll = () => saveView({ scroll: window.scrollY });
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // restore only after the rows exist, or the page is still too short to scroll
  const restored = useRef(false);
  useLayoutEffect(() => {
    if (restored.current || !data || !saved.scroll) return;
    restored.current = true;
    requestAnimationFrame(() => window.scrollTo(0, saved.scroll));
  }, [data]);

  useEffect(() => {
    fetch("/tracked")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => {
        if (!Array.isArray(d.tracked)) throw new Error("invalid tracking response");
        setTracked(new Set(d.tracked.map((t) => t.cik)));
        setTrackingError(null);
      })
      .catch((e) => setTrackingError(`Tracked companies could not be loaded: ${e.message}`));
  }, []);

  const toggleTracked = async (row, e) => {
    e?.stopPropagation();         // the row click opens the detail panel; the detail star has none
    const on = tracked.has(row.cik);
    setTracked((prev) => {        // optimistic: the list is local and cheap to revert
      const next = new Set(prev);
      on ? next.delete(row.cik) : next.add(row.cik);
      return next;
    });
    try {
      const response = await send(on ? `/tracked/${row.cik}` : "/tracked", {
        method: on ? "DELETE" : "POST",
        body: on ? undefined : { cik: row.cik },
      });
      const result = await response.json().catch(() => null);
      if (!response.ok || result?.tracked !== !on || result?.cik !== row.cik)
        throw new Error(result?.detail ?? `HTTP ${response.status}: invalid tracking response`);
      setTrackingError(null);
    } catch (e) {
      // The optimistic star must not claim persistence the server did not confirm.
      setTracked((prev) => {
        const next = new Set(prev);
        on ? next.add(row.cik) : next.delete(row.cik);
        return next;
      });
      setTrackingError(`Tracking change was not saved: ${e.message}`);
    }
  };

  const navigate = (next) => {
    window.location.hash = next === "portfolio" ? "portfolio" : "";
    setPage(next);
  };

  const clearFilters = () => {
    const empty = unfilteredView();
    setLens(empty.lens);
    setFit(empty.fit);
    setGaps(empty.gaps);
    setProfiles(new Set(empty.profiles));
    setSectors(new Set(empty.sectors));
    setVenues(new Set(empty.venues));
    setIdxSel(new Set(empty.indexes));
    setMinCap(empty.minCap);
    setMinMet(empty.minMet);
    setMinPositiveEps(empty.minPositiveEps);
    setMaxPe(empty.maxPe);
    setMaxPe3(empty.maxPe3);
    setTrackedOnly(empty.trackedOnly);
    setHideNA(empty.hideNA);
    setHideNoApply(empty.hideNoApply);
    setBelowNcav(empty.belowNcav);
  };

  const deleteTrade = async (trade) => {
    if (!portfolioData) return;
    const description = `${trade.side} ${trade.quantity} ${trade.ticker} at $${trade.price}`;
    if (!window.confirm(`Delete ${description}?\n\nThis corrects the local ledger and cannot be undone.`)) return;
    try {
      const response = await send(`/portfolio/${portfolioData.portfolio.id}/trades/${trade.id}`, {
        method: "DELETE",
      });
      const result = await response.json().catch(() => null);
      if (!response.ok || !result?.portfolio)
        throw new Error(result?.detail ?? `HTTP ${response.status}: trade was not deleted`);
      setPortfolioData(result.portfolio);
      setPortfolioError(null);
      loadPortfolio();
    } catch (err) {
      setPortfolioError(err.message);
    }
  };

  const portfolioCiks = useMemo(() => openPortfolioCiks(portfolioData), [portfolioData]);

  const rows = useMemo(() => {
    if (!data) return [];
    const returnQualities = returnQualityPercentiles(data.rows);
    return data.rows.map((r, index) => {
      const financialPrice = valuationPrice(r);
      return {
        ...r,
        mcap: r.price && r.shares ? r.price * r.shares : null,
        eps10: epsEvidence(r.annual_eps, r.ch13?.latest_fy),
        // any criterion we could not decide either way leaves the verdict incomplete
        unjudged: r.criteria.some((c) => c.status !== "PASS" && c.status !== "FAIL"),
        // criteria 2 and 3 cannot be asked of a filer with no classified balance
        // sheet — unlike a missing figure, no later filing will ever supply it
        inapplicable: r.criteria.some((c) => c.status === "NOT_APPLICABLE"),
        netNet: r.ncavps != null && financialPrice != null
          && r.ncavps > 0 && financialPrice <= r.ncavps,
        // only while net current assets are positive: a negative denominator
        // would turn the cheapest-looking ratio into the most expensive company
        pncav: r.ncavps != null && financialPrice != null && r.ncavps > 0
          ? financialPrice / r.ncavps : null,
        idx: r.index_memberships ?? [],
        pe3: pe3(r),
        returnQuality: returnQualities[index],
        dividendYield: dividendYield(r),
        // This is the explicitly labelled total-capex floor return, not a
        // definitive Buffett owner-earnings return.
        roic: r.owner_earnings?.all_capex_return ?? null,
        profile: r.graham_profile ?? "REVIEW",
        warnings: payloadWarnings(r),
        // searched fields, flattened once: identity, industry, venue, and country
        hay: [r.ticker, r.name, r.sector, r.industry, r.exchange,
              r.graham_profile, r.incorporation?.split("|")[1]]
          .filter(Boolean).join(" ").toLowerCase(),
      };
    });
  }, [data]);

  // This market reference deliberately ignores the filters below. It describes
  // every S&P 500 member represented in the loaded dashboard payload.
  const sp500 = useMemo(() => indexValuation(rows, "S&P 500"), [rows]);

  const view = useMemo(() => {
    const match = matcher(q);
    let out = rows;
    if (profiles.size) out = out.filter((r) => profiles.has(r.profile));
    if (lens !== "BOTH") {
      const key = lens.toLowerCase();
      // Selecting one lens also hides companies to which that historical method
      // does not apply; their valuation evidence remains available under Both.
      out = out.filter((r) => r.alignment?.[key]?.verdict !== "OUT_OF_SCOPE");
    }
    if (fit !== "ALL") {
      const key = lens === "BOTH" ? null : lens.toLowerCase();
      out = out.filter((r) => key
        ? r.alignment?.[key]?.verdict === fit
        : [r.alignment?.enterprising?.verdict, r.alignment?.defensive?.verdict].includes(fit));
    }
    // Gaps are meaningful only for Chapter 15's industrial direct tests.
    if (lens === "ENTERPRISING" && gaps !== "ALL")
      out = out.filter((r) => enterprisingGap(r) === gaps);
    if (sectors_.size) out = out.filter((r) => sectors_.has(r.sector ?? "Unclassified"));
    if (venues.size) out = out.filter((r) => venues.has(r.exchange));
    if (idxSel.size)
      out = out.filter((r) =>
        r.idx.some((m) => idxSel.has(m)) || (idxSel.has("Outside") && r.idx.length === 0));
    // a company with no price or no share count cannot be sized, so a floor excludes it
    if (minCap) out = out.filter((r) => (r.mcap ?? 0) >= minCap);
    if (trackedOnly) out = out.filter((r) => tracked.has(r.cik));
    if (minMet) out = out.filter((r) => r.n_pass >= minMet);
    // Uses the same completed FY(L-9)..FY(L) evidence window shown in the table.
    // Missing years stay missing; they are never silently filled with zero.
    if (minPositiveEps) out = out.filter((r) => r.eps10?.positive >= minPositiveEps);
    // A maximum multiple is meaningful only for positive earnings. Missing or
    // non-positive multiples stay missing; they never sneak under the ceiling.
    if (maxPe) out = out.filter((r) => {
      const value = byN(r, 1).value;
      return value != null && value > 0 && value <= maxPe;
    });
    if (maxPe3) out = out.filter((r) => r.pe3 != null && r.pe3 > 0 && r.pe3 <= maxPe3);
    if (hideNoApply) out = out.filter((r) => !r.inapplicable);
    if (hideNA) out = out.filter((r) => !r.unjudged);
    if (belowNcav) out = out.filter((r) => r.netNet);
    if (match) out = match(out);
    // null means "no value" — never a number. Returning Infinity here would park
    // those rows at the top as soon as the sort flipped to descending.
    const val = (r, key) => {
      // Graham fit displays Enterprising and Defensive points together, so its
      // sort follows that visible combined score rather than verdict labels.
      if (key === "ticker") return r.ticker ?? "";
      if (key === "sector") return r.sector ?? null;
      if (key === "name") return r.name ?? "";
      if (key === "n_pass") return -r.n_pass;
      if (key === "pe") return byN(r, 1).value ?? null;
      if (key === "pe3") return r.pe3 ?? null;
      if (key === "dividend") return r.dividendYield == null ? null : -r.dividendYield;
      // the interesting end is the most beaten-down, so sort those to the top
      if (key === "offhigh")
        return r.price_stats?.pct_below_52w_high == null
          ? null : -r.price_stats.pct_below_52w_high;
      if (key === "price") return r.price ?? null;
      if (key === "mcap") return r.mcap == null ? null : -r.mcap;
      if (key === "eps10") {
        // The column is sorted by the visible positive-year count, not by its
        // separate Graham growth calculation.  A negative value puts the most
        // positive years first on the initial descending-style sort.
        return r.eps10 ? -r.eps10.positive : null;
      }
      if (key === "returns")
        return r.returnQuality.score == null ? null : -r.returnQuality.score;
      return 0;
    };
    return out.sort((a, b) => compareRows(a, b, sort, val));
  }, [rows, q, lens, fit, gaps, sort, sectors_, profiles, venues, idxSel, minCap,
    minMet, minPositiveEps, maxPe, maxPe3, hideNA, hideNoApply, belowNcav,
    trackedOnly, tracked]);

  // Keep the initial DOM small, then append another page when the reader reaches
  // the end. Filtering/sorting starts a fresh window; the sentinel below handles
  // the rest without requiring a second click.
  useEffect(() => {
    setVisibleCount(300);
  }, [q, lens, fit, gaps, sort, sectors_, profiles, venues, idxSel, minCap, minMet,
    minPositiveEps, maxPe, maxPe3, hideNA, hideNoApply, belowNcav, trackedOnly, tracked]);
  useEffect(() => {
    const node = loadMoreRef.current;
    if (!node || visibleCount >= view.length || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting))
        setVisibleCount((count) => Math.min(count + 300, view.length));
    }, { rootMargin: "600px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [visibleCount, view.length]);

  // Counted over the rows on screen, not over all 5,893: with the app's own
  // defaults the "below NCAV" chip read 132 beside a table holding one of them,
  // and "judged on all 6" read 4,393 against 896. A count that describes a
  // universe the reader cannot see is not a count of anything they can act on.
  const positiveEpsCounts = useMemo(() => Object.fromEntries(
    EPS_POSITIVE_FLOORS.map((floor) => [floor, view.filter((r) => r.eps10?.positive >= floor).length])
  ), [view]);
  const naCount = useMemo(() => view.filter((r) => r.unjudged).length, [view]);
  const netNetCount = useMemo(() => view.filter((r) => r.netNet).length, [view]);
  const noApplyCount = useMemo(() => view.filter((r) => r.inapplicable).length, [view]);
  const portfolioMatches = useMemo(() => [
    ...(portfolioData?.positions ?? []),
    ...(portfolioData?.assets?.bonds ?? []),
    ...(portfolioData?.assets?.crypto ?? []),
  ].filter((holding) => holding.cik
    ? matchesPortfolio(holding, portfolioQ)
    : matchesPortfolioAsset(holding, portfolioQ)).length, [portfolioData, portfolioQ]);

  const metCounts = useMemo(() => {
    const c = {};
    view.forEach((r) => {
      for (let n = 1; n <= r.n_pass; n++) c[n] = (c[n] ?? 0) + 1;
    });
    return c;
  }, [view]);

  const sectorCounts = useMemo(() => {
    const c = {};
    rows.forEach((r) => {
      const k = r.sector ?? "Unclassified";
      c[k] = (c[k] ?? 0) + 1;
    });
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  }, [rows]);
  const profileCounts = useMemo(() => {
    const c = {};
    rows.forEach((r) => (c[r.profile] = (c[r.profile] ?? 0) + 1));
    return Object.entries(c).sort((a, b) => a[0].localeCompare(b[0]));
  }, [rows]);
  const exchanges = useMemo(() => {
    const c = {};
    rows.forEach((r) => r.exchange && (c[r.exchange] = (c[r.exchange] ?? 0) + 1));
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  }, [rows]);
  const indexCounts = useMemo(() => {
    const c = { Outside: 0 };
    rows.forEach((r) => {
      if (!r.idx.length) c.Outside += 1;
      r.idx.forEach((m) => (c[m] = (c[m] ?? 0) + 1));
    });
    return Object.entries(c)
      .sort((a, b) => (INDEX_RANK[a[0]] ?? 9) - (INDEX_RANK[b[0]] ?? 9));
  }, [rows]);

  // a search that hits nothing inside the active filters, but matches elsewhere,
  // must say so rather than showing an empty table
  const matchesAnywhere = useMemo(() => {
    const match = matcher(q);
    return match ? match(rows).length : 0;
  }, [rows, q]);
  const activeFilters = hasActiveFilters({
    lens, fit, gaps, profiles, sectors: sectors_, venues, indexes: idxSel,
    minCap, minMet, minPositiveEps, maxPe, maxPe3,
    trackedOnly, hideNA, hideNoApply, belowNcav,
  });


  if (error)
    return (
      <div className="app">
        <LoadBar onFinished={load} />
        <div className="msg err">
          No data yet — {error}
          <br />
          Use <b>Load all filers</b> above, or run <code>make bulk &amp;&amp; make export</code>.
        </div>
      </div>
    );
  if (!data) return <div className="msg">Loading universe…</div>;

  const sortBy = (key, additive) => setSort((current) => updateSort(current, key, additive));
  const recordTrade = (row) => setTradeTarget(row ?? {});

  if (page === "portfolio") return (
    <div className="app">
      <AppHeader page={page} onNavigate={navigate} rows={rows} data={data}
                 portfolioData={portfolioData} portfolioQ={portfolioQ}
                 setPortfolioQ={setPortfolioQ} portfolioMatches={portfolioMatches} />
      <LoadBar onFinished={() => { load(); loadPortfolio(); }} shown={rows.length} />
      <Portfolio data={portfolioData} loading={portfolioLoading} error={portfolioError}
                 query={portfolioQ}
                 onRecordTrade={recordTrade} onDeleteTrade={deleteTrade}
                 onDataChange={setPortfolioData} />
      {tradeTarget !== null && portfolioData && (
        <TradeModal portfolio={portfolioData.portfolio} rows={rows}
                    initialRow={tradeTarget.cik ? tradeTarget : null}
                    onClose={() => setTradeTarget(null)}
                    onSaved={(next) => { setPortfolioData(next); setTradeTarget(null); loadPortfolio(); }} />
      )}
    </div>
  );

  return (
    <div className="app">
      <AppHeader page={page} onNavigate={navigate} rows={rows} data={data}
                 portfolioData={portfolioData} q={q} setQ={setQ} matches={view.length} />
      <LoadBar onFinished={load} shown={rows.length} />

      <MarketReference overview={sp500} />

      {trackingError && <div className="msg err">{trackingError}</div>}

      <DashboardFilters view={{ lens, fit, gaps, profiles, sectors: sectors_, venues, indexes: idxSel, minCap, minMet, minPositiveEps, maxPe, maxPe3, hideNA, hideNoApply, belowNcav, trackedOnly }}
        onChange={(patch) => {
          const setters = {
            lens: setLens, fit: setFit, gaps: setGaps,
            profiles: (value) => setProfiles(new Set(value)), sectors: (value) => setSectors(new Set(value)),
            venues: (value) => setVenues(new Set(value)), indexes: (value) => setIdxSel(new Set(value)),
            minCap: setMinCap, minMet: setMinMet, minPositiveEps: setMinPositiveEps,
            maxPe: setMaxPe, maxPe3: setMaxPe3, hideNA: setHideNA, hideNoApply: setHideNoApply,
            belowNcav: setBelowNcav, trackedOnly: setTrackedOnly,
          };
          Object.entries(patch).forEach(([key, value]) => setters[key](value));
        }}
        facets={{ profiles: profileCounts, sectors: sectorCounts, venues: exchanges, indexes: indexCounts }}
        counts={{ unjudged: naCount, inapplicable: noApplyCount, net_net: netNetCount,
          met_1: metCounts[1], met_2: metCounts[2], met_3: metCounts[3], met_4: metCounts[4], met_5: metCounts[5], met_6: metCounts[6],
          eps_5: positiveEpsCounts[5], eps_6: positiveEpsCounts[6], eps_7: positiveEpsCounts[7], eps_9: positiveEpsCounts[9], eps_10: positiveEpsCounts[10] }}
        total={rows.length} matched={view.length} trackedCount={tracked.size}
        activeFilters={activeFilters} onClear={clearFilters} />

      <CompanyTable rows={view.slice(0, visibleCount)} sort={sort} onSort={sortBy}
                    onSelect={setSelected} tracked={tracked} portfolioCiks={portfolioCiks}
                    onToggleTracked={toggleTracked} />
      {visibleCount < view.length && (
        <div ref={loadMoreRef} className="load-more" aria-live="polite">
          <button className="linkish" onClick={() => setVisibleCount((count) => Math.min(count + 300, view.length))}>
            Load more ({Math.min(300, view.length - visibleCount).toLocaleString()})
          </button>
        </div>
      )}
      {view.length === 0 && matchesAnywhere > 0 && (
        <p className="msg">
          No match inside the current filters, but <b>{matchesAnywhere.toLocaleString()}</b>{" "}
          elsewhere.{" "}
          <button className="linkish" onClick={clearFilters}>
            search everything
          </button>
        </p>
      )}
      {view.length === 0 && matchesAnywhere === 0 && q.trim() && (
        <p className="msg dim">Nothing matches “{q.trim()}”.</p>
      )}

      {selected && <Detail row={selected} onClose={() => setSelected(null)}
                           tracked={tracked.has(selected.cik)}
                           onToggleTracked={() => toggleTracked(selected)}
                           onRecordTrade={(detailRow) => recordTrade(detailRow ?? selected)} />}
      {tradeTarget !== null && portfolioData && (
        <TradeModal portfolio={portfolioData.portfolio} rows={rows}
                    initialRow={tradeTarget.cik ? tradeTarget : null}
                    onClose={() => setTradeTarget(null)}
                    onSaved={(next) => { setPortfolioData(next); setTradeTarget(null); }} />
      )}
    </div>
  );
}

function AppHeader({ page, onNavigate, rows, data, portfolioData, q, setQ, matches,
                     portfolioQ, setPortfolioQ, portfolioMatches }) {
  const searchValue = page === "portfolio" ? portfolioQ : q;
  const setSearch = page === "portfolio" ? setPortfolioQ : setQ;
  const searchMatches = page === "portfolio" ? portfolioMatches : matches;
  return (
    <header className="app-header">
      <div>
        <h1>Graham Screener</h1>
        <p className="sub">
          {page === "portfolio"
            ? `${portfolioHoldingCount(portfolioData)} holdings · stocks, bonds, crypto and liquid cash`
            : `${rows.length.toLocaleString()} companies · enterprising and defensive evidence · engine v${data.engine_version}`}
        </p>
        <nav className="app-nav" aria-label="Primary views">
          <button className={page === "screener" ? "active" : ""} onClick={() => onNavigate("screener")}>Research</button>
          <button className={page === "portfolio" ? "active" : ""} onClick={() => onNavigate("portfolio")}>
            Portfolio <span>{portfolioHoldingCount(portfolioData)}</span>
          </button>
        </nav>
      </div>
      <div className="searchwrap">
        <input className="search" placeholder={page === "portfolio" ? "Search portfolio…" : "Search…"}
               value={searchValue ?? ""}
               onChange={(event) => setSearch(event.target.value)} />
        {(searchValue ?? "").trim() && <span className="searchhint">
          {(searchMatches ?? 0).toLocaleString()} match{searchMatches === 1 ? "" : "es"}
        </span>}
      </div>
    </header>
  );
}

const dividendYield = (r) => {
  const dps = r.recurring_dividend_per_share ?? r.dividend_per_share;
  const price = valuationPrice(r);
  return dps != null && price > 0 ? (dps / price) * 100 : null;
};

export { drawdownTitle } from "./CompanyTable.jsx";
