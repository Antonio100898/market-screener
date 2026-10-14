import { useCallback, useEffect, useRef, useState } from "react";
import CompanyTable from "./CompanyTable.jsx";
import DashboardFilters from "./DashboardFilters.jsx";
import MarketReference from "./MarketReference.jsx";
import Detail from "./Detail.jsx";
import IntrinsicValueTool from "./IntrinsicValueTool.jsx";
import { hasActiveFilters } from "./view.js";
import { updateSort } from "./sort.js";
import { payloadWarnings } from "./warnings.js";
import { createQuerySession, emptyQueryState, initialQueryView, readPinnedDetail } from "./querySession.js";
import WorkspacePresentation from "./WorkspacePresentation.jsx";
import { createWorkspaceSession } from "./workspaceSession.js";

function PinnedDetail({ selected, release, onClose, onRestart }) {
  const [row, setRow] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const load = useCallback(async (base, signal, assumed = true) => {
    try { return await readPinnedDetail(base, release, assumed, signal); }
    catch (error) { if (error.status === 409 && !signal?.aborted) onRestart(); throw error; }
  }, [release, onRestart]);
  useEffect(() => {
    const controller = new AbortController();
    setRow(null); setError(null);
    load(selected, controller.signal, false).then((next) => {
      if (!controller.signal.aborted) setRow(next);
    }).catch((error) => {
      if (!controller.signal.aborted) setError(error.message);
    });
    return () => controller.abort();
  }, [selected, load, attempt]);
  if (row) return <Detail row={row} onClose={onClose} loadAssumedRow={load} historyContext={{ security_id: selected.security_id, release_id: release }} />;
  return <><div className="scrim" onClick={onClose} /><aside className="detail clean-detail">
    <button className="close-btn" onClick={onClose} aria-label="Close company details">×</button>
    <h2>{selected.ticker}</h2>
    {error ? <p className="msg err" role="alert">{error} <button onClick={() => setAttempt((value) => value + 1)}>Retry</button></p>
      : <p role="status">Loading company evidence…</p>}
  </aside></>;
}

export default function ApiDashboard({ accountContext = null }) {
  const [view, setView] = useState(initialQueryView);
  const [state, setState] = useState(emptyQueryState);
  const [session] = useState(() => createQuerySession(setState));
  const [selected, setSelected] = useState(null);
  const [columns, setColumns] = useState(undefined);
  const [presentation, setPresentation] = useState(null);
  const workspaceRef = useRef(null), admitted = useRef(false), blocked = useRef(false);
  const sessionContext = useRef(accountContext), requestedContext = useRef(accountContext);
  requestedContext.current = accountContext;
  const contextCurrent = sessionContext.current === accountContext;
  const viewRef = useRef(view);
  const applyView = (next) => { viewRef.current = next; setView(next); };
  const [workspace] = useState(() => createWorkspaceSession((next) => {
    admitted.current = !!next.draft;
    blocked.current = next.applicationBlocked;
    setPresentation(next);
  }, {
    onApply: applyView,
    onColumns: setColumns,
    onClear: () => {
      const draft = workspaceRef.current?.getState().draft;
      if (!draft && !admitted.current) return;
      blocked.current = true;
      session.cancel(); setSelected(null); setState({ ...emptyQueryState(), phase: "idle" });
      applyView(draft ? draft.view : initialQueryView());
      if (!draft) setColumns(undefined);
    },
  }));
  workspaceRef.current = workspace;
  const applicationBlocked = !contextCurrent || (presentation?.applicationBlocked ?? false);
  const visibleView = contextCurrent ? view : initialQueryView();
  const restart = useCallback(() => {
    if (blocked.current || sessionContext.current !== requestedContext.current) return;
    setSelected(null); session.start(viewRef.current);
  }, [session]);
  useEffect(() => {
    sessionContext.current = accountContext;
    workspace.open();
    return () => { workspace.close(); session.cancel(); };
  }, [workspace, session, accountContext]);
  useEffect(() => {
    if (contextCurrent && !blocked.current && view === viewRef.current) session.start(view);
    return session.cancel;
  }, [session, view, applicationBlocked, accountContext]);
  useEffect(() => { setSelected(null); }, [state.generation]);
  useEffect(() => {
    const check = () => {
      if (!blocked.current && sessionContext.current === requestedContext.current && document.visibilityState !== "hidden") session.check();
    };
    window.addEventListener("focus", check);
    window.addEventListener("online", check);
    document.addEventListener("visibilitychange", check);
    const timer = window.setInterval(check, 60000);
    return () => {
      window.removeEventListener("focus", check);
      window.removeEventListener("online", check);
      document.removeEventListener("visibilitychange", check);
      window.clearInterval(timer);
    };
  }, [session]);
  const changeView = (patch) => {
    if (sessionContext.current !== requestedContext.current) return;
    const draft = workspace.getState().draft;
    if (draft) workspace.patchView(typeof patch === "function" ? patch(draft.view) : patch);
    else applyView({ ...viewRef.current, ...(typeof patch === "function" ? patch(viewRef.current) : patch) });
  };
  const clear = () => changeView((current) => ({ ...initialQueryView(), q: current.q, sort: current.sort }));
  const rows = state.rows.map((row) => ({ ...row, warnings: payloadWarnings(row) }));
  return <><div className="app">
    <header className="app-header"><div>
      <h1>Market Screener</h1><p className="sub">Shared screening preview</p>
      <nav className="app-nav"><a href="/dashboard">Current dashboard and portfolios</a><a href="/">Public pages</a></nav>
    </div><div className="searchwrap">
      <input className="search" aria-label="Search companies" placeholder="Search…" maxLength={1000}
        value={visibleView.q} onChange={(event) => changeView({ q: event.target.value })} />
    </div></header>
    <p className="sub">Snapshot validity is unverified. Tracking and portfolios are available in the current dashboard.</p>
    {contextCurrent && presentation && <WorkspacePresentation state={presentation} session={workspace} />}
    {!applicationBlocked && state.phase === "loading" && <p className="msg" role="status">Loading published screening data…</p>}
    {!applicationBlocked && state.phase === "error" && <p className="msg err" role="alert">{state.error} <button onClick={restart}>Retry</button></p>}
    {!applicationBlocked && state.phase === "ready" && state.view === view && <>
      <p className="sub">Data release {state.release.release_id} · published {state.release.published_at}
        {" · "}{state.release.total.toLocaleString()} companies · engine {state.release.engine_revisions.join(", ")}
        {" · "}<button className="linkish" onClick={restart}>Refresh snapshot</button></p>
      <MarketReference overview={state.overview} />
      <DashboardFilters view={view} onChange={changeView}
        facets={state.facets} counts={state.counts} total={state.release.total} matched={state.matched}
        activeFilters={hasActiveFilters(view)} onClear={clear} />
      <CompanyTable rows={rows} sort={view.sort} columns={columns}
        onSort={(key, additive) => changeView((current) => ({ sort: updateSort(current.sort, key, additive) }))}
        onSelect={setSelected} />
      <p className="sub">Loaded {rows.length.toLocaleString()} of {state.matched.toLocaleString()} matching companies.</p>
      {state.error && <p className="msg err" role="alert">{state.error}</p>}
      {state.cursor && <div className="load-more"><button className="linkish" disabled={state.moreLoading} onClick={() => {
        if (!blocked.current && sessionContext.current === requestedContext.current) session.more();
      }}>
        {state.moreLoading ? "Loading…" : "Load more"}</button></div>}
      {state.matched === 0 && <p className="msg">{state.matchesAnywhere > 0
        ? <>{state.matchesAnywhere.toLocaleString()} companies match outside these filters. <button className="linkish" onClick={clear}>Search everything</button></>
        : view.q.trim() ? `Nothing matches “${view.q.trim()}”.` : "No companies match these filters."}</p>}
      {selected && <PinnedDetail key={`${state.release.release_id}:${selected.security_id}`} selected={selected}
        release={state.release.release_id} onClose={() => setSelected(null)} onRestart={restart} />}
    </>}
  </div><IntrinsicValueTool /></>;
}
