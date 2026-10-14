import { useCallback, useEffect, useRef, useState } from "react";
import { createHistorySession, historyViews, publicHistoryUrl } from "./figureHistory.js";

function useHistory(context, operation, params, identity) {
  const [state, setState] = useState({ rows: [] });
  const [session] = useState(() => createHistorySession(setState));
  useEffect(() => {
    if (context && operation) session.start(context, operation, params);
    return session.cancel;
  }, [session, context?.security_id, context?.release_id, operation, identity]);
  return [state, session];
}
function Records({ value, depth = 0 }) {
  if (value == null) return <span>Unavailable</span>;
  if (depth > 16) return <span>Further detail unavailable</span>;
  if (Array.isArray(value)) return value.length ? <ul>{value.map((item, i) => <li key={i}><Records value={item} depth={depth + 1} /></li>)}</ul> : <span>None captured</span>;
  if (typeof value === "object") return <dl>{Object.entries(value).filter(([key]) => !["supporting_records", "selector_handle"].includes(key)).map(([key, item]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd><Records value={item} depth={depth + 1} /></dd></div>)}</dl>;
  const url = publicHistoryUrl(value);
  return url ? <a href={url} target="_blank" rel="noreferrer">{value}</a> : <span>{String(value)}</span>;
}
function Paging({ state, session }) {
  return <>{state.loading && <p role="status">Loading captured records…</p>}
    {state.error && <p role="alert">{state.error}</p>}
    {state.page?.next_cursor && <button disabled={state.loading || state.restartJournal} onClick={session.more}>Load older / more records</button>}</>;
}
function HandleButtons({ handles, onOpen }) {
  return handles && <div className="history-actions">{Object.entries(handles).map(([kind, handle]) => <button key={kind} onClick={() => onOpen({ kind, handle })}>{kind}</button>)}</div>;
}
function Support({ context, target, onOpen, onJournal }) {
  const operation = target.kind === "source-events" ? "source-events" : "support";
  const [attempt, retry] = useState(0);
  const params = target.kind === "source-events" ? { source_item_id: target.source_item_id } : target;
  const [state, session] = useHistory(context, operation, params, `${target.handle ?? target.source_item_id}:${attempt}`);
  return <section aria-label="Supporting records"><h4>{target.kind}</h4>
    {state.page && <><p>{state.page.supporting_status ?? state.page.journal_coverage}. Direct captured records only.</p><Records value={state.page.binding ?? { selected_reason: state.page.selected_reason }} /></>}
    {state.rows.map((item, i) => <article key={i}><Records value={item} /><HandleButtons handles={item.supporting_records} onOpen={onOpen} />
      {target.kind === "sources" && Number.isSafeInteger(item.source_item_id) && <button onClick={() => onJournal(item.source_item_id)}>Source checks</button>}</article>)}
    {state.page && !state.rows.length && <p>No qualified records. Supporting evidence may be unavailable.</p>}
    <Paging state={state} session={session} />
    {state.error && <button onClick={() => retry((n) => n + 1)}>{state.restartJournal ? "Restart this source journal" : "Retry these records"}</button>}
  </section>;
}
function Inspector({ context, figure, onClose }) {
  const [operation, setOperation] = useState("timeline");
  const [trail, setTrail] = useState([]);
  const close = useRef();
  const dialog = useRef();
  const [attempt, retry] = useState(0);
  const [state, session] = useHistory(context, operation, { handle: figure.selector_handle, selector: figure.selector }, `${figure.selector_handle}:${attempt}`);
  useEffect(() => {
    close.current?.focus();
    const esc = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); onClose(); }
      if (event.key === "Tab") {
        const items = [...dialog.current.querySelectorAll("button:not(:disabled),a[href],select")];
        const first = items[0], last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    window.addEventListener("keydown", esc, true);
    return () => window.removeEventListener("keydown", esc, true);
  }, [onClose]);
  const open = (target) => setTrail((old) => [...old, target]);
  return <div ref={dialog} className="figure-history-dialog" role="dialog" aria-modal="true" aria-labelledby="history-title">
    <button ref={close} onClick={onClose}>Close history</button><h3 id="history-title">Captured figure revisions</h3>
    <p>Release {context.release_id}. This history does not verify current official financial values or complete earlier history.</p>
    <Records value={figure.selector} />
    <p>Exact values are shown as captured text. Delta is not computed; the cause and native scope may be unknown.</p>
    <div className="history-actions"><button aria-pressed={operation === "timeline"} onClick={() => { setTrail([]); setOperation("timeline"); }}>Changes</button>
      <button aria-pressed={operation === "activations"} onClick={() => { setTrail([]); setOperation("activations"); }}>Activations</button></div>
    {state.page && <><Records value={{ coverage: state.page.coverage, endpoint: state.page.endpoint }} /><HandleButtons handles={state.page.endpoint.supporting_records} onOpen={open} />
      <p>Scanned {state.page.scanned_releases} releases. {state.page.traversal_complete ? "This captured chain ended." : "Older releases may remain."}</p></>}
    {state.rows.map((item, i) => <article key={`${item.release_id}:${i}`}><Records value={item} />
      {[item.previous, item.replacement, item.endpoint].filter(Boolean).map((endpoint, j) => <div key={j}><p>Evidence for release {endpoint.release_id}</p><HandleButtons handles={endpoint.supporting_records} onOpen={open} /></div>)}
    </article>)}
    {state.page && !state.rows.length && <p>No changes in this scanned window. This does not prove a complete or unchanged financial history.</p>}
    <Paging state={state} session={session} />
    {state.error && <button onClick={() => retry((n) => n + 1)}>Retry captured history</button>}
    {trail.length > 0 && <><button onClick={() => setTrail((old) => old.slice(0, -1))}>Back to previous supporting record</button>
      <Support key={trail.length + ":" + (trail.at(-1).handle ?? trail.at(-1).source_item_id)} context={context} target={trail.at(-1)} onOpen={open}
        onJournal={(source_item_id) => open({ kind: "source-events", source_item_id })} /></>}
  </div>;
}
export default function FigureHistory({ context, servedView = "UNKNOWN" }) {
  const [view, setView] = useState("STRICT");
  const [figure, setFigure] = useState(null);
  const opener = useRef();
  const [attempt, retry] = useState(0);
  const [state, session] = useHistory(context, "catalog", { view }, `${view}:${servedView}:${attempt}`);
  useEffect(() => { setFigure(null); }, [context?.security_id, context?.release_id, servedView, view]);
  const close = useCallback(() => { setFigure(null); opener.current?.focus(); }, []);
  if (!context) return <section className="captured-history"><h3>Figure revisions</h3><p>History unavailable: this displayed snapshot has no verified published figure binding.</p></section>;
  return <section className="captured-history"><h3>Captured figures and revisions</h3>
    <p>Existing detail figures have no captured history binding. This separate inventory lists published graph outputs; its views are not verified associations with the displayed {servedView.toLowerCase()} values.</p>
    <label>Captured graph view <select value={view} onChange={(event) => setView(event.target.value)}>{historyViews.map((item) => <option key={item}>{item}</option>)}</select></label>
    {state.page && <p>Release {context.release_id} · {state.page.catalog_status}. Earlier history and native financial scope are incomplete.</p>}
    {state.rows.map((item, i) => <article key={item.selector_handle}><h4>{item.selector.metric_path ?? "Figure identifier unavailable"}</h4><Records value={item.endpoint.selection} /><Records value={item.coverage} />
      <button onClick={(event) => { opener.current = event.currentTarget; setFigure(item); }} aria-haspopup="dialog">Inspect captured history</button></article>)}
    {state.page && !state.rows.length && <p>No captured outputs are available for this view. Missing evidence is not zero.</p>}
    <Paging state={state} session={session} />
    {state.error && <button onClick={() => retry((n) => n + 1)}>Retry captured catalog</button>}
    {figure && <Inspector key={figure.selector_handle} context={context} figure={figure} onClose={close} />}
  </section>;
}
