import { PublicDataError, readPublicCompany, markPublicSnapshotResponse } from "../../app/publicData.js";
import { siteUrl } from "../../app/site.js";
import PublicPage, { UnavailableContent } from "../../components/PublicPage.jsx";
import CompanySnapshot from "../../app/CompanySnapshot.jsx";

export async function getServerSideProps({ params, res }) {
  const canonical = siteUrl(`/stocks/${encodeURIComponent(params.ticker.toUpperCase())}`);
  try {
    const result = await readPublicCompany(params.ticker);
    markPublicSnapshotResponse(res, 200);
    return { props: { result, error: null, canonical } };
  } catch (error) {
    if (!(error instanceof PublicDataError)) throw error;
    markPublicSnapshotResponse(res, error.status);
    return { props: { result: null, error: { status: error.status, message: error.message }, canonical } };
  }
}

export default function CompanyPage({ result, error, canonical }) {
  const title = result ? `${result.row.name || result.row.ticker} (${result.row.ticker})`
    : error.status === 404 ? "Company not found" : "Company data unavailable";
  return <PublicPage title={title} canonical={canonical}
    description={result ? "Published company figures and their filing sources. Source freshness is unverified." : title}>
    {result ? <CompanySnapshot result={result} /> : <UnavailableContent {...error} />}
  </PublicPage>;
}
