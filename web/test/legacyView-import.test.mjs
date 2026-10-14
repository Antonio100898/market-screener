import assert from "node:assert/strict";
import test from "node:test";
import { legacyViewSort, readSavedBrowserView, unfilteredView } from "../src/view.js";
import { createWorkspaceSession, WorkspaceError } from "../src/workspaceSession.js";
import { configuration, revision, deferred } from "./workspaceFixtures.mjs";

const storage = (raw) => ({ getItem(key) { assert.equal(key, "screener-view"); return raw; } });
function setup(raw, requestOverride) {
  const calls = [], views = [], columns = []; let reads = 0, current = revision(), key = 0;
  const request = async (path, options = {}) => {
    calls.push({ path, ...options });
    if (requestOverride) return requestOverride(path, options);
    if (options.method === "PUT") { const body = JSON.parse(options.body); current = revision(body.expected_revision + 1, body.configuration); }
    return structuredClone(current);
  };
  const session = createWorkspaceSession(() => {}, { request, newKey: () => `same-key-${++key}`,
    readBrowserView: () => { reads++; return readSavedBrowserView(storage(raw)); },
    onApply: (view) => views.push(view), onColumns: (value) => columns.push(value) });
  return { session, calls, views, columns, reads: () => reads };
}

test("legacy object/array aliases use actual legacy filter and fallback, with ordered priorities", () => {
  assert.deepEqual(legacyViewSort({ key: "grade", dir: -1 }), [{ key: "fit", dir: 1 }]);
  assert.deepEqual(legacyViewSort([{ key: "ptbv", dir: -1 }, { key: "ticker", dir: -1 }, { key: "price", dir: 1 }]), [{ key: "ticker", dir: -1 }, { key: "price", dir: 1 }]);
  assert.deepEqual(legacyViewSort({ key: "n_pass", dir: -1 }), [{ key: "fit", dir: 1 }]);
  assert.deepEqual(readSavedBrowserView(storage("{}")), { view: { q: "", ...unfilteredView(), sort: [{ key: "fit", dir: 1 }] }, excludedScroll: false });
});

test("explicit import retains every supported view field and columns, excludes scroll, never writes source or server", async () => {
  const raw = JSON.stringify({ q: "unseen company", gaps: "TWO_GAPS", lens: "DEFENSIVE", fit: "BLOCKED", profiles: ["Unseen profile"],
    sectors: ["Unseen sector"], venues: ["Unseen venue"], indexes: ["Unseen index"], minCap: 100, minMet: 6,
    minPositiveEps: 10, maxPe: 22.5, maxPe3: 17, trackedOnly: false, hideNA: true, hideNoApply: true, belowNcav: true,
    sort: [{ key: "price", dir: -1 }, { key: "ticker", dir: 1 }], scroll: 700 });
  const env = setup(raw); await env.session.open();
  assert.equal(env.reads(), 0);
  const before = env.session.getState();
  assert.equal(env.session.importBrowserView(before.contextGeneration), true);
  const after = env.session.getState();
  const expected = JSON.parse(raw); delete expected.scroll;
  assert.deepEqual(after.draft.view, expected); assert.deepEqual(after.draft.columns, before.draft.columns);
  assert.deepEqual(after.saved, before.saved); assert.equal(after.dirty, true); assert.match(after.browserImport.message, /Scroll position stays/);
  assert.equal(env.calls.length, 1); assert.equal(env.columns.length, 1); assert.equal(raw, JSON.stringify(JSON.parse(raw)));
  assert.equal(await env.session.save(), true);
  const mutation = env.calls.find((call) => call.method === "PUT"), body = JSON.parse(mutation.body);
  assert.equal(body.expected_revision, before.saved.edit_revision); assert.equal(body.request_key, "same-key-1");
  assert.deepEqual(body.configuration, after.draft); assert.equal(env.session.getState().saved.edit_revision, 2);
});

for (const floor of [7, 8]) test(`supported old criterion floor ${floor} imports as six and uses ordinary manual Save`, async () => {
  const raw = JSON.stringify({ minMet: floor, q: "old browser", scroll: 22 });
  const env = setup(raw); await env.session.open(); const before = env.session.getState();
  assert.equal(env.session.importBrowserView(before.contextGeneration), true);
  const imported = env.session.getState();
  assert.equal(imported.draft.view.minMet, 6); assert.equal(imported.draft.view.q, "old browser");
  assert.deepEqual(imported.draft.columns, before.draft.columns); assert.deepEqual(imported.saved, before.saved);
  assert.equal(env.calls.length, 1); assert.equal(env.reads(), 1); assert.match(imported.browserImport.message, /Scroll position stays/);
  assert.equal(await env.session.save(), true);
  const writes = env.calls.filter((call) => call.method === "PUT"); assert.equal(writes.length, 1);
  const body = JSON.parse(writes[0].body);
  assert.deepEqual(body.configuration, imported.draft); assert.equal(body.expected_revision, before.saved.edit_revision);
  assert.equal(body.request_key, "same-key-1"); assert.equal(env.session.getState().saved.configuration.view.minMet, 6);
  assert.equal(readSavedBrowserView(storage(raw)).view.minMet, 6); assert.equal(JSON.parse(raw).minMet, floor);
});

const rejected = [
  ["missing", null], ["corrupt", "{"], ["null record", "null"], ["array record", "[]"],
  ["oversized", " ".repeat(65537)], ["unknown", '{"token":"private"}'], ["calculator", '{"currentIncome":"9"}'],
  ["nonfinite", '{"minCap":1e400}'], ["wrong type", '{"hideNA":1}'], ["negative", '{"maxPe":-1}'],
  ["unsupported enum", '{"gaps":"THREE_PLUS"}'], ["invalid sort key", '{"sort":{"key":"unknown","dir":1}}'],
  ["invalid sort direction", '{"sort":{"key":"ticker","dir":0}}'], ["empty sort", '{"sort":[]}'],
  ["invalid excluded scroll", '{"scroll":"invalid"}'], ["too long query", JSON.stringify({ q: "x".repeat(1001) })],
  ["duplicate selection", '{"sectors":["same","same"]}'], ["negative criterion floor", '{"minMet":-1}'],
  ["fractional criterion floor", '{"minMet":6.5}'], ["string criterion floor", '{"minMet":"7"}'],
  ["null criterion floor", '{"minMet":null}'], ["boolean criterion floor", '{"minMet":true}'],
];
for (const [name, raw] of rejected) test(`${name} saved view reports error without changing draft, saved state or source`, async () => {
  const env = setup(raw); await env.session.open(); const before = env.session.getState();
  assert.equal(env.session.importBrowserView(before.contextGeneration), false);
  const after = env.session.getState();
  assert.deepEqual(after.draft, before.draft); assert.deepEqual(after.saved, before.saved);
  assert.equal(after.browserImport.status, "error"); assert.equal(after.dirty, before.dirty); assert.equal(env.calls.length, 1);
});

test("unavailable browser storage fails explicitly", () => {
  assert.throws(() => readSavedBrowserView({ getItem() { throw new Error("disabled"); } }), /could not be read/);
});

test("tracked-only imports block live view through existing owner; explicit edit unblocks", async () => {
  const env = setup('{"trackedOnly":true}'); await env.session.open(); const applied = env.views.length;
  assert.equal(env.session.importBrowserView(env.session.getState().contextGeneration), true);
  assert.equal(env.session.getState().applicationBlocked, true); assert.equal(env.views.length, applied);
  env.session.patchView({ trackedOnly: false }); assert.equal(env.session.getState().applicationBlocked, false);
  assert.equal(env.session.getState().draft.view.trackedOnly, false);
});

test("idle, loading and denied import perform no legacy read or mutation", async () => {
  const pending = deferred(), env = setup('{}', () => pending.promise);
  assert.equal(env.session.importBrowserView(0), false);
  const open = env.session.open(); assert.equal(env.session.importBrowserView(env.session.getState().contextGeneration), false);
  pending.reject(new WorkspaceError("sign-in", 401)); await open;
  assert.equal(env.session.importBrowserView(env.session.getState().contextGeneration), false); assert.equal(env.reads(), 0);
});

for (const failure of [new WorkspaceError("uncertain", 0, null, true), new WorkspaceError("conflict", 409, 9)])
test(`${failure.status === 409 ? "conflict" : "uncertain"} state refuses import and keeps exact pending request`, async () => {
  const calls = []; let mutations = 0;
  const env = setup('{}', async (path, options) => { calls.push({ path, ...options });
    if (options.method === "PUT") { mutations++; throw failure; } return revision(); });
  await env.session.open(); env.session.patchView({ q: "existing draft" }); await env.session.save();
  const before = env.session.getState(); assert.equal(env.session.importBrowserView(before.contextGeneration), false);
  assert.deepEqual(env.session.getState(), before); assert.equal(env.reads(), 0);
  if (failure.status !== 409) { await env.session.retry(); assert.equal(mutations, 2); assert.equal(calls[1].body, calls[2].body); }
});

test("context reset refuses prior control even when same workspace/revision is admitted again", async () => {
  const env = setup('{"q":"browser"}'); await env.session.open(); const old = env.session.getState().contextGeneration;
  await env.session.open(); const before = env.session.getState();
  assert.equal(env.session.importBrowserView(old), false); assert.deepEqual(env.session.getState(), before); assert.equal(env.reads(), 0);
  assert.equal(env.session.importBrowserView(before.contextGeneration), true);
  env.session.close(); assert.equal(env.session.getState().browserImport, null);
});

test("a pending write and accepted-but-unchecked revision refuse import before any browser read", async () => {
  const pending = deferred(); let current = revision(), reads = 0;
  const env = setup('{}', async (_path, options) => {
    if (options.method === "PUT") { await pending.promise; current = revision(2, JSON.parse(options.body).configuration); return current; }
    if (++reads > 1) throw new WorkspaceError("unavailable", 503);
    return current;
  });
  await env.session.open(); env.session.patchView({ q: "draft" }); const save = env.session.save();
  const writing = env.session.getState(); assert.equal(env.session.importBrowserView(writing.contextGeneration), false);
  assert.deepEqual(env.session.getState(), writing); pending.resolve(); await save;
  const unchecked = env.session.getState(); assert.equal(unchecked.needsRefresh, true);
  assert.equal(env.session.importBrowserView(unchecked.contextGeneration), false);
  assert.deepEqual(env.session.getState(), unchecked); assert.equal(env.reads(), 0);
});

test("inaccessible storage preserves admitted draft and saved identity", async () => {
  const session = createWorkspaceSession(() => {}, { request: async () => revision(),
    readBrowserView: () => readSavedBrowserView({ getItem() { throw new Error("SecurityError"); } }) });
  await session.open(); const before = session.getState(); assert.equal(session.importBrowserView(before.contextGeneration), false);
  assert.deepEqual(session.getState().draft, before.draft); assert.deepEqual(session.getState().saved, before.saved);
  assert.match(session.getState().browserImport.message, /could not be read/);
});

test("import does not weaken history/ordinary source-qualified restore", async () => {
  let current = revision(3), captured;
  const original = configuration(); original.view.q = "historical";
  const env = setup('{"q":"browser"}', async (path, options) => {
    if (path.includes("revisions")) return { revisions: [revision(1, original)], next_before: null };
    if (options.method === "POST") { captured = JSON.parse(options.body); current = revision(4, original, 1); }
    return structuredClone(current);
  });
  await env.session.open(); env.session.importBrowserView(env.session.getState().contextGeneration);
  await env.session.history(); assert.equal(await env.session.restore(1), true);
  assert.deepEqual(captured, { expected_revision: 3, source_revision: 1, request_key: "same-key-1" });
  assert.equal(env.session.getState().saved.restored_from_revision, 1); assert.equal(env.session.getState().draft.view.q, "historical");
});
