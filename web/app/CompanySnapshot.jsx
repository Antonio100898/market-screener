import FigureHistory from "../src/FigureHistory.jsx";
import { figureText } from "./publicData.js";
import { filingUrl } from "../src/sourceLink.js";

const fields = [
  ["total_assets", "Total assets"], ["total_liabilities", "Total liabilities"], ["current_assets", "Current assets"],
  ["current_liabilities", "Current liabilities"], ["total_debt", "Reported total debt"],
  ["long_term_debt", "Long-term debt"], ["short_term_debt", "Short-term debt"],
  ["cash", "Cash", "asset_quality"], ["shares", "Shares outstanding"],
];

function Source({ cik, source }) {
  if (!source) return <>Source unavailable</>;
  const url = filingUrl(cik, source.accn);
  return <>
    <div><code>{source.tag || "Source tag unavailable"}</code></div>
    <div>{url ? <a href={url}>{source.form || "Filing"} · {source.accn}</a>
      : <>{source.form || "Form unavailable"} · {source.accn || "Accession unavailable"}</>}</div>
    <div>Period end: {source.end || "Unavailable"} · Unit: {source.unit || "Unavailable"}</div>
    {source.document && <div>Document/source row: {source.document}</div>}
    {source.concept && <div>Scope: {source.concept}</div>}
    {(source.components || []).map((part, index) => <div key={index}>Component: <Source cik={cik} source={part} /></div>)}
  </>;
}

export default function CompanySnapshot({ result }) {
  const { row, release } = result;
  return <>
    <h1>{row.name || row.ticker} ({row.ticker})</h1>
    <p className="notice">Published snapshot. These figures have not been verified against later source changes.</p>
    <p>Data release {release.release_id}. Published {release.published_at || "date unavailable"}.<br />
      Statement currency: {row.reporting_currency || row.currency || "Unavailable"}. Values remain in their reported units.</p>
    <h2>Reported figures and filing sources</h2>
    <div className="table-scroll"><table><thead><tr><th>Figure</th><th>Value</th><th>Source</th></tr></thead><tbody>
      {fields.map(([key, label, parent]) => <tr key={key}><th scope="row">{label}</th>
        <td>{figureText(parent ? row[parent]?.[key] : row[key])}</td><td><Source cik={row.cik} source={row.sources?.[key]} /></td></tr>)}
    </tbody></table></div>
    <h2>Stored screening results</h2>
    <p>Verdict: {row.verdict || "Unavailable"}. These results use the strict stored evidence; no zero assumption is applied here.</p>
    <p>Debt used by the stored screen: {figureText(row.debt)}. This is a calculated amount; the reported debt components and their sources are shown above.</p>
    <ul>{(row.criteria || []).map((criterion) => <li key={criterion.n}>
      Criterion {criterion.n}: {criterion.status || "Unavailable"}{criterion.note ? ` — ${criterion.note}` : ""}
    </li>)}</ul>
    {(row.context_notes || []).length > 0 && <section><h2>Disclosed notes</h2><ul>
      {row.context_notes.map((note, index) => <li key={index}>{note.text ?? note}</li>)}
    </ul></section>}
    {(row.assumptions || []).length > 0 && <section><h2>Stored calculation assumptions</h2><ul>
      {row.assumptions.map((note, index) => <li key={index}>{note}</li>)}
    </ul></section>}
    <FigureHistory context={{ security_id: result.security_id, release_id: release.release_id }} servedView="STRICT" />
    <footer><a href="/guides/read-a-figure-source">How to read a figure's source</a><br />
      <a href="/dashboard">Open the research dashboard</a></footer>
  </>;
}
