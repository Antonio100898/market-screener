import { QueryError, readQueryApi } from "./querySession.js";

const record = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const positive = (v) => Number.isSafeInteger(v) && v > 0;
const views = ["STRICT", "ASSUMED", "CONSERVATIVE"];
const token = (v) => typeof v === "string" && v.length > 0 && v.length <= 8192;
const fail = () => { throw new QueryError("The history service returned an unsupported response."); };
export const historyViews = views;

export function historyContext(security, release) {
  if (!positive(security) || !positive(release)) return null;
  return { security_id: security, release_id: release };
}

export function historyPath(context, operation, params = {}) {
  if (!historyContext(context?.security_id, context?.release_id)) fail();
  const q = new URLSearchParams({ limit: "50" });
  if (operation === "catalog") {
    if (!views.includes(params.view)) fail();
    q.set("view", params.view); q.set("release", context.release_id);
  } else if (operation === "timeline" || operation === "activations") {
    if (!token(params.handle)) fail();
    q.set("handle", params.handle); q.set("operation", operation);
  } else if (operation === "support") {
    if (!token(params.handle)) fail();
    q.set("handle", params.handle);
  } else if (operation === "source-events") {
    if (!positive(params.source_item_id)) fail();
    q.set("source_item_id", params.source_item_id); q.set("release", context.release_id);
  } else fail();
  if (params.cursor != null) {
    if (!token(params.cursor)) fail(); q.set("cursor", params.cursor);
  }
  const suffix = { catalog: "figures", timeline: "selected", activations: "selected", support: "dependencies", "source-events": "source-events" }[operation];
  return `companies/${context.security_id}/figure-history/${suffix}?${q}`;
}

const exact = (v) => v === null || typeof v === "string" && /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?$/.test(v);
function selection(v) {
  return record(v) && (v.metric_path === null || typeof v.metric_path === "string" && v.metric_path.length > 0) && views.includes(v.view)
    && record(v.period) && ["INSTANT", "DURATION", "UNKNOWN"].includes(v.period.kind)
    && ["start", "end"].every((key) => v.period[key] == null || typeof v.period[key] === "string")
    && exact(v.value_decimal_text) && typeof v.state === "string";
}
function endpoint(v) {
  return record(v) && (positive(v.release_id) || v.release_id === null && v.selection === null) && (v.selection === null || selection(v.selection))
    && (v.supporting_records == null || record(v.supporting_records) && Object.values(v.supporting_records).every(token));
}
function boundEndpoint(value, context, selector, release) {
  return endpoint(value) && value.security_id === context.security_id && value.release_id === release
    && (value.selection === null || matchingSelector(value.selection, selector));
}
function matchingSelector(a, b) {
  return a?.metric_path === b?.metric_path && a?.view === b?.view && a?.period?.kind === b?.period?.kind
    && (a?.period?.start ?? null) === (b?.period?.start ?? null) && (a?.period?.end ?? null) === (b?.period?.end ?? null);
}
export function checkHistoryPage(body, context, operation, params = {}) {
  if (!record(body) || body.history_contract_revision !== 1 || body.security_id !== context.security_id
      || !Array.isArray(body.rows) || body.rows.length > 50 || !(body.next_cursor === null || token(body.next_cursor))) fail();
  const release = body.release?.release_id ?? body.release_id;
  if (release !== context.release_id) fail();
  if (operation === "catalog") {
    if (body.operation !== "catalog" || body.view !== params.view || body.financial_readiness !== "NOT_ASSESSED"
        || body.display_association !== "NOT_ASSESSED" || body.earlier_history_complete !== false) fail();
    for (const item of body.rows) {
      if (!record(item) || !token(item.selector_handle) || !endpoint(item.endpoint) || !item.endpoint.selection
          || !matchingSelector(item.selector, item.endpoint.selection) || !boundEndpoint(item.endpoint, context, item.selector, release) || item.selector.view !== params.view
          || !record(item.coverage) || item.endpoint.release_id !== release) fail();
    }
  } else if (operation === "timeline" || operation === "activations") {
    if (body.operation !== operation || !boundEndpoint(body.endpoint, context, params.selector, release) || !matchingSelector(body.selector, params.selector)
        || body.financial_readiness !== "NOT_ASSESSED" || body.earlier_history_complete !== false
        || !Number.isSafeInteger(body.scanned_releases) || body.scanned_releases < 1 || body.scanned_releases > 300) fail();
    for (const item of body.rows) {
      if (!record(item) || !positive(item.release_id)) fail();
      if (operation === "timeline" && (!boundEndpoint(item.previous, context, params.selector, item.parent_release_id) || !boundEndpoint(item.replacement, context, params.selector, item.release_id)
          || item.delta?.status !== "NOT_COMPUTED" || item.delta.value_decimal_text !== null)) fail();
      if (operation === "activations" && !boundEndpoint(item.endpoint, context, params.selector, item.release_id)) fail();
    }
  } else if (operation === "support") {
    if (body.kind !== params.kind || !positive(body.endpoint_release_id) || body.transitive_expansion_complete !== false) fail();
    for (const item of body.rows) {
      if (!record(item) || item.selection != null && !selection(item.selection)
          || item.observation != null && !exact(item.observation.value_decimal_text)
          || item.supporting_records != null && (!record(item.supporting_records) || !Object.values(item.supporting_records).every(token))) fail();
    }
  } else if (operation === "source-events") {
    if (body.source_item_id !== params.source_item_id || body.financial_readiness !== "NOT_ASSESSED" || body.affected_figures_assessed !== false) fail();
    if (body.rows.some((item) => !record(item) || !positive(item.occurrence_id))) fail();
  } else fail();
  return body;
}

export function checkHistoryCapabilities(value) {
  if (!record(value) || value.api_contract_major !== 1 || value.figure_history_supported !== true
      || value.figure_history_contract_revision !== 1 || value.figure_history_complete !== false
      || !Array.isArray(value.figure_history_operations)
      || !["catalog", "selected", "timeline", "activations", "dependencies", "source-events"].every((operation) => value.figure_history_operations.includes(operation))) {
    throw new QueryError("Captured figure navigation is unavailable in this service version.");
  }
}

// Independent instances keep catalog, inspector and supporting navigation races apart.
export function createHistorySession(onChange, read = readQueryApi) {
  let generation = 0, request, context, operation, params, state = {};
  const emit = (patch) => { state = { ...state, ...patch }; onChange(state); };
  const cancel = () => { generation += 1; request?.abort(); };
  const run = async (cursor = null) => {
    const epoch = generation;
    request = new AbortController();
    emit({ loading: true, error: null, restartJournal: false });
    try {
      if (cursor === null) {
        checkHistoryCapabilities(await read("capabilities", request.signal));
        if (epoch !== generation) return;
      }
      const result = checkHistoryPage(await read(historyPath(context, operation, { ...params, cursor }), request.signal), context, operation, params);
      if (epoch !== generation) return;
      emit({ page: result, rows: cursor ? [...state.rows, ...result.rows] : result.rows, loading: false });
    } catch (error) {
      if (epoch !== generation || request.signal.aborted) return;
      emit({ loading: false, error: error.message, restartJournal: operation === "source-events" && error.status === 409 });
    }
  };
  return {
    cancel,
    start: async (nextContext, nextOperation, nextParams = {}) => {
      cancel(); context = nextContext; operation = nextOperation; params = nextParams;
      state = {}; emit({ rows: [], page: null, error: null }); return run();
    },
    more: () => state.page?.next_cursor && !state.loading ? run(state.page.next_cursor) : Promise.resolve(),
  };
}

export function publicHistoryUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash
      && ["", "80", "443"].includes(url.port)
      && ["www.sec.gov", "sec.gov", "data.sec.gov", "disclosure.edinet-fsa.go.jp", "disclosure2.edinet-fsa.go.jp", "disclosure2dl.edinet-fsa.go.jp"].includes(url.hostname) ? value : null;
  } catch { return null; }
}
