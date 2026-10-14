import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createQuerySession, initialQueryView, queryParameters, QueryError, readPinnedDetail, readQueryApi } from "../src/querySession.js";

const release = (id = 1) => ({ release_id: id, projection_revision: 1, total: 6676,
  published_at: "2026-10-04T10:00:00Z", engine_revisions: [181] });
const criteria = [1,2,3,4,5,7].map((n) => ({ n, status: "INSUFFICIENT_DATA", value: null }));
const row = (id) => ({ security_id: id, ticker: `T${id}`, name: `Company ${id}`, profile: "OPERATING", criteria,
  returnQuality: { score: 31.6, missingInputs: [], assumptions: [], estimatedInputs: {}, assumedInputs: {} } });
const counts = { matched: 426, unjudged: 1, inapplicable: 1, net_net: 20, judged: 425,
  ...Object.fromEntries([1,2,3,4,5,6].map((n) => [`met_${n}`, 100])),
  ...Object.fromEntries([5,6,7,9,10].map((n) => [`eps_${n}`, 100])) };
const page = (id = 1, cursor = "second", rows = [row(1)]) => ({ release: release(id), rows,
  matched: counts.matched, counts, matches_anywhere: 667, next_cursor: cursor });
const fixture = () => {
  let active = 1, major = 1;
  const paths = [];
  const read = async (path) => {
    paths.push(path);
    if (path === "capabilities") return { api_contract_major: major, projection_revision: 1, max_page_size: 300, row_fields: ["full"] };
    if (path === "data-status") return { release: release(active), active_release_id: active, projection_ready: true, query_configuration_ready: true };
    const id = Number(new URLSearchParams(path.split("?")[1]).get("release"));
    if (path.startsWith("facets")) return { release: release(id), sectors: [["Energy", 900]], venues: [], profiles: [], indexes: [["S&P 500", 500]] };
    if (path.startsWith("overview")) return { release: release(id), members: 500, pe: { median: 24.3, count: 426 }, pe3: { median: 26.1, count: 400 } };
    if (path.startsWith("companies?")) return path.includes("cursor=") ? page(id, null, [row(2)]) : page(id);
    return { release: release(id), security_id: 1, row: { ticker: "T1", criteria, assumption_mode: { status: path.endsWith("true") ? "APPLIED" : "STRICT" } } };
  };
  return { read, paths, active: (id) => { active = id; }, major: (id) => { major = id; } };
};
const deferred = () => { let resolve; const promise = new Promise((next) => { resolve = next; }); return { promise, resolve }; };

test("all shared filters and multi-sort serialize with a fixed bound and no private query", () => {
  const params = queryParameters({ ...initialQueryView(), q: "energy %_", lens: "ENTERPRISING", fit: "ALIGNED", gaps: "ONE_GAP",
    sectors: ["Energy", "Utilities"], profiles: ["OPERATING"], venues: ["NYSE"], indexes: ["Outside"],
    minCap: 500e6, minMet: 4, minPositiveEps: 9, maxPe: 20, maxPe3: 15, hideNA: true, hideNoApply: true,
    belowNcav: true, trackedOnly: true, workspace_revision: 8, sort: [{ key: "returns", dir: 1 }, { key: "pe", dir: -1 }] }, 9, "opaque&cursor");
  assert.deepEqual(params.getAll("sort"), ["returns:1", "pe:-1"]);
  assert.deepEqual(params.getAll("sector"), ["Energy", "Utilities"]);
  assert.equal(params.get("q"), "energy %_");
  assert.equal(params.get("min_cap"), "500000000");
  assert.equal(params.get("min_met"), "4");
  assert.equal(params.get("min_positive_eps"), "9");
  assert.equal(params.get("max_pe"), "20");
  assert.equal(params.get("max_pe3"), "15");
  assert.equal(params.get("hide_na"), "true");
  assert.equal(params.get("hide_no_apply"), "true");
  assert.equal(params.get("below_ncav"), "true");
  assert.equal(params.get("limit"), "100");
  assert.equal(params.get("row_fields"), "full");
  assert.equal(params.get("release"), "9");
  assert.equal(params.get("cursor"), "opaque&cursor");
  assert.equal(params.has("tracked_only"), false);
  assert.equal(params.has("workspace_revision"), false);
});

test("one release supplies bounded pages, full-match counts, global facets and market reference", async () => {
  const api = fixture(); let state;
  const session = createQuerySession((next) => { state = next; }, api.read);
  await session.start(initialQueryView());
  assert.equal(state.phase, "ready");
  assert.equal(state.rows.length, 1);
  assert.equal(state.rows[0].returnQuality.score, 31.6);
  assert.equal(state.matched, 426);
  assert.equal(state.counts.eps_9, 100);
  assert.equal(state.facets.sectors[0][1], 900);
  assert.equal(state.overview.members, 500);
  assert.equal(state.overview.pe.median, 24.3);
  await Promise.all([session.more(), session.more()]);
  assert.deepEqual(state.rows.map((r) => r.security_id), [1,2]);
  assert.equal(state.matched, 426);
  assert.equal(state.overview.pe.median, 24.3);
  assert.equal(state.cursor, null);
  assert.equal(api.paths.filter((path) => path.includes("cursor=")).length, 1);
  for (const path of api.paths.filter((path) => path.includes("?"))) assert.equal(new URLSearchParams(path.split("?")[1]).get("release"), "1");
  session.cancel();
});

test("query edits abort prior requests and ignore late first pages and continuations", async () => {
  const api = fixture(), old = deferred(); let state, priorSignal;
  const session = createQuerySession((next) => { state = next; }, async (path, signal) => {
    if (path.startsWith("companies?") && path.includes("q=old")) { priorSignal = signal; return old.promise; }
    return api.read(path, signal);
  });
  const first = session.start({ ...initialQueryView(), q: "old" });
  await new Promise((resolve) => setImmediate(resolve));
  await session.start({ ...initialQueryView(), q: "new" });
  assert.equal(priorSignal.aborted, true);
  old.resolve(page(1, null, [row(99)])); await first;
  assert.equal(state.view.q, "new");
  assert.deepEqual(state.rows.map((r) => r.security_id), [1]);
  const continuation = deferred();
  const originalRead = api.read;
  const moreSession = createQuerySession((next) => { state = next; }, (path, signal) => path.includes("cursor=") ? continuation.promise : originalRead(path, signal));
  await moreSession.start(initialQueryView()); const more = moreSession.more();
  api.active(2); await moreSession.start({ ...initialQueryView(), maxPe: 15 });
  continuation.resolve(page(1, null, [row(2)])); await more;
  assert.equal(state.release.release_id, 2);
  assert.deepEqual(state.rows.map((r) => r.security_id), [1]);
  session.cancel(); moreSession.cancel();
});

test("publication refresh replaces all release-dependent state and renegotiates after reconnect", async () => {
  const api = fixture(); let state;
  const session = createQuerySession((next) => { state = next; }, api.read);
  const view = { ...initialQueryView(), sectors: ["Energy"], maxPe: 20 };
  await session.start(view); await session.more();
  api.active(2); await session.check();
  assert.equal(state.release.release_id, 2);
  assert.equal(state.facets.release.release_id, 2);
  assert.equal(state.overview.release.release_id, 2);
  assert.equal(state.view, view);
  assert.deepEqual(state.rows.map((r) => r.security_id), [1]);
  api.major(2); await session.check();
  assert.equal(state.phase, "error");
  assert.equal(state.rows.length, 0);
  assert.match(state.error, /incompatible/);
  api.major(1); await session.check(); assert.equal(state.phase, "ready");
  session.cancel();
});

test("409 restarts the complete selected query instead of appending another release", async () => {
  const api = fixture(); let state, expired = true;
  const session = createQuerySession((next) => { state = next; }, async (path, signal) => {
    if (path.includes("cursor=") && expired) { expired = false; api.active(2); throw new QueryError("expired", 409); }
    return api.read(path, signal);
  });
  await session.start(initialQueryView()); await session.more();
  assert.equal(state.release.release_id, 2);
  assert.deepEqual(state.rows.map((r) => r.security_id), [1]);
  assert.equal(state.facets.release.release_id, 2);
  session.cancel();
});

test("malformed, mismatched and oversized responses never become a ready dashboard", async () => {
  const cases = [null, 0, { ...page(), release: release(2) }, { ...page(), rows: Array.from({ length: 101 }, (_, i) => row(i + 1)) },
    { ...page(), rows: [row(1), row(1)] }, { ...page(), rows: [{ ...row(1), returnQuality: null }] },
    { ...page(), counts: { ...counts, net_net: 427 } }];
  for (const broken of cases) {
    const api = fixture(); let state;
    const session = createQuerySession((next) => { state = next; }, (path, signal) => path.startsWith("companies?") ? broken : api.read(path, signal));
    await session.start(initialQueryView());
    assert.equal(state.phase, "error"); assert.equal(state.rows.length, 0); session.cancel();
  }
  const api = fixture(); let state;
  const session = createQuerySession((next) => { state = next; }, (path, signal) => path.includes("cursor=") ? page(1, "third", [row(1)]) : api.read(path, signal));
  await session.start(initialQueryView()); await session.more();
  assert.deepEqual(state.rows.map((r) => r.security_id), [1]);
  assert.match(state.error, /unsupported data response/); session.cancel();
});

test("detail requests explicitly choose the same-release assumed or strict stored view", async () => {
  const api = fixture();
  assert.equal((await readPinnedDetail(row(1), 9, true, undefined, api.read)).assumption_mode.status, "APPLIED");
  assert.equal((await readPinnedDetail(row(1), 9, false, undefined, api.read)).assumption_mode.status, "STRICT");
  assert.deepEqual(api.paths, ["companies/1?release=9&assume_absent_zero=true", "companies/1?release=9&assume_absent_zero=false"]);
  await assert.rejects(readPinnedDetail(row(1), 9, true, undefined, async () => ({ release: release(10), security_id: 1, row: { ticker: "T1", criteria: [] } })), /unsupported/);
});

test("envelope identity survives strict, default assumed, strict switch and retry without a row identity", async () => {
  for (const status of ["APPLIED", "UNAVAILABLE"]) {
    const paths = [];
    const read = async (path) => {
      paths.push(path);
      return { release: release(9), security_id: 2, row: { ticker: "ABT", criteria,
        assumption_mode: { status: path.endsWith("true") ? status : "STRICT" } } };
    };
    const selected = { security_id: 2, ticker: "ABT" };
    const strict = await readPinnedDetail(selected, 9, false, undefined, read);
    assert.equal(strict.security_id, 2);
    const assumed = await readPinnedDetail(strict, 9, true, undefined, read);
    assert.equal(assumed.security_id, 2); assert.equal(assumed.assumption_mode.status, status);
    const switched = await readPinnedDetail(assumed, 9, false, undefined, read);
    assert.equal(switched.security_id, 2); assert.equal(switched.assumption_mode.status, "STRICT");
    await assert.rejects(readPinnedDetail(strict, 9, true, undefined, async () => { throw new QueryError("unavailable", 503); }), /unavailable/);
    assert.equal((await readPinnedDetail(strict, 9, true, undefined, read)).security_id, 2);
    assert.ok(paths.every((path) => path.startsWith("companies/2?release=9&")));
    await assert.rejects(readPinnedDetail(strict, 9, true, undefined, async () => ({ release: release(9), security_id: 3, row: { ticker: "ABT", criteria } })), /unsupported/);
  }
});

test("browser transport is GET-only, uncached, cancellable and reports server failures", async () => {
  const controller = new AbortController(); let options;
  const request = readQueryApi("companies?limit=100", controller.signal, async (path, next) => {
    assert.equal(path, "/api/v1/companies?limit=100"); options = next;
    return Response.json({ ok: true });
  });
  assert.deepEqual(await request, { ok: true });
  assert.equal(options.method, undefined); assert.equal(options.cache, "no-store");
  assert.deepEqual(options.headers, { Accept: "application/json" });
  await assert.rejects(readQueryApi("data-status", undefined, async () => Response.json({ detail: { code: "QUERY_NOT_READY", message: "No published release" } }, { status: 503 })), /No published release/);
  await assert.rejects(readQueryApi("capabilities", undefined, async () => Response.json(null)), /unsupported/);
  const cancelled = new AbortController();
  const pending = readQueryApi("companies", cancelled.signal, async (_, next) => new Promise((resolve, reject) => {
    next.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  }));
  cancelled.abort(); await assert.rejects(pending, { name: "AbortError" });
});

test("an obsolete detail response canceled while consuming409 cannot restart the current query", async () => {
  const source = readFileSync(new URL("../src/ApiDashboard.jsx", import.meta.url), "utf8");
  const start = source.indexOf("async (base, signal, assumed = true) => {");
  const end = source.indexOf("}, [release, onRestart]", start);
  const createLoad = new Function("readPinnedDetail", "release", "onRestart", `return (${source.slice(start, end)} });`);
  let restarts = 0;
  const callback = createLoad(readPinnedDetail, 1, () => { restarts += 1; });
  const aborted = new AbortController(), consuming = deferred();
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_, options) => ({ ok: false, status: 409, json() {
    consuming.resolve();
    return new Promise((resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("canceled body", "AbortError")), { once: true }));
  } });
  try {
    const pending = callback(row(1), aborted.signal);
    await consuming.promise; aborted.abort();
    await assert.rejects(pending, { name: "AbortError" });
    assert.equal(restarts, 0);
    const defensiveCallback = createLoad(async () => { throw new QueryError("obsolete", 409); }, 1, () => { restarts += 1; });
    await assert.rejects(defensiveCallback(row(1), aborted.signal));
    assert.equal(restarts, 0);
  } finally { globalThis.fetch = originalFetch; }
});
