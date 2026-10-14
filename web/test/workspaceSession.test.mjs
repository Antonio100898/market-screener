import assert from "node:assert/strict";
import test from "node:test";
import { checkWorkspaceConfig, createWorkspaceTransport, createWorkspaceSession, WorkspaceError, workspaceColumns } from "../src/workspaceSession.js";
import { configuration, revision, deferred } from "./workspaceFixtures.mjs";

const response = (body, status = 200, cache = "private, no-store") => new Response(JSON.stringify(body), { status, headers: { "Cache-Control": cache } });
function setup(request) {
  const events = [], views = [], columns = []; let clears = 0, keys = 0;
  const session = createWorkspaceSession((state) => events.push(state), { request, newKey: () => `key-${++keys}`,
    onApply: (view) => views.push(view), onColumns: (cols) => columns.push(cols), onClear: () => clears++ });
  return { session, events, views, columns, clears: () => clears, keys: () => keys };
}

test("private transport uses same-origin/no-store and the existing injected instance-token accessor", async () => {
  const calls = []; let tokens = 0;
  const request = createWorkspaceTransport({ getToken: async () => { tokens++; return "instance-guard"; }, fetcher: async (url, options) => { calls.push({ url, options }); return response(revision()); } });
  await request("/default"); await request("/7", { method: "PUT", body: '{"expected_revision":1}' });
  assert.equal(tokens, 1); assert.equal(calls[0].url, "/research/workspaces/default");
  for (const call of calls) { assert.equal(call.options.credentials, "same-origin"); assert.equal(call.options.cache, "no-store"); assert.equal(call.options.headers["X-Owner-Id"], undefined); }
  assert.equal(calls[1].options.headers["X-Screener-Token"], "instance-guard");
});

for (const status of [401, 403, 404, 409, 422, 503]) test(`private HTTP${status} preserves typed denial/conflict without token prompt`, async () => {
  const request = createWorkspaceTransport({ getToken: async () => "", fetcher: async () => response({ detail: { message: "untrusted private text", current_revision: 4 } }, status) });
  await assert.rejects(request("/7/restore", { method: "POST", body: "{}" }), (error) => error instanceof WorkspaceError && error.status === status && !error.message.includes("untrusted") && error.currentRevision === ([401, 403].includes(status) ? null : 4) && error.uncertain === (status >= 500));
});

test("timeout covers delayed response body and retains uncertain write status", async () => {
  const request = createWorkspaceTransport({ timeoutMs: 10, getToken: async () => "", fetcher: async () => ({ ok: true, headers: new Headers({ "Cache-Control": "private, no-store" }), json: () => new Promise(() => {}) }) });
  await assert.rejects(request("/7", { method: "PUT", body: "{}" }), (error) => error.uncertain && /timed out/.test(error.message));
});

test("caller abort cancels body consumption; unsupported cache or JSON never looks like an accepted configuration", async () => {
  const controller = new AbortController();
  const request = createWorkspaceTransport({ fetcher: async () => ({ ok: true, headers: new Headers(), json: () => new Promise(() => {}) }) });
  const pending = request("/default", { signal: controller.signal }); controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  await assert.rejects(createWorkspaceTransport({ fetcher: async () => response(revision(), 200, "public") })("/default"), /Unsupported/);
  await assert.rejects(createWorkspaceTransport({ fetcher: async () => response(null) })("/default"), /Unsupported/);
});

test("schema1 preserves every field, unknown facets, fit sort, empty columns and Unicode lengths without defaults", () => {
  const config = configuration(); config.columns = []; config.view.q = "😀".repeat(1000); config.view.trackedOnly = true;
  assert.deepEqual(checkWorkspaceConfig(config), config);
  for (const changed of [ { ...config, extra: 1 }, { ...config, schema_revision: 2 }, { ...config, view: { ...config.view, future: 1 } },
    { ...config, view: { ...config.view, minMet: true } }, { ...config, columns: ["price", "price"] },
    { ...config, view: { ...config.view, sort: [{ key: "fit", dir: true }] } }, { ...config, view: { ...config.view, profiles: ["x", "x"] } } ]) assert.throws(() => checkWorkspaceConfig(changed), /Unsupported/);
  const incomplete = configuration(); delete incomplete.view.maxPe;
  assert.throws(() => checkWorkspaceConfig(incomplete), /Unsupported/);
});

test("loading/denial never creates client defaults or saves; tracked-only blocks apply and columns do not restart queries", async () => {
  const waiting = deferred(); let writes = 0;
  const env = setup(async (_path, options) => { if (options.method) writes++; return waiting.promise; });
  const loading = env.session.open(); assert.equal(await env.session.save(), false); assert.equal(env.session.getState().draft, null);
  const config = configuration(); config.view.trackedOnly = true; config.columns = [];
  waiting.resolve(revision(1, config)); await loading;
  assert.equal(writes, 0); assert.equal(env.views.length, 0); assert.equal(env.session.getState().applicationBlocked, true);
  env.session.setColumns(["dividend"]); assert.equal(env.views.length, 0);
  env.session.patchView({ trackedOnly: false }); assert.equal(env.views.length, 1);
  env.session.setColumns([]); assert.equal(env.views.length, 1); assert.deepEqual(env.session.getState().draft.columns, []);
  const denied = setup(async () => { throw new WorkspaceError("Verified sign-in is required.", 401); });
  await denied.session.open(); assert.equal(denied.session.getState().phase, "denied"); assert.equal(denied.session.getState().draft, null);
});

test("manual save sends the full supported configuration; newer draft survives its delayed acknowledgement", async () => {
  const delayed = deferred(); const calls = []; let current = revision();
  const env = setup(async (path, options) => { calls.push({ path, ...options }); return options.method ? delayed.promise : current; });
  await env.session.open(); env.session.patchView({ q: "save this" }); const saving = env.session.save();
  const body = JSON.parse(calls.at(-1).body); assert.deepEqual(Object.keys(body), ["expected_revision", "request_key", "configuration"]);
  assert.equal(body.expected_revision, 1); assert.equal(body.request_key, "key-1");
  assert.deepEqual(body.configuration, env.session.getState().draft);
  env.session.patchView({ q: "newer draft" }); current = revision(2, body.configuration); delayed.resolve(current); await saving;
  assert.equal(env.session.getState().saved.edit_revision, 2); assert.equal(env.session.getState().draft.view.q, "newer draft"); assert.equal(env.session.getState().dirty, true);
});

test("uncertain delivery retries identical key/body despite newer draft; old replay reconciles current without rebasing", async () => {
  const calls = []; let current = revision(), failed = false;
  const env = setup(async (path, options) => {
    calls.push({ path, ...options });
    if (!options.method) return current;
    if (!failed) { failed = true; throw new WorkspaceError("lost response", 0, null, true); }
    const saved = revision(2, JSON.parse(options.body).configuration); current = revision(3, { ...configuration(), view: { ...configuration().view, q: "another session" } }); return saved;
  });
  await env.session.open(); env.session.patchView({ q: "submitted" }); await env.session.save();
  assert.ok(env.session.getState().uncertain); env.session.patchView({ q: "newer draft" }); assert.equal(await env.session.save(), false);
  await env.session.retry(); const writes = calls.filter((call) => call.method);
  assert.equal(writes.length, 2); assert.equal(writes[0].body, writes[1].body); assert.equal(env.keys(), 1);
  assert.equal(env.session.getState().saved.edit_revision, 2); assert.equal(env.session.getState().current.edit_revision, 3);
  assert.equal(env.session.getState().draft.view.q, "newer draft"); assert.ok(env.session.getState().conflict);
  assert.equal(await env.session.save(), false); await env.session.reload();
  assert.equal(env.session.getState().saved.edit_revision, 3); assert.equal(env.session.getState().draft.view.q, "another session");
});

test("known acknowledgement followed by failed current GET retries only the read", async () => {
  let current = revision(), reads = 0, writes = 0, failRead = true;
  const env = setup(async (_path, options) => {
    if (options.method) { writes++; current = revision(2, JSON.parse(options.body).configuration); return current; }
    if (++reads === 2 && failRead) throw new WorkspaceError("read failed", 503);
    return current;
  });
  await env.session.open(); env.session.patchView({ q: "saved" }); await env.session.save();
  assert.equal(env.session.getState().saved.edit_revision, 2); assert.equal(env.session.getState().needsRefresh, true); assert.equal(env.session.getState().uncertain, null);
  failRead = false; await env.session.retry(); assert.equal(writes, 1); assert.equal(env.session.getState().needsRefresh, false);
});

test("409 retains draft and never retries or adopts the new parent automatically", async () => {
  let writes = 0;
  const env = setup(async (_path, options) => { if (options.method) { writes++; throw new WorkspaceError("conflict", 409, 8); } return revision(); });
  await env.session.open(); env.session.setColumns([]); await env.session.save();
  assert.equal(env.session.getState().conflict.currentRevision, 8); assert.deepEqual(env.session.getState().draft.columns, []);
  assert.equal(await env.session.save(), false); assert.equal(await env.session.retry(), false); assert.equal(writes, 1);
});

for (const late of ["success", "error"]) test(`context reset rejects late ${late} from GET/history/save and clears private state`, async () => {
  for (const kind of ["read", "history", "write", "restore"]) {
    const pending = deferred(); let first = true;
    const env = setup(async (path, options) => {
      if (kind !== "read" && first) { first = false; return revision(); }
      if (kind === "restore" && path.includes("revisions")) return { revisions: [revision()], next_before: null };
      return pending.promise;
    });
    let work;
    if (kind === "read") work = env.session.open();
    else { await env.session.open(); if (kind === "restore") await env.session.history(); work = kind === "history" ? env.session.history() : kind === "restore" ? env.session.restore(1) : env.session.save(); }
    env.session.reset();
    if (late === "error") pending.reject(new WorkspaceError("old private error", 403));
    else pending.resolve(kind === "history" ? { revisions: [revision()], next_before: null } : revision(2));
    await work; const state = env.session.getState();
    assert.equal(state.phase, "idle"); assert.equal(state.draft, null); assert.equal(state.saved, null); assert.deepEqual(state.history.revisions, []); assert.equal(state.error, null);
  }
});

test("history page and restore preserve source identity with exact ownership-free body and live-view callback", async () => {
  let current = revision(4); const calls = [];
  const old = revision(2, { ...configuration(), columns: [] }, 1);
  const env = setup(async (path, options) => {
    calls.push({ path, ...options });
    if (path.includes("revisions")) return { revisions: [old, revision(1)], next_before: null };
    if (options.method) { current = revision(5, old.configuration, 2); return current; }
    return current;
  });
  await env.session.open(); await env.session.history(); env.session.selectRevision(2);
  assert.equal(env.session.getState().history.selected.restored_from_revision, 1);
  await env.session.restore(2); const write = calls.find((call) => call.method);
  assert.equal(write.path, "/7/restore"); assert.equal(write.method, "POST");
  assert.deepEqual(JSON.parse(write.body), { expected_revision: 4, source_revision: 2, request_key: "key-1" });
  assert.deepEqual(env.session.getState().draft.columns, []); assert.equal(env.session.getState().saved.restored_from_revision, 2);
  assert.equal(calls.find((call) => call.path.includes("revisions")).path, "/7/revisions?limit=10");
});

test("history seeks one bounded page and ignores an older page after a new request", async () => {
  const late = deferred(); let calls = 0;
  const env = setup(async (path) => { if (!path.includes("revisions")) return revision(20); if (++calls === 1) return late.promise; return { revisions: [revision(2)], next_before: null }; });
  await env.session.open(); const first = env.session.history(); await env.session.history(3);
  late.resolve({ revisions: [revision(10)], next_before: null }); await first;
  assert.equal(env.session.getState().history.revisions[0].edit_revision, 2);
  assert.equal(await env.session.history(null, 101), false); assert.equal(calls, 2);
});

test("account denial after loaded state clears draft and revision preview", async () => {
  let denied = false;
  const env = setup(async (path) => { if (denied) throw new WorkspaceError("account unavailable", 403); return path.includes("revisions") ? { revisions: [revision()], next_before: null } : revision(); });
  await env.session.open(); await env.session.history(); env.session.selectRevision(1); denied = true; await env.session.history();
  assert.equal(env.session.getState().phase, "denied"); assert.equal(env.session.getState().draft, null); assert.equal(env.session.getState().history.selected, null);
});

test("identity denial clears loaded state even when its cache contract is broken", async () => {
  const request = createWorkspaceTransport({ fetcher: async () => response({}, 401, "public") });
  await assert.rejects(request("/default"), (error) => error.status === 401 && /Verified sign-in/.test(error.message));
});

test("uncertain restore retains source/key/body and preserves a newer draft after retry", async () => {
  const calls = []; let current = revision(3), failed = false;
  const selected = revision(1, { ...configuration(), columns: [] });
  const env = setup(async (path, options) => {
    calls.push({ path, ...options });
    if (path.includes("revisions")) return { revisions: [selected], next_before: null };
    if (options.method) {
      if (!failed) { failed = true; throw new WorkspaceError("temporarily unavailable", 503, null, true); }
      current = revision(4, selected.configuration, 1); return current;
    }
    return current;
  });
  await env.session.open(); await env.session.history(); await env.session.restore(1);
  env.session.patchView({ q: "new draft" }); await env.session.retry();
  const writes = calls.filter((call) => call.method);
  assert.equal(writes[0].body, writes[1].body); assert.equal(env.keys(), 1);
  assert.deepEqual(Object.keys(JSON.parse(writes[0].body)), ["expected_revision", "source_revision", "request_key"]);
  assert.equal(env.session.getState().draft.view.q, "new draft"); assert.equal(env.session.getState().saved.restored_from_revision, 1);
});

test("unsupported acknowledged response keeps delivery uncertain and never replaces a draft", async () => {
  const env = setup(async (_path, options) => options.method ? { ...revision(2), configuration: { ...configuration(), future: 1 } } : revision());
  await env.session.open(); env.session.patchView({ q: "draft" }); await env.session.save();
  assert.ok(env.session.getState().uncertain); assert.equal(env.session.getState().draft.view.q, "draft"); assert.equal(env.session.getState().saved.edit_revision, 1);
});

test("malformed timestamps, actors and history order produce explicit unsupported errors", async () => {
  for (const patch of [{ created_at: {} }, { actor: "fabricated" }, { updated_at: "invalid" }]) {
    const env = setup(async () => ({ ...revision(), ...patch })); await env.session.open();
    assert.equal(env.session.getState().phase, "error"); assert.equal(env.session.getState().draft, null);
  }
  const env = setup(async (path) => path.includes("revisions") ? { revisions: [revision(1), revision(2)], next_before: null } : revision(2));
  await env.session.open(); await env.session.history(); assert.equal(env.session.getState().history.phase, "error");
});

test("known current revision cannot regress on a later stale GET", async () => {
  let current = revision(2);
  const env = setup(async () => current); await env.session.open();
  current = revision(5); await env.session.refresh(); assert.equal(env.session.getState().current.edit_revision, 5);
  current = revision(3); await env.session.refresh();
  assert.equal(env.session.getState().phase, "error"); assert.equal(env.session.getState().current.edit_revision, 5);
  assert.equal(env.session.getState().saved.edit_revision, 2);
});

test("supported column contract cannot be mutated by a consumer", () => {
  assert.throws(() => workspaceColumns.push("arbitrary-code"), TypeError);
  assert.equal(workspaceColumns.length, 11);
});

test("restore acknowledgement must match inspected configuration across paging and exact retry", async () => {
  const inspected = revision(1, { ...configuration(), columns: [] });
  const different = { ...configuration(), view: { ...configuration().view, q: "wrong source" } };
  let current = revision(3), writes = 0, pages = 0;
  const calls = [];
  const env = setup(async (path, options) => {
    if (path.includes("revisions")) return { revisions: [++pages === 1 ? inspected : revision(2)], next_before: null };
    if (options.method) {
      calls.push({ path, body: options.body });
      if (++writes < 3) return revision(4, different, 1);
      current = revision(4, inspected.configuration, 1); return current;
    }
    return current;
  });
  await env.session.open(); await env.session.history(); await env.session.restore(1);
  assert.ok(env.session.getState().uncertain);
  assert.equal(env.session.getState().saved.edit_revision, 3);
  assert.ok(!env.views.some((view) => view.q === "wrong source"));
  await env.session.history(); env.session.patchView({ q: "new draft" });
  await env.session.retry();
  assert.ok(env.session.getState().uncertain);
  assert.equal(env.session.getState().saved.edit_revision, 3);
  assert.equal(env.session.getState().draft.view.q, "new draft");
  await env.session.retry();
  assert.equal(env.session.getState().uncertain, null);
  assert.deepEqual(env.session.getState().saved.configuration, inspected.configuration);
  assert.equal(env.session.getState().saved.restored_from_revision, 1);
  assert.equal(env.session.getState().draft.view.q, "new draft");
  assert.ok(calls.every((call) => call.path === calls[0].path && call.body === calls[0].body));
  assert.equal(env.keys(), 1);
});

for (const field of ["configuration", "restored_from_revision"]) test(`observed current identity rejects changed ${field} before refresh or discard`, async () => {
  let current = revision(2);
  const env = setup(async () => current);
  await env.session.open(); env.session.patchView({ q: "unsaved draft" });
  const observed = revision(5, { ...configuration(), columns: [] }, 1);
  current = observed; await env.session.refresh();
  current = field === "configuration" ? revision(5, configuration(), 1) : revision(5, observed.configuration, 2);
  assert.equal(await env.session.refresh(), false);
  assert.equal(env.session.getState().phase, "error");
  assert.deepEqual(env.session.getState().current, observed);
  assert.equal(await env.session.reload(), false);
  assert.deepEqual(env.session.getState().current, observed);
  assert.equal(env.session.getState().saved.edit_revision, 2);
  assert.equal(env.session.getState().draft.view.q, "unsaved draft");
  current = revision(6, configuration(), 2); await env.session.refresh();
  assert.equal(env.session.getState().current.edit_revision, 6);
  assert.equal(env.session.getState().conflict.currentRevision, 6);
  assert.equal(env.session.getState().draft.view.q, "unsaved draft");
});
