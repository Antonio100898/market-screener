import { publicSitemapPaths, siteUrl } from "./site.js";

export default function sitemap() {
  return publicSitemapPaths.map((path) => ({ url: siteUrl(path) }));
}
