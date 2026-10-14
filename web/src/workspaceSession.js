// Private presentation only. Shared queries and configuration defaults have other owners.
import { readSavedBrowserView } from "./view.js";

export const workspaceColumns = Object.freeze(["ticker", "name", "sector", "eps10", "returns", "mcap", "price", "offhigh", "pe", "pe3", "dividend"]);
const viewKeys = ["q", "gaps", "lens", "fit", "profiles", "sectors", "venues", "indexes", "minCap", "minMet", "minPositiveEps", "maxPe", "maxPe3", "trackedOnly", "hideNA", "hideNoApply", "belowNcav", "sort"];
const clone = (value) => structuredClone(value);
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const positive = (value) => Number.isSafeInteger(value) && value > 0;
const exactKeys = (value, keys) => object(value) && Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key));
const text = (value, min, max) => typeof value === "string" && [...value].length >= min && [...value].length <= max;
const unique = (values) => new Set(values).size === values.length;
const canonical = (value) => JSON.stringify(value, (_key, item) => object(item) ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]])) : item);
const equal = (a, b) => canonical(a) === canonical(b);

export class WorkspaceError extends Error {
  constructor(message, status = 0, currentRevision = null, uncertain = false) {
    super(message); this.status = status; this.currentRevision = currentRevision; this.uncertain = uncertain;
  }
}
const unsupported = () => { throw new WorkspaceError("Unsupported saved presentation. Reload or use a compatible client."); };

export function checkWorkspaceConfig(config) {
  if (!exactKeys(config, ["schema_revision", "view", "columns"]) || config.schema_revision !== 1
    || !exactKeys(config.view, viewKeys) || !Array.isArray(config.columns) || config.columns.length > 11
    || !unique(config.columns) || !config.columns.every((id) => workspaceColumns.includes(id))) unsupported();
  const view = config.view;
  if (!text(view.q, 0, 1000) || !["ALL", "ALL_PASS", "ONE_GAP", "TWO_GAPS", "INCOMPLETE"].includes(view.gaps)
    || !["BOTH", "ENTERPRISING", "DEFENSIVE"].includes(view.lens)
    || !["ALL", "ALIGNED", "EVIDENCE_INCOMPLETE", "BLOCKED"].includes(view.fit)) unsupported();
  for (const key of ["profiles", "sectors", "venues", "indexes"]) {
    if (!Array.isArray(view[key]) || view[key].length > 100 || !unique(view[key]) || !view[key].every((label) => text(label, 1, 120))) unsupported();
  }
  for (const key of ["minCap", "maxPe", "maxPe3"]) if (!Number.isFinite(view[key]) || view[key] < 0) unsupported();
  for (const [key, max] of [["minMet", 6], ["minPositiveEps", 10]]) if (!Number.isInteger(view[key]) || view[key] < 0 || view[key] > max) unsupported();
  for (const key of ["trackedOnly", "hideNA", "hideNoApply", "belowNcav"]) if (typeof view[key] !== "boolean") unsupported();
  if (!Array.isArray(view.sort) || view.sort.length < 1 || view.sort.length > 13 || !unique(view.sort.map((item) => item?.key))
    || !view.sort.every((item) => exactKeys(item, ["key", "dir"]) && [...workspaceColumns, "fit", "n_pass"].includes(item.key) && [1, -1].includes(item.dir))) unsupported();
  if (new TextEncoder().encode(JSON.stringify(config)).length > 65536) unsupported();
  return clone(config);
}

function checkRevision(row, workspace = null) {
  if (!object(row) || !positive(row.workspace_id) || (workspace !== null && row.workspace_id !== workspace)
    || !positive(row.edit_revision) || row.schema_revision !== 1
    || typeof row.created_at !== "string" || !Number.isFinite(Date.parse(row.created_at))
    || (row.updated_at !== undefined && (typeof row.updated_at !== "string" || !Number.isFinite(Date.parse(row.updated_at))))
    || (row.actor !== undefined && !["USER", "AGENT", "SYSTEM"].includes(row.actor))) unsupported();
  const source = row.restored_from_revision ?? null;
  if (source !== null && (!positive(source) || source >= row.edit_revision)) unsupported();
  return { ...row, configuration: checkWorkspaceConfig(row.configuration), restored_from_revision: source };
}

export function createWorkspaceTransport({ fetcher = fetch, getToken = async () => (await import("./api.js")).token(), timeoutMs = 15000 } = {}) {
  return async (path, { method = "GET", body, signal } = {}) => {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    const timer = setTimeout(abort, timeoutMs);
    let rejectAbort;
    const aborted = new Promise((_resolve, reject) => { rejectAbort = reject; });
    const onAbort = () => rejectAbort(new DOMException("Request canceled", "AbortError"));
    controller.signal.addEventListener("abort", onAbort, { once: true });
    if (controller.signal.aborted) onAbort();
    try {
      return await Promise.race([aborted, (async () => {
        const instanceToken = method === "GET" ? "" : await getToken();
        if (controller.signal.aborted) throw new DOMException("Request canceled", "AbortError");
        const response = await fetcher(`/research/workspaces${path}`, { method, body, signal: controller.signal,
          credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json",
            ...(body === undefined ? {} : { "Content-Type": "application/json" }),
            ...(instanceToken ? { "X-Screener-Token": instanceToken } : {}) } });
        const result = await response.json().catch(() => null);
        if (controller.signal.aborted) throw new DOMException("Request canceled", "AbortError");
        if ([401, 403].includes(response.status)) throw new WorkspaceError(response.status === 401
          ? "Verified sign-in is required." : "This research account is unavailable.", response.status);
        const cache = (response.headers.get("cache-control") ?? "").toLowerCase().split(",").map((part) => part.trim());
        if (!cache.includes("private") || !cache.includes("no-store")) unsupported();
        if (!response.ok) {
          const messages = { 404: "Saved workspace or revision was not found.", 409: "Workspace changed. Inspect the latest saved view before editing.",
            422: "The presentation request was rejected. Check its supported fields." };
          throw new WorkspaceError(messages[response.status] ?? "Private workspace request failed. Try again.", response.status,
            positive(result?.detail?.current_revision) ? result.detail.current_revision : null, method !== "GET" && response.status >= 500);
        }
        if (!object(result)) unsupported();
        return result;
      })()]);
    } catch (error) {
      if (signal?.aborted) throw new DOMException("Request canceled", "AbortError");
      if (error instanceof WorkspaceError) throw error;
      throw new WorkspaceError(error?.name === "AbortError" ? "Private request timed out. Delivery may be uncertain." : "Private request could not finish. Delivery may be uncertain.", 0, null, method !== "GET");
    } finally {
      clearTimeout(timer); signal?.removeEventListener("abort", abort); controller.signal.removeEventListener("abort", onAbort);
    }
  };
}

const emptyState = () => ({ phase: "idle", saved: null, current: null, draft: null, dirty: false, applicationBlocked: false,
  contextGeneration: 0, browserImport: null,
  inFlight: null, uncertain: null, needsRefresh: false, conflict: null, error: null,
  history: { phase: "idle", revisions: [], nextBefore: null, selected: null, error: null } });

export function createWorkspaceSession(onChange, { request = createWorkspaceTransport(), newKey = () => crypto.randomUUID(),
  onApply = () => {}, onColumns = () => {}, onClear = () => {}, readBrowserView = readSavedBrowserView } = {}) {
  let state = emptyState(), generation = 0, draftVersion = 0, readVersion = 0, historyVersion = 0;
  let readController, writeController, historyController, pending = null;
  const publish = () => { state.dirty = !!state.draft && !equal(state.draft, state.saved?.configuration); onChange(clone(state)); };
  const reset = (phase = "idle", error = null) => {
    generation++; readVersion++; historyVersion++;
    readController?.abort(); writeController?.abort(); historyController?.abort();
    pending = null; draftVersion = 0; state = { ...emptyState(), phase, error, contextGeneration: generation }; onClear(); publish();
  };
  const apply = (configuration) => {
    const previous = state.draft;
    state.browserImport = null;
    state.draft = clone(configuration); state.applicationBlocked = configuration.view.trackedOnly;
    if (!previous || !equal(previous.columns, configuration.columns)) onColumns(clone(configuration.columns));
    if (!previous || !equal(previous.view, configuration.view)) {
      if (state.applicationBlocked) onClear(); else onApply(clone(configuration.view));
    }
  };
  const denial = (error) => {
    if ([401, 403].includes(error.status)) { reset("denied", error.message); return true; }
    return false;
  };
  const canWrite = () => state.phase === "ready" && state.saved && !state.inFlight && !state.uncertain && !state.needsRefresh && !state.conflict;

  async function refresh(discardDraft = false) {
    if (state.uncertain || state.inFlight) return false;
    const context = generation, version = ++readVersion, edited = draftVersion;
    readController?.abort(); readController = new AbortController();
    state.phase = "loading"; state.error = null; publish();
    try {
      const row = checkRevision(await request(state.saved ? `/${state.saved.workspace_id}` : "/default", { signal: readController.signal }), state.saved?.workspace_id ?? null);
      if (context !== generation || version !== readVersion) return false;
      if (row.edit_revision < Math.max(state.saved?.edit_revision ?? 0, state.current?.edit_revision ?? 0)) unsupported();
      for (const known of [state.saved, state.current]) {
        if (known && row.edit_revision === known.edit_revision
          && (!equal(row.configuration, known.configuration) || row.restored_from_revision !== known.restored_from_revision)) unsupported();
      }
      state.current = row; state.phase = "ready"; state.needsRefresh = false;
      if (!state.saved || (discardDraft && draftVersion === edited)) {
        state.saved = row; state.conflict = null; apply(row.configuration);
      } else if (row.edit_revision > state.saved.edit_revision) {
        state.conflict = { currentRevision: row.edit_revision, message: "A newer saved view exists. Your draft is retained." };
      }
      publish(); return true;
    } catch (error) {
      if (context !== generation || version !== readVersion) return false;
      if (!denial(error)) { state.phase = "error"; state.error = error.message; publish(); }
      return false;
    }
  }

  async function perform(operation) {
    const context = generation;
    writeController = new AbortController(); state.inFlight = { kind: operation.kind, key: operation.key };
    state.uncertain = null; state.error = null; publish();
    try {
      const row = checkRevision(await request(operation.path, { method: operation.method, body: operation.body, signal: writeController.signal }), operation.workspace);
      if (context !== generation) return false;
      if (row.edit_revision !== operation.expected + 1 || !equal(row.configuration, operation.configuration)
        || row.restored_from_revision !== operation.source) unsupported();
      pending = null; state.inFlight = null; state.needsRefresh = true;
      if (!state.saved || row.edit_revision >= state.saved.edit_revision) state.saved = row;
      if (draftVersion === operation.edited) apply(row.configuration);
      publish();
      return await refresh();
    } catch (error) {
      if (context !== generation) return false;
      state.inFlight = null;
      if (denial(error)) return false;
      if (error.status === 409) { pending = null; state.conflict = { currentRevision: error.currentRevision, message: error.message }; }
      else if (error.uncertain || !error.status || error.status >= 500) state.uncertain = { kind: operation.kind, key: operation.key };
      else pending = null;
      state.error = error.message; publish(); return false;
    }
  }

  function submit(kind, source = null) {
    if (!canWrite()) return Promise.resolve(false);
    const selected = state.history.revisions.find((row) => row.edit_revision === source);
    if (kind === "restore" && (!positive(source) || !selected)) return Promise.resolve(false);
    const key = newKey(), workspace = state.saved.workspace_id, expected = state.saved.edit_revision;
    const configuration = checkWorkspaceConfig(kind === "restore" ? selected.configuration : state.draft);
    const body = kind === "save" ? { expected_revision: expected, request_key: key, configuration }
      : { expected_revision: expected, source_revision: source, request_key: key };
    pending = { kind, key, workspace, expected, configuration, source, edited: draftVersion,
      path: `/${workspace}${kind === "restore" ? "/restore" : ""}`, method: kind === "save" ? "PUT" : "POST", body: JSON.stringify(body) };
    return perform(pending);
  }

  async function history(before = null, limit = 10) {
    if (!state.saved || !["ready", "error"].includes(state.phase) || (before !== null && !positive(before)) || !positive(limit) || limit > 100) return false;
    const context = generation, version = ++historyVersion, workspace = state.saved.workspace_id;
    historyController?.abort(); historyController = new AbortController();
    state.history = { ...state.history, phase: "loading", selected: null, error: null }; publish();
    const query = new URLSearchParams({ limit: String(limit) }); if (before !== null) query.set("before", String(before));
    try {
      const result = await request(`/${workspace}/revisions?${query}`, { signal: historyController.signal });
      if (context !== generation || version !== historyVersion) return false;
      if (!Array.isArray(result.revisions) || result.revisions.length > limit) unsupported();
      const rows = result.revisions.map((row) => checkRevision(row, workspace));
      if (rows.some((row, i) => (before !== null && row.edit_revision >= before) || (i > 0 && row.edit_revision >= rows[i-1].edit_revision))) unsupported();
      if (!(result.next_before === null || (rows.length === limit && result.next_before === rows.at(-1).edit_revision))) unsupported();
      state.history = { phase: "ready", revisions: rows, nextBefore: result.next_before, selected: null, error: null }; publish(); return true;
    } catch (error) {
      if (context !== generation || version !== historyVersion) return false;
      if (!denial(error)) { state.history.phase = "error"; state.history.error = error.message; publish(); }
      return false;
    }
  }

  function setDraft(configuration) {
    if (!state.saved) return false;
    const checked = checkWorkspaceConfig(configuration);
    draftVersion++; apply(checked); publish(); return true;
  }

  function importBrowserView(context) {
    if (context !== generation || !canWrite()) return false;
    try {
      const imported = readBrowserView();
      setDraft({ ...state.draft, view: imported.view });
      state.browserImport = { status: "imported", message: "Browser view imported into your draft. Save view to keep it."
        + (imported.excludedScroll ? " Scroll position stays in this browser." : "") };
    } catch (error) {
      state.browserImport = { status: "error", message: error.message };
    }
    publish(); return state.browserImport.status === "imported";
  }

  return {
    getState: () => clone(state), canWrite,
    open: () => { reset(); return refresh(); }, close: () => reset(), reset: () => reset(),
    reload: () => refresh(true), refresh: () => refresh(),
    setDraft, importBrowserView,
    patchView: (patch) => { if (!state.draft) return false; const checked = checkWorkspaceConfig({ ...state.draft, view: { ...state.draft.view, ...patch } }); draftVersion++; apply(checked); publish(); return true; },
    setColumns: (columns) => { if (!state.draft) return false; const checked = checkWorkspaceConfig({ ...state.draft, columns }); draftVersion++; apply(checked); publish(); return true; },
    save: () => submit("save"), restore: (source) => submit("restore", source),
    retry: () => pending && state.uncertain ? perform(pending) : state.needsRefresh ? refresh() : Promise.resolve(false),
    history, selectRevision: (revision) => { state.history.selected = state.history.revisions.find((row) => row.edit_revision === revision) ?? null; publish(); },
  };
}
