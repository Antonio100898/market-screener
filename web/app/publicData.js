import { backendOrigin } from "../backend.js";

export class PublicDataError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function markPublicSnapshotResponse(res, status) {
  res.statusCode = status;
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Robots-Tag", "noindex, nofollow");
  if (status === 503) res.setHeader("Retry-After", "60");
}

export async function readPublicApi(path, fetcher = fetch, { allowNotFound = false } = {}) {
  let response;
  try {
    response = await fetcher(`${backendOrigin}/api/v1${path}`, {
      cache: "no-store", signal: AbortSignal.timeout(8_000),
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new PublicDataError(503, "Published company data is unavailable. Try again later.");
  }
  if (!response.ok) {
    const absent = response.status === 404 && allowNotFound;
    throw new PublicDataError(absent ? 404 : 503,
      absent ? "Company not found in the published release." : "Published company data is unavailable. Try again later.");
  }
  try {
    const result = await response.json();
    if (!isRecord(result)) throw new Error("Unsupported response");
    return result;
  }
  catch { throw new PublicDataError(503, "The company service returned an unsupported response."); }
}

const isRecord = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const optionalText = (value) => value == null || typeof value === "string";

function validSource(source) {
  return source == null || (isRecord(source)
    && ["tag", "form", "accn", "end", "unit", "document", "concept"].every((key) => optionalText(source[key]))
    && (source.components == null || (Array.isArray(source.components) && source.components.every(validSource))));
}

function validCompanyRow(row) {
  return isRecord(row) && typeof row.ticker === "string" && row.ticker.length > 0
    && ["name", "cik", "reporting_currency", "currency", "verdict"].every((key) => optionalText(row[key]))
    && (row.asset_quality == null || isRecord(row.asset_quality))
    && (row.sources == null || (isRecord(row.sources) && Object.values(row.sources).every(validSource)))
    && (row.criteria == null || (Array.isArray(row.criteria) && row.criteria.every((criterion) =>
      isRecord(criterion) && [1, 2, 3, 4, 5, 7].includes(criterion.n)
      && typeof criterion.status === "string" && optionalText(criterion.note))))
    && (row.context_notes == null || (Array.isArray(row.context_notes) && row.context_notes.every((note) =>
      typeof note === "string" || (isRecord(note) && typeof note.text === "string"))))
    && (row.assumptions == null || (Array.isArray(row.assumptions) && row.assumptions.every((note) => typeof note === "string")));
}

function checkRelease(release) {
  if (!Number.isSafeInteger(release?.release_id) || release.release_id <= 0 || release.projection_revision !== 1 || !optionalText(release.published_at))
    throw new PublicDataError(503, "The company service returned an unsupported release.");
}

export async function readPublicCompany(ticker, fetcher = fetch) {
  const [capabilities, status] = await Promise.all([
    readPublicApi("/capabilities", fetcher), readPublicApi("/data-status", fetcher),
  ]);
  if (capabilities.api_contract_major !== 1 || !status.projection_ready)
    throw new PublicDataError(503, "Published company data is not ready.");
  checkRelease(status.release);
  const result = await readPublicApi(`/companies/by-ticker/${encodeURIComponent(ticker)}?release=${status.release.release_id}&assume_absent_zero=false`, fetcher, { allowNotFound: true });
  checkRelease(result.release);
  if (result.release.release_id !== status.release.release_id || !validCompanyRow(result.row) || result.row.ticker !== ticker.toUpperCase())
    throw new PublicDataError(503, "The company service returned an inconsistent release.");
  return result;
}

export async function readPublicCompanyIndex(search, fetcher = fetch) {
  const query = new URLSearchParams({ row_fields: "identity", limit: "100" });
  for (const key of ["cursor", "release"]) if (search.has(key)) query.set(key, search.get(key));
  const result = await readPublicApi(`/companies?${query}`, fetcher);
  checkRelease(result.release);
  if (search.has("release") && result.release.release_id !== Number(search.get("release")))
    throw new PublicDataError(503, "The company service returned an inconsistent release.");
  if (!Array.isArray(result.rows) || result.rows.length > 100 || result.rows.some((row) =>
    !isRecord(row) || !Number.isSafeInteger(row.security_id) || row.security_id <= 0
    || typeof row.ticker !== "string" || !row.ticker || typeof row.name !== "string")
    || (result.next_cursor != null && typeof result.next_cursor !== "string")
    || !Number.isSafeInteger(result.matched) || result.matched < 0)
    throw new PublicDataError(503, "The company service returned an unsupported company list.");
  return result;
}

export function figureText(value) {
  if (value == null) return "Missing evidence";
  if (typeof value !== "number" || !Number.isFinite(value)) return "Unavailable";
  return new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 }).format(value);
}
