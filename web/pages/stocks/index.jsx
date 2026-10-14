import { PublicDataError, readPublicCompanyIndex, markPublicSnapshotResponse } from "../../app/publicData.js";
import { siteUrl } from "../../app/site.js";
import PublicPage, { UnavailableContent } from "../../components/PublicPage.jsx";
import CompanyDirectory from "../../app/stocks/CompanyDirectory.jsx";

export async function getServerSideProps({ query, res }) {
  const canonical = siteUrl("/stocks");
  const search = new URLSearchParams();
  for (const key of ["cursor", "release"]) if (typeof query[key] === "string") search.set(key, query[key]);
  try {
    const result = await readPublicCompanyIndex(search);
    markPublicSnapshotResponse(res, 200);
    return { props: { result, error: null, canonical } };
  } catch (error) {
    if (!(error instanceof PublicDataError)) throw error;
    markPublicSnapshotResponse(res, error.status);
    return { props: { result: null, error: { status: error.status, message: error.message }, canonical } };
  }
}

export default function CompanyDirectoryPage({ result, error, canonical }) {
  return <PublicPage title="Company directory" description="Company identities in a published data release." canonical={canonical}>
    {result ? <CompanyDirectory result={result} /> : <UnavailableContent {...error} />}
  </PublicPage>;
}
