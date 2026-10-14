// SQL semantics oracle: execute the existing App callbacks and imported helpers.
// No financial derivation or source verification is established by this check.
import fs from "node:fs";
import crypto from "node:crypto";
import { TOTAL_CRITERIA, byN, indexValuation, pe3, returnQualityPercentiles, valuationPrice } from "../../web/src/screen.js";
import { epsEvidence } from "../../web/src/earningsEvidence.js";
import { compareRows } from "../../web/src/sort.js";
import { payloadWarnings } from "../../web/src/warnings.js";

const [baselinePath, casesPath, appPath] = process.argv.slice(2);
const source = fs.readFileSync(appPath, "utf8");
function between(start, end) {
  if (source.split(start).length !== 2) throw new Error(`App callback marker changed: ${start}`);
  const tail = source.split(start)[1];
  if (!tail.includes(end)) throw new Error(`App callback end changed: ${end}`);
  return tail.split(end)[0];
}
const helperSource = between("const STOPWORDS =", "// narrowest first:");
const { matcher, enterprisingGap } = new Function(`${"const STOPWORDS ="}${helperSource}; return { matcher, enterprisingGap };`)();
const dividendSource = between("const dividendYield =", "export { drawdownTitle }");
const dividendYield = new Function("valuationPrice", `const dividendYield =${dividendSource}; return dividendYield;`)(valuationPrice);
const enrich = new Function("data", "returnQualityPercentiles", "valuationPrice", "epsEvidence", "pe3", "dividendYield", "payloadWarnings",
  between("  const rows = useMemo(() => {", "  }, [data]);"));
const data = JSON.parse(fs.readFileSync(baselinePath, "utf8"));
const rows = enrich(data, returnQualityPercentiles, valuationPrice, epsEvidence, pe3, dividendYield, payloadWarnings);
const viewSource = between("  const view = useMemo(() => {", "  }, [rows, q,");
const view = new Function("rows", "state", "matcher", "enterprisingGap", "byN", "compareRows", `
  const {q="",lens="BOTH",fit="ALL",gaps="ALL",min_cap:minCap=0,min_met:minMet=0,
    min_positive_eps:minPositiveEps=0,max_pe:maxPe=0,max_pe3:maxPe3=0,hide_na:hideNA=false,
    hide_no_apply:hideNoApply=false,below_ncav:belowNcav=false} = state;
  const profiles=new Set(state.profiles??[]), sectors_=new Set(state.sectors??[]),
    venues=new Set(state.venues??[]), idxSel=new Set(state.indexes??[]);
  const sort=(state.sorts??[["ticker",1]]).map(([key,dir])=>({key,dir}));
  const trackedOnly=false, tracked=new Set();
  ${viewSource}`);
const facets = {};
for (const [key, callback] of [["sectors", "sectorCounts"], ["profiles", "profileCounts"], ["venues", "exchanges"], ["indexes", "indexCounts"]]) {
  const body = between(`  const ${callback} = useMemo(() => {`, "  }, [rows]);");
  facets[key] = new Function("rows", "INDEX_RANK", body)(rows, {DJIA:0,"S&P 500":1,"Nasdaq 100":2,"Nasdaq Comp":3});
}
const metCounts = new Function("view", between("  const metCounts = useMemo(() => {", "  }, [view]);"));
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));
const results = cases.map(({name, query}) => {
  const selected = view([...rows], query, matcher, enterprisingGap, byN, compareRows);
  const met = metCounts(selected);
  const counts = {matched:selected.length,unjudged:selected.filter(r=>r.unjudged).length,
    inapplicable:selected.filter(r=>r.inapplicable).length,net_net:selected.filter(r=>r.netNet).length};
  counts.judged=counts.matched-counts.unjudged;
  for(const floor of [5,6,7,9,10]) counts[`eps_${floor}`]=selected.filter(r=>r.eps10?.positive>=floor).length;
  for(let n=1;n<=TOTAL_CRITERIA;n++) counts[`met_${n}`]=met[n]??0;
  const match=matcher(query.q??"");
  return {name,tickers:selected.map(r=>r.ticker),counts,matches_anywhere:match?match(rows).length:0};
});
process.stdout.write(JSON.stringify({rows:rows.length,engine:data.engine_version,
  app_sha256:crypto.createHash("sha256").update(source).digest("hex"),locale:Intl.DateTimeFormat().resolvedOptions().locale,
  facets,overview:indexValuation(rows,"S&P 500"),cases:results}));
