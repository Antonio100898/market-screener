import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { transformWithEsbuild } from "vite";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { configuration, revision } from "./workspaceFixtures.mjs";

const source = await readFile(new URL("../src/WorkspacePresentation.jsx", import.meta.url), "utf8");
const compiled = await transformWithEsbuild(source, "WorkspacePresentation.jsx", { jsx: "transform" });
const code = compiled.code.replace('"react"', JSON.stringify(import.meta.resolve("react")))
  .replace('"./workspaceSession.js"', JSON.stringify(new URL("../src/workspaceSession.js", import.meta.url).href));
const { default: Presentation } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
const session = Object.fromEntries(["save", "restore", "retry", "reload", "refresh", "history", "selectRevision", "setColumns", "patchView"].map((name) => [name, () => {}]));
const state = () => ({ phase: "ready", saved: revision(), current: revision(), draft: configuration(), dirty: true,
  inFlight: null, uncertain: null, needsRefresh: false, conflict: null, error: null, applicationBlocked: false,
  history: { phase: "ready", revisions: [], nextBefore: null, selected: null, error: null } });
const render = (value) => renderToStaticMarkup(createElement(Presentation, { state: value, session }));

test("manual controls render selection/order/empty columns and typed revision preview without formula or auth claims", () => {
  const value = state(); value.draft.columns = []; value.history = { ...value.history, revisions: [revision(2, configuration(), 1)], selected: revision(2, configuration(), 1), nextBefore: 2 };
  const html = render(value);
  assert.match(html, /Visible columns/); assert.equal((html.match(/type="checkbox"/g) ?? []).length, 11);
  assert.match(html, /No visible columns/); assert.match(html, /Save view/); assert.match(html, /Inspect revisions/);
  assert.match(html, /Preview revision 2/); assert.match(html, /Restore revision 2/); assert.match(html, /restored from revision 1/);
  assert.match(html, /Older revisions/); assert.match(html, /Current financial data stays live/);
  assert.match(html, /Unseen sector/); assert.doesNotMatch(html, /API key|signup complete|formula function/);
});

test("pending/denied/conflict/uncertain/tracked states expose honest accessible actions", () => {
  const pending = state(); pending.inFlight = { kind: "restore", key: "k" };
  assert.match(render(pending), /role="status">Restoring/); assert.match(render(pending), /disabled="">Save view/);
  const denied = state(); Object.assign(denied, { phase: "denied", saved: null, current: null, draft: null, error: "Verified sign-in is required." });
  assert.match(render(denied), /role="alert"/); assert.doesNotMatch(render(denied), /type="checkbox"/);
  const conflict = state(); conflict.conflict = { currentRevision: 5, message: "Draft retained" };
  assert.match(render(conflict), /Load latest saved view \(discard draft\)/); assert.match(render(conflict), /disabled="">Save view/);
  const uncertain = state(); uncertain.uncertain = { key: "same", kind: "save" };
  assert.match(render(uncertain), /Retry same request/);
  const tracked = state(); tracked.applicationBlocked = true; tracked.draft.view.trackedOnly = true;
  assert.match(render(tracked), /results are blocked/); assert.match(render(tracked), /Turn off tracked-only filtering/);
  assert.match(render(state()), /aria-label="Move Price up"/);
});

test("explicit browser import is reachable only in writable state with an honest draft-only notice", () => {
  assert.match(render(state()), /Import this browser&#x27;s saved view/);
  assert.doesNotMatch(render(state()), /disabled="">Import this browser/);
  assert.match(render(state()), /keeps your columns and does not save changes/);
  for (const patch of [{ phase: "loading" }, { inFlight: { kind: "save" } }, { uncertain: { kind: "save" } },
    { needsRefresh: true }, { conflict: { currentRevision: 4, message: "conflict" } }]) {
    assert.match(render({ ...state(), ...patch }), /disabled="">Import this browser/);
  }
  const error = state(); error.browserImport = { status: "error", message: "No saved browser view was found." };
  assert.match(render(error), /role="alert">No saved browser view was found/);
});
