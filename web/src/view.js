import { normalizeSort } from "./sort.js";
import { TOTAL_CRITERIA } from "./screen.js";

// Filters, sort and scroll position survive a refresh. Kept in localStorage rather
// than the URL: this is a working view you return to, not something you link to,
// and Sets serialise badly as query strings.
const KEY = "screener-view";

/** Every table-filter control in its genuinely unfiltered state. */
export function unfilteredView() {
  return {
    gaps: "ALL",
    lens: "BOTH",
    fit: "ALL",
    profiles: [],
    sectors: [],
    venues: [],
    indexes: [],
    minCap: 0,
    minMet: 0,
    minPositiveEps: 0,
    maxPe: 0,
    maxPe3: 0,
    trackedOnly: false,
    hideNA: false,
    hideNoApply: false,
    belowNcav: false,
  };
}

const selected = (value) => value?.size ?? value?.length ?? 0;

/** Whether the table is narrower than the full loaded universe. */
export function hasActiveFilters(view) {
  return view.lens !== "BOTH"
    || view.fit !== "ALL"
    || view.gaps !== "ALL"
    || selected(view.profiles) > 0
    || selected(view.sectors) > 0
    || selected(view.venues) > 0
    || selected(view.indexes) > 0
    || view.minCap > 0
    || view.minMet > 0
    || view.minPositiveEps > 0
    || view.maxPe > 0
    || view.maxPe3 > 0
    || Boolean(view.trackedOnly)
    || Boolean(view.hideNA)
    || Boolean(view.hideNoApply)
    || Boolean(view.belowNcav);
}

const DEFAULTS = {
  q: "",
  ...unfilteredView(),
  sort: { key: "fit", dir: 1 },
  scroll: 0,
};

const TABLE_SORT_KEYS = new Set([
  "ticker", "name", "sector", "eps10", "returns", "mcap", "price", "offhigh",
  "pe", "pe3", "dividend",
]);

export function legacyViewSort(value) {
  return normalizeSort(normalizeSort(value)
    .map((item) => ["grade", "ptbv", "fit"].includes(item.key) ? { key: "n_pass", dir: 1 } : item)
    .filter((item) => TABLE_SORT_KEYS.has(item.key)));
}

export function readSavedBrowserView(storage = localStorage) {
  let raw;
  try { raw = storage.getItem(KEY); }
  catch { throw new Error("This browser's saved view could not be read."); }
  if (raw === null) throw new Error("No saved browser view was found.");
  if (typeof raw !== "string" || new TextEncoder().encode(raw).length > 65536)
    throw new Error("This browser's saved view is too large or unsupported.");
  let saved;
  try { saved = JSON.parse(raw); }
  catch { throw new Error("This browser's saved view is not valid JSON."); }
  if (saved === null || typeof saved !== "object" || Array.isArray(saved)
    || Object.keys(saved).some((key) => !Object.hasOwn(DEFAULTS, key)))
    throw new Error("This browser's saved view has unsupported fields.");
  if (Object.hasOwn(saved, "scroll") && (!Number.isFinite(saved.scroll) || saved.scroll < 0))
    throw new Error("This browser's saved scroll position is invalid.");
  const { scroll, ...view } = { ...DEFAULTS, ...saved };
  if (Number.isInteger(view.minMet) && view.minMet >= 0) view.minMet = Math.min(view.minMet, TOTAL_CRITERIA);
  const sorts = Array.isArray(view.sort) ? view.sort : [view.sort];
  if (!sorts.length || sorts.some((item) => !item || typeof item !== "object" || Array.isArray(item)
    || Object.keys(item).length !== 2 || !Object.hasOwn(item, "key") || !Object.hasOwn(item, "dir")
    || ![...TABLE_SORT_KEYS, "grade", "ptbv", "fit", "n_pass"].includes(item.key) || ![1, -1].includes(item.dir)))
    throw new Error("This browser's saved sort is unsupported.");
  return { view: { ...view, sort: legacyViewSort(view.sort) }, excludedScroll: Object.hasOwn(saved, "scroll") };
}

export function loadView() {
  try {
    return { ...DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY)) || {}) };
  } catch {
    return { ...DEFAULTS };   // corrupt or unavailable storage must not break the app
  }
}

let pending = null;

export function saveView(patch) {
  const next = { ...loadView(), ...patch };
  // scrolling fires constantly; coalesce writes into one per frame
  cancelAnimationFrame(pending);
  pending = requestAnimationFrame(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      /* private mode or quota — the view simply will not persist */
    }
  });
}

/** Browsers restore scroll before the rows exist, which lands in the wrong place. */
export function takeOverScrollRestoration() {
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";
}
