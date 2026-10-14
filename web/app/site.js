export const siteOrigin = (process.env.SCREENER_SITE_ORIGIN || "http://127.0.0.1:3000").replace(/\/$/, "");
export const siteUrl = (path) => `${siteOrigin}${path}`;

// Company snapshots and authored drafts await the publication readiness gate.
export const publicSitemapPaths = ["/"];

export const draftMetadata = (title, description, path) => ({
  title: `${title} | Market Screener`, description,
  alternates: { canonical: siteUrl(path) },
  robots: { index: false, follow: false },
});
