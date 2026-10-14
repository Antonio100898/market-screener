// Complete schema1 response fixtures, not production presentation defaults.
export const configuration = () => ({ schema_revision: 1, columns: ["price", "ticker"], view: {
  q: "ABT", gaps: "ALL", lens: "BOTH", fit: "ALL", profiles: [], sectors: ["Unseen sector"], venues: [], indexes: [],
  minCap: 0, minMet: 0, minPositiveEps: 0, maxPe: 0, maxPe3: 0, trackedOnly: false, hideNA: false,
  hideNoApply: false, belowNcav: false, sort: [{ key: "fit", dir: 1 }],
} });
export const revision = (n = 1, config = configuration(), source = null) => ({ workspace_id: 7, name: "Default",
  edit_revision: n, schema_revision: 1, configuration: structuredClone(config), restored_from_revision: source,
  actor: "USER", created_at: "2026-10-05T00:00:00Z", updated_at: "2026-10-05T00:00:00Z" });
export const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
