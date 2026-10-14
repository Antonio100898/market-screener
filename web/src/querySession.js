import { unfilteredView } from "./view.js";
import { normalizeSort } from "./sort.js";

export const PAGE_SIZE = 100;
export const initialQueryView = () => ({ ...unfilteredView(), q: "", sort: [{ key: "ticker", dir: 1 }] });
export const emptyQueryState = () => ({ phase: "loading", rows: [], release: null, facets: null,
  overview: null, counts: {}, matched: 0, matchesAnywhere: 0, cursor: null, moreLoading: false, error: null, generation: 0 });

export class QueryError extends Error {
  constructor(message, status = 503) { super(message); this.status = status; }
}

const record = (value) => value != null && typeof value === "object" && !Array.isArray(value);
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const fail = () => { throw new QueryError("The server returned an unsupported data response. Reload or try again."); };
const nullableNumber = (value) => value == null || Number.isFinite(value);
const validCriteria = (criteria) => Array.isArray(criteria) && criteria.length === 6 && [1,2,3,4,5,7].every((n) =>
  criteria.filter((item) => record(item) && item.n === n && typeof item.status === "string" && nullableNumber(item.value)).length === 1);

export function queryParameters(view, release, cursor) {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), row_fields: "full" });
  for (const [field, parameter] of Object.entries({ q: "q", lens: "lens", fit: "fit", gaps: "gaps",
    minCap: "min_cap", minMet: "min_met", minPositiveEps: "min_positive_eps", maxPe: "max_pe",
    maxPe3: "max_pe3", hideNA: "hide_na", hideNoApply: "hide_no_apply", belowNcav: "below_ncav" })) {
    params.set(parameter, String(view[field]));
  }
  for (const [field, parameter] of Object.entries({ profiles: "profile", sectors: "sector", venues: "venue", indexes: "index" })) {
    for (const value of view[field]) params.append(parameter, value);
  }
  for (const { key, dir } of normalizeSort(view.sort)) params.append("sort", `${key}:${dir}`);
  if (release != null) params.set("release", String(release));
  if (cursor != null) params.set("cursor", cursor);
  return params;
}

export async function readQueryApi(path, signal, fetcher = fetch) {
  const timeout = new AbortController();
  const abort = () => timeout.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetcher(`/api/v1/${path}`, { cache: "no-store", signal: timeout.signal,
      headers: { Accept: "application/json" } });
    const body = await response.json().catch((error) => {
      if (error?.name === "AbortError") throw error;
      return null;
    });
    if (timeout.signal.aborted) throw new DOMException("Request canceled", "AbortError");
    if (!response.ok) throw new QueryError(body?.detail?.message ?? (typeof body?.detail === "string" ? body.detail : `Data request failed (HTTP ${response.status}).`), response.status);
    if (!record(body)) fail();
    return body;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (error.name === "AbortError") throw new QueryError("Data request timed out. Try again.");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}

export function checkQueryRelease(body, expected) {
  const release = body?.release;
  if (!record(release) || !integer(release.release_id) || release.release_id === 0
    || release.projection_revision !== 1 || !integer(release.total)
    || !Array.isArray(release.engine_revisions) || !release.engine_revisions.every(integer)
    || typeof release.published_at !== "string" || !Number.isFinite(Date.parse(release.published_at))) fail();
  if (expected != null && release.release_id !== expected) fail();
  return release;
}

function checkPage(body, expected) {
  const release = checkQueryRelease(body, expected);
  if (!Array.isArray(body.rows) || body.rows.length > PAGE_SIZE || !integer(body.matched)
    || body.matched > release.total || !record(body.counts) || body.counts.matched !== body.matched
    || !integer(body.matches_anywhere) || body.matches_anywhere > release.total
    || !(body.next_cursor === null || (typeof body.next_cursor === "string" && body.next_cursor.length > 0 && body.next_cursor.length <= 8192))) fail();
  for (const key of ["unjudged", "inapplicable", "net_net", "judged", ...[1,2,3,4,5,6].map((n) => `met_${n}`), ...[5,6,7,9,10].map((n) => `eps_${n}`)]) {
    if (!integer(body.counts[key]) || body.counts[key] > body.matched) fail();
  }
  const ids = new Set();
  for (const row of body.rows) {
    const quality = row?.returnQuality;
    if (!record(row) || !integer(row.security_id) || row.security_id === 0 || ids.has(row.security_id)
      || typeof row.ticker !== "string" || !row.ticker || typeof row.name !== "string"
      || typeof row.profile !== "string" || !validCriteria(row.criteria)
      || ![row.price, row.mcap, row.pe3, row.dividendYield].every(nullableNumber)
      || (row.annual_eps != null && (!record(row.annual_eps) || !Object.values(row.annual_eps).every(nullableNumber)))
      || !record(quality) || !Array.isArray(quality.missingInputs) || !Array.isArray(quality.assumptions)
      || !record(quality.estimatedInputs) || !record(quality.assumedInputs)
      || !Number.isFinite(quality.score) || quality.score < 0 || quality.score > 100) fail();
    ids.add(row.security_id);
  }
  if ((body.rows.length === 0 && body.next_cursor != null) || body.rows.length > body.matched) fail();
  return body;
}

function checkFacets(body, release) {
  checkQueryRelease(body, release);
  for (const key of ["sectors", "venues", "profiles", "indexes"]) {
    if (!Array.isArray(body[key]) || !body[key].every((item) => Array.isArray(item) && item.length === 2 && typeof item[0] === "string" && integer(item[1]))) fail();
  }
  return body;
}

function checkOverview(body, release) {
  checkQueryRelease(body, release);
  if (!integer(body.members)) fail();
  for (const key of ["pe", "pe3"]) {
    const metric = body[key];
    if (!record(metric) || !integer(metric.count) || metric.count > body.members
      || !(metric.median === null || (Number.isFinite(metric.median) && metric.median > 0))) fail();
  }
  return body;
}

export async function negotiateQuery(read, signal) {
  const [capabilities, status] = await Promise.all([read("capabilities", signal), read("data-status", signal)]);
  if (capabilities?.api_contract_major !== 1 || capabilities.projection_revision !== 1
    || !integer(capabilities.max_page_size) || capabilities.max_page_size < PAGE_SIZE
    || !Array.isArray(capabilities.row_fields) || !capabilities.row_fields.includes("full")) {
    throw new QueryError("This dashboard and server are incompatible. Reload or upgrade the application.");
  }
  if (status?.projection_ready !== true || status.query_configuration_ready !== true) {
    throw new QueryError("Published screening data is unavailable. Try again later.");
  }
  const release = checkQueryRelease(status);
  if (status.active_release_id !== release.release_id) fail();
  return release;
}

export async function readPinnedDetail(row, release, assumed, signal, read = readQueryApi) {
  const body = await read(`companies/${row.security_id}?release=${release}&assume_absent_zero=${assumed}`, signal);
  checkQueryRelease(body, release);
  if (body.security_id !== row.security_id || !record(body.row) || body.row.ticker !== row.ticker
    || !validCriteria(body.row.criteria)) fail();
  return { ...body.row, security_id: body.security_id };
}

export function createQuerySession(onChange, read = readQueryApi) {
  let state = emptyQueryState(), view, generation = 0, request, checking;
  const emit = (patch) => { state = { ...state, ...patch }; onChange(state); };
  const current = (epoch) => epoch === generation;
  const errorState = (error) => emit({ ...emptyQueryState(), generation, phase: "error", error: error.message });
  const cancel = () => { generation += 1; request?.abort(); checking?.abort(); };

  const start = async (nextView, mayRestart = true) => {
    cancel(); view = nextView;
    const epoch = generation;
    request = new AbortController();
    const signal = request.signal;
    emit({ ...emptyQueryState(), generation });
    try {
      const release = await negotiateQuery(read, signal);
      if (!current(epoch)) return;
      const id = release.release_id;
      const [page, facets, overview] = await Promise.all([
        read(`companies?${queryParameters(view, id)}`, signal).then((body) => checkPage(body, id)),
        read(`facets?release=${id}`, signal).then((body) => checkFacets(body, id)),
        read(`overview?release=${id}`, signal).then((body) => checkOverview(body, id)),
      ]);
      if (!current(epoch)) return;
      emit({ phase: "ready", view: nextView, release: page.release, rows: page.rows, counts: page.counts, matched: page.matched,
        matchesAnywhere: page.matches_anywhere, cursor: page.next_cursor, facets, overview });
    } catch (error) {
      if (!current(epoch)) return;
      request.abort();
      if (error.status === 409 && mayRestart) await start(nextView, false);
      else if (error.status === 409) errorState(new QueryError("The published release changed. Refresh this query to continue."));
      else errorState(error);
    }
  };

  const more = async () => {
    if (state.phase !== "ready" || state.moreLoading || !state.cursor) return;
    const epoch = generation, cursor = state.cursor, id = state.release.release_id;
    emit({ moreLoading: true, error: null });
    try {
      const page = checkPage(await read(`companies?${queryParameters(view, id, cursor)}`, request.signal), id);
      if (!current(epoch)) return;
      const seen = new Set(state.rows.map((row) => row.security_id));
      if (page.rows.some((row) => seen.has(row.security_id)) || page.next_cursor === cursor
        || Object.keys(state.counts).some((key) => state.counts[key] !== page.counts[key])
        || state.rows.length + page.rows.length > state.matched) fail();
      emit({ rows: [...state.rows, ...page.rows], cursor: page.next_cursor, moreLoading: false });
    } catch (error) {
      if (!current(epoch)) return;
      if (error.status === 409) await start(view);
      else emit({ moreLoading: false, error: error.message });
    }
  };

  const check = async () => {
    if (checking || !view) return;
    const epoch = generation;
    const probe = new AbortController(); checking = probe;
    try {
      const release = await negotiateQuery(read, probe.signal);
      if (current(epoch) && (state.phase !== "ready" || state.release.release_id !== release.release_id)) await start(view);
    } catch (error) {
      if (current(epoch)) { request?.abort(); generation += 1; errorState(error); }
    } finally { if (checking === probe) checking = null; }
  };
  return { start, more, check, cancel };
}
