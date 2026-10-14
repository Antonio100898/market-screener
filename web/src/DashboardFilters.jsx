import MultiSelect from "./MultiSelect.jsx";
import { TOTAL_CRITERIA } from "./screen.js";

export const EPS_POSITIVE_FLOORS = [5, 6, 7, 9, 10];
const PE_MAXIMUMS = [10, 15, 20, 25, 30];
const LENSES = ["BOTH", "ENTERPRISING", "DEFENSIVE"];
const FITS = ["ALL", "ALIGNED", "EVIDENCE_INCOMPLETE", "BLOCKED"];
const PROFILE_NAMES = {
  OPERATING: "Operating businesses", UTILITY: "Public utilities",
  FINANCIAL: "Financials & real estate", SPECIAL: "Special structures",
  REVIEW: "Manual review",
};
export default function DashboardFilters({ view, onChange, facets, counts, total, matched, trackedCount, activeFilters, onClear }) {
  const { lens, fit, gaps, minCap, minMet, minPositiveEps, maxPe, maxPe3, hideNA, hideNoApply, belowNcav, trackedOnly } = view;
  const profiles = new Set(view.profiles), sectors_ = new Set(view.sectors), venues = new Set(view.venues), idxSel = new Set(view.indexes);
  const { profiles: profileCounts, sectors: sectorCounts, venues: exchanges, indexes: indexCounts } = facets;
  const metCounts = Object.fromEntries([1,2,3,4,5,6].map((n) => [n, counts[`met_${n}`]]));
  const positiveEpsCounts = Object.fromEntries([5,6,7,9,10].map((n) => [n, counts[`eps_${n}`]]));
  const { unjudged: naCount, inapplicable: noApplyCount, net_net: netNetCount } = counts;
  const change = (key, value) => {
    const next = typeof value === "function" ? value(view[key]) : value;
    onChange({ [key]: next instanceof Set ? [...next] : next });
  };
  return (
      <div className="filters graham-controls">
        <select className="mincap lens" value={lens} onChange={(e) => change("lens", e.target.value)}
                title="Choose which Graham framework to use for the alignment view and ranking.">
          <option value="BOTH">Graham lens: Both</option>
          <option value="ENTERPRISING">Graham lens: Enterprising</option>
          <option value="DEFENSIVE">Graham lens: Defensive</option>
        </select>
        {lens === "ENTERPRISING" && <select className="mincap lens" value={gaps} onChange={(e) => change("gaps", e.target.value)}
          title="A research filter for operating companies only. It counts measured gaps in the six direct Enterprising tests.">
          <option value="ALL">Any Enterprising gaps</option>
          <option value="ALL_PASS">All 6 direct tests</option>
          <option value="ONE_GAP">One measured gap</option>
          <option value="TWO_GAPS">Two measured gaps</option>
          <option value="INCOMPLETE">Incomplete evidence</option>
        </select>}
        <select className="mincap lens" value={fit} onChange={(e) => change("fit", e.target.value)}
                title="Filter by the selected framework's alignment status. Under Both, a company is kept if either framework has that status.">
          <option value="ALL">Any alignment evidence</option>
          <option value="ALIGNED">Aligned only</option>
          <option value="EVIDENCE_INCOMPLETE">Evidence incomplete</option>
          <option value="BLOCKED">Blocked by a measured test</option>
        </select>
        <MultiSelect label="Applicability" options={profileCounts} selected={profiles} onChange={(value) => change("profiles", value)}
          formatName={(profile) => PROFILE_NAMES[profile] ?? profile}
          renderOption={(profile) => PROFILE_NAMES[profile] ?? profile} />
        <MultiSelect label="Sector" options={sectorCounts} selected={sectors_} onChange={(value) => change("sectors", value)} />
        <MultiSelect label="Exchange" options={exchanges} selected={venues} onChange={(value) => change("venues", value)} />
        <MultiSelect label="Index" options={indexCounts} selected={idxSel} onChange={(value) => change("indexes", value)} />
        <select className="mincap" value={minMet} onChange={(e) => change("minMet", Number(e.target.value))}>
          <option value={0}>Any criteria met</option>
          {[...Array(4)].map((_, i) => TOTAL_CRITERIA - i).map((n) => (
            <option key={n} value={n}>
              {n}/{TOTAL_CRITERIA} and above{metCounts[n] ? ` (${metCounts[n].toLocaleString()})` : ""}
            </option>
          ))}
        </select>
        <select className="mincap" value={minPositiveEps} onChange={(e) => change("minPositiveEps", Number(e.target.value))}
                title="Keeps companies with at least the chosen count of positive annual EPS observations in the completed FY(L-9)..FY(L) window. Missing years do not count as positive.">
          <option value={0}>Any 10Y positive EPS count</option>
          {EPS_POSITIVE_FLOORS.map((floor) => (
            <option key={floor} value={floor}>
              {floor}+ positive EPS years{positiveEpsCounts[floor] ? ` (${positiveEpsCounts[floor].toLocaleString()})` : ""}
            </option>
          ))}
        </select>
        <select className="mincap" value={maxPe} onChange={(e) => change("maxPe", Number(e.target.value))}
                title="Maximum positive trailing P/E. Companies with missing or non-positive earnings are excluded when a maximum is selected.">
          <option value={0}>Any P/E</option>
          {PE_MAXIMUMS.map((maximum) => (
            <option key={maximum} value={maximum}>P/E ≤ {maximum}</option>
          ))}
        </select>
        <select className="mincap" value={maxPe3} onChange={(e) => change("maxPe3", Number(e.target.value))}
                title="Maximum price divided by average EPS for the latest three completed fiscal years. Missing or non-positive averages are excluded.">
          <option value={0}>Any P/E3</option>
          {PE_MAXIMUMS.map((maximum) => (
            <option key={maximum} value={maximum}>P/E3 ≤ {maximum}</option>
          ))}
        </select>
        <select className="mincap" value={minCap} onChange={(e) => change("minCap", Number(e.target.value))}>
          <option value={0}>Any size</option>
          <option value={50e6}>$50M+</option>
          <option value={500e6}>$500M+</option>
          <option value={2e9}>$2B+</option>
          <option value={10e9}>$10B+</option>
        </select>
        <button className={`chip na ${hideNoApply ? "on" : ""}`}
                onClick={() => change("hideNoApply", (v) => !v)}
                title="Banks, insurers and REITs file no classified balance sheet, so criteria 2 and 3 ask for a figure their accounts never contain. No later filing will supply it, so these can never be graded on every criterion.">
          {hideNoApply ? "Applicable only" : "Hide not-applicable"} <span className="count">{noApplyCount}</span>
        </button>
        <button className={`chip na ${hideNA ? "on" : ""}`}
                onClick={() => change("hideNA", (v) => !v)}
                title="Hides any company where a criterion could not be decided — banks and homebuilders whose filings make criteria 2 and 3 meaningless, and companies missing the figures a test needs. What remains has been judged on every criterion.">
          {hideNA ? "Judged only" : `Judged on all ${TOTAL_CRITERIA}`} <span className="count">{naCount}</span>
        </button>
        <button className={`chip netnet ${belowNcav ? "on" : ""}`}
                onClick={() => change("belowNcav", (v) => !v)}
                title="Price at or below net current asset value per share — the liquid assets left after paying every liability. Graham's deepest-value test.">
          ≤ NCAV <span className="count">{netNetCount}</span>
        </button>
        {trackedCount != null && <button className={`chip star ${trackedOnly ? "on" : ""}`}
                onClick={() => change("trackedOnly", (v) => !v)}>
          ★ Tracked <span className="count">{trackedCount}</span>
        </button>}
        {activeFilters && <button className="chip clear" onClick={onClear}>clear all</button>}
        <span className="showing">
          showing {matched.toLocaleString()} of {total.toLocaleString()}
        </span>
        <span className="sort-help">Shift-click headers to add sort</span>
      </div>

  );
}
