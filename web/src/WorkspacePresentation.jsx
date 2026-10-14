import React from "react";
import { workspaceColumns } from "./workspaceSession.js";

const labels = { ticker: "Ticker", name: "Company", sector: "Sector", eps10: "10Y EPS evidence", returns: "Return quality",
  mcap: "Market cap", price: "Price", offhigh: "Off high", pe: "P/E", pe3: "P/E 3Y", dividend: "Dividend yield" };

export default function WorkspacePresentation({ state, session }) {
  const draft = state.draft;
  const busy = !!state.inFlight || state.phase === "loading";
  const writable = state.phase === "ready" && !!state.saved && !state.inFlight && !state.uncertain && !state.needsRefresh && !state.conflict;
  const toggle = (id) => session.setColumns(draft.columns.includes(id) ? draft.columns.filter((value) => value !== id) : [...draft.columns, id]);
  const move = (index, offset) => {
    const columns = [...draft.columns];
    [columns[index], columns[index+offset]] = [columns[index+offset], columns[index]];
    session.setColumns(columns);
  };
  return <section aria-label="Private presentation">
    <h2>Saved view and columns</h2>
    {state.phase === "loading" && <p role="status">Loading saved presentation…</p>}
    {state.error && <p role="alert">{state.error}</p>}
    {state.phase === "error" && !state.uncertain && !state.needsRefresh && !state.conflict
      && <button type="button" disabled={busy} onClick={() => session.refresh()}>Retry saved view load</button>}
    {state.phase === "denied" && <p>Private presentation requires a verified account. The shared preview remains available.</p>}
    {state.inFlight && <p role="status">{state.inFlight.kind === "restore" ? "Restoring selected revision…" : "Saving presentation…"}</p>}
    {state.uncertain && <p role="alert">Delivery is uncertain. Retry the same request to check its result.
      {" "}<button type="button" disabled={busy} onClick={() => session.retry()}>Retry same request</button></p>}
    {state.needsRefresh && !state.inFlight && <p role="alert">The edit was accepted. Current saved state still needs checking.
      {" "}<button type="button" disabled={busy} onClick={() => session.retry()}>Check current saved state</button></p>}
    {state.conflict && <p role="alert">{state.conflict.message} {state.conflict.currentRevision != null && <>Current revision {state.conflict.currentRevision}. </>}
      <button type="button" disabled={busy || !!state.uncertain} onClick={() => session.reload()}>Load latest saved view (discard draft)</button></p>}
    {draft && <>
      <p>Saved revision {state.saved.edit_revision}{state.saved.restored_from_revision != null && <> · restored from revision {state.saved.restored_from_revision}</>}
        {" · "}{state.dirty ? "Unsaved changes" : "No unsaved changes"}</p>
      <button type="button" disabled={!writable} onClick={() => session.importBrowserView(state.contextGeneration)}>Import this browser's saved view</button>
      <p>Import replaces your draft filters and sort. It keeps your columns and does not save changes.</p>
      {state.browserImport && <p role={state.browserImport.status === "error" ? "alert" : "status"}>{state.browserImport.message}</p>}
      {state.applicationBlocked && <p role="alert">Tracked-only filtering is not available in this screen. Your choice is kept; results are blocked.
        {" "}<button type="button" onClick={() => session.patchView({ trackedOnly: false })}>Turn off tracked-only filtering</button></p>}
      <fieldset><legend>Visible columns</legend>
        {workspaceColumns.map((id) => <label key={id}><input type="checkbox" checked={draft.columns.includes(id)} onChange={() => toggle(id)} />{labels[id]} </label>)}
      </fieldset>
      <ol aria-label="Column order">{draft.columns.map((id, index) => <li key={id}>{labels[id]} {" "}
        <button type="button" aria-label={`Move ${labels[id]} up`} disabled={index === 0} onClick={() => move(index, -1)}>Up</button>{" "}
        <button type="button" aria-label={`Move ${labels[id]} down`} disabled={index === draft.columns.length-1} onClick={() => move(index, 1)}>Down</button>
      </li>)}</ol>
      {draft.columns.length === 0 && <p>No visible columns. Select columns above to show them.</p>}
      <button type="button" disabled={!writable || !state.dirty} onClick={() => session.save()}>Save view</button>{" "}
      <button type="button" disabled={!state.saved || state.history.phase === "loading"} onClick={() => session.history()}>Inspect revisions</button>
    </>}
    {state.history.phase === "loading" && <p role="status">Loading revision page…</p>}
    {state.history.error && <p role="alert">{state.history.error}</p>}
    {state.history.revisions.length > 0 && <div>
      <ul aria-label="Presentation revisions">{state.history.revisions.map((row) => <li key={row.edit_revision}>
        <button type="button" onClick={() => session.selectRevision(row.edit_revision)}>Preview revision {row.edit_revision}</button>
        {" · "}{row.actor} · {row.created_at}
        {row.restored_from_revision != null && <> · restored from revision {row.restored_from_revision}</>}
      </li>)}</ul>
      {state.history.nextBefore != null && <button type="button" disabled={state.history.phase === "loading"} onClick={() => session.history(state.history.nextBefore)}>Older revisions</button>}
      {state.history.selected && <div aria-label={`Revision ${state.history.selected.edit_revision} preview`}>
        <h3>Revision {state.history.selected.edit_revision}</h3>
        <pre>{JSON.stringify(state.history.selected.configuration, null, 2)}</pre>
        <p>Restore replaces the whole saved view and columns. It creates a new revision. Current financial data stays live.</p>
        {state.dirty && <p>The selected revision will replace your draft unless you edit it while the request is pending.</p>}
        <button type="button" disabled={!writable} onClick={() => session.restore(state.history.selected.edit_revision)}>Restore revision {state.history.selected.edit_revision}</button>
      </div>}
    </div>}
  </section>;
}
