import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import WorkspacePresentation from "../../src/WorkspacePresentation.jsx";
import { createWorkspaceSession, WorkspaceError } from "../../src/workspaceSession.js";
import { configuration, revision } from "../workspaceFixtures.mjs";

window.proof = { calls: [], reads: 0, applied: [], columns: [], stale: null };
let current = revision(), denied = false;
const session = createWorkspaceSession((state) => window.dispatchEvent(new CustomEvent("fixture-state", { detail: state })), {
  request: async (path, options = {}) => {
    window.proof.calls.push({ path, method: options.method ?? "GET", body: options.body });
    if (denied) throw new WorkspaceError("Verified sign-in is required.", 401);
    if (options.method === "PUT") { const body = JSON.parse(options.body); current = revision(body.expected_revision + 1, body.configuration); }
    return structuredClone(current);
  },
  newKey: () => "fixture-import-manual-save",
  onApply: (view) => window.proof.applied.push(view), onColumns: (columns) => window.proof.columns.push(columns),
});
window.session = session;
window.setDenied = (value) => { denied = value; };
window.resetFixture = async () => { current = revision(); denied = false; await session.open(); };
window.captureStaleImport = () => { const generation = session.getState().contextGeneration; window.proof.stale = () => session.importBrowserView(generation); };
const read = Storage.prototype.getItem;
Storage.prototype.getItem = function(key) { if (key === "screener-view") window.proof.reads++; return read.call(this, key); };

function Fixture() {
  const [state, setState] = useState(session.getState());
  useEffect(() => {
    const changed = (event) => setState(event.detail); window.addEventListener("fixture-state", changed);
    session.open(); return () => { window.removeEventListener("fixture-state", changed); session.close(); };
  }, []);
  return <><h1>LOCAL DIRECT CONTROL FIXTURE ONLY</h1>
    <p>In-memory presentation transport. No Next, FastAPI, production account or financial readiness proof.</p>
    <WorkspacePresentation state={state} session={session} />
    <pre aria-label="Fixture draft">{JSON.stringify(state.draft)}</pre></>;
}
createRoot(document.getElementById("root")).render(<Fixture />);
